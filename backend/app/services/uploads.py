"""任务交付物上传存储(ZIP 解压存树 / 单文件原样存)

存储落点由存储后端抽象(settings.STORAGE_BACKEND,见 app/services/upload_storage.py)
决定:local 后端为 UPLOADS_DIR/{upload_id}/files/,s3 后端为对象 key
{S3_PREFIX}{upload_id}/files/{relpath}。本模块只负责校验 + 门面,不直接碰磁盘/对象布局:
- file:单文件原样存到 {upload_id}/files/<filename>
- zip:校验后解压成树存到 {upload_id}/files/

每个上传目录带 meta.json(filename/kind/size/file_count/user_id/created_at):
- 任务创建时校验归属(只能引用自己上传的文件)
- 任务创建后长期保留,失败重试 / 完成后追问 resume 时可重新取用
  (上传文件 agent 无法自行重新获取,服务端必须保留)

安全边界(settings.UPLOAD_MAX_*):
- 上传体 ≤ UPLOAD_MAX_FILE_MB(路由层分块读,超限即停,不整读进内存)
- zip:单文件 ≤ UPLOAD_MAX_SINGLE_FILE_MB、解压总大小 ≤ UPLOAD_MAX_EXTRACT_MB
  (防解压炸弹)、条目数 ≤ UPLOAD_MAX_FILES
- zip-slip:拒绝绝对路径与 .. 穿越(同 skills/uploader.py 的 _validate_entry_name)
- 跳过 Mac zip 噪音(__MACOSX/.DS_Store);非 UTF-8 文件名拒绝

与 skill 上传的区别:交付物不设扩展名白名单(项目文件类型不受限),
改用解压炸弹限制兜底。
"""
import io
import json
import logging
import shutil
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import settings
from app.services.upload_storage import (
    FILES_DIR,
    META_FILE,
    UploadError,
    get_backend,
)

# UploadError 下移到存储层(后端直接抛出),此处 re-export 保持
# `from app.services.uploads import UploadError` 的既有导入不变。
__all__ = [
    "UploadError",
    "save_upload",
    "load_upload_meta",
    "materialize_upload_files",
    "validate_upload_for_task",
]

logger = logging.getLogger(__name__)

# Mac zip 常见噪音(打包时自动生成的资源分叉 / 元数据)
_SKIP_PREFIXES = ("__MACOSX/",)
_SKIP_NAMES = {".DS_Store"}


def _validate_entry_name(name: str) -> None:
    """校验 zip 条目名:拒绝绝对路径与 .. 穿越

    抛出 UploadError:非法条目名
    """
    if name.startswith("/") or "\\" in name:
        raise UploadError(f"zip 条目名不合法(绝对路径): {name}")
    if ".." in Path(name).parts:
        raise UploadError(f"zip 条目名不合法(路径穿越): {name}")


def _is_skippable(name: str) -> bool:
    """Mac zip 噪音(资源分叉目录 / .DS_Store)跳过"""
    if name in _SKIP_NAMES:
        return True
    return any(name.startswith(p) for p in _SKIP_PREFIXES)


def _sanitize_single_filename(filename: str) -> str:
    """单文件名清洗:只保留 basename(客户端可能传带路径的名字)

    抛出 UploadError:清洗后为空
    """
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in (".", ".."):
        raise UploadError("上传文件名不合法")
    return name


def save_upload(data: bytes, filename: str, user_id) -> dict:
    """校验并存储一次上传,返回 meta dict

    参数:
        data: 上传体完整字节(路由层已限制 ≤ UPLOAD_MAX_FILE_MB)
        filename: 客户端原始文件名(.zip 结尾按 zip 处理,否则按单文件)
        user_id: 上传者用户 id(写入 meta,任务创建时校验归属)

    抛出 UploadError:校验失败(zip 非法、超限、条目名不合法等)
    """
    max_file = settings.UPLOAD_MAX_FILE_MB * 1024 * 1024
    if len(data) > max_file:
        raise UploadError(f"上传内容超过大小上限 {settings.UPLOAD_MAX_FILE_MB}MB")
    if not data:
        raise UploadError("上传内容为空")

    is_zip = (filename or "").lower().endswith(".zip")
    # 时间戳前缀便于按目录排查;唯一性由 uuid 尾部保证
    upload_id = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:12]}"
    )

    # 存储后端提供本地暂存根:local=最终目录(is_temp=False),s3=临时目录(is_temp=True)
    backend = get_backend()
    staging_root, is_temp = backend.staging_root(upload_id)
    files_dir = staging_root / FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)

    try:
        if is_zip:
            file_count = _extract_zip_tree(data, files_dir)
            kind = "zip"
        else:
            name = _sanitize_single_filename(filename)
            (files_dir / name).write_bytes(data)
            file_count = 1
            kind = "file"

        meta = {
            "upload_id": upload_id,
            "kind": kind,
            "filename": filename or "upload",
            "size": len(data),
            "file_count": file_count,
            "user_id": str(user_id),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (staging_root / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # local:staging 即最终位置(no-op);s3:上传整棵树为对象
        backend.persist(upload_id, staging_root)
    except Exception:
        # 落盘/上传失败:清理半成品,避免残留脏目录/孤儿对象
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    finally:
        # s3 临时暂存目录用完即删;local 的 staging 是真源,绝不能删
        if is_temp:
            shutil.rmtree(staging_root, ignore_errors=True)

    logger.info(
        f"[uploads] 保存成功: upload_id={upload_id}, kind={kind}, "
        f"filename={filename}, file_count={file_count}, size={len(data)}"
    )
    return meta


def _extract_zip_tree(zip_bytes: bytes, files_dir: Path) -> int:
    """校验并解压 zip 到 files_dir,返回解压出的文件数

    先全量预检(条目名/单文件/总大小/条目数),全部通过才落盘。
    抛出 UploadError / zipfile.BadZipFile(由调用方统一转 UploadError)。
    """
    max_single = settings.UPLOAD_MAX_SINGLE_FILE_MB * 1024 * 1024
    max_extract = settings.UPLOAD_MAX_EXTRACT_MB * 1024 * 1024
    max_files = settings.UPLOAD_MAX_FILES

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise UploadError(f"不是合法的 zip 文件: {e}") from e

    with zf:
        entries: list[zipfile.ZipInfo] = []
        total_size = 0
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or _is_skippable(name):
                continue
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                raise UploadError(f"zip 条目名不是 UTF-8 编码,拒绝: {name!r}")
            _validate_entry_name(name)

            if info.file_size > max_single:
                raise UploadError(
                    f"zip 内单文件超过大小上限 {settings.UPLOAD_MAX_SINGLE_FILE_MB}MB: {name}"
                )
            total_size += info.file_size
            if total_size > max_extract:
                raise UploadError(
                    f"解压后总大小超过上限 {settings.UPLOAD_MAX_EXTRACT_MB}MB"
                )
            if len(entries) + 1 > max_files:
                raise UploadError(f"zip 内文件数超过上限 {max_files}")
            entries.append(info)

        if not entries:
            raise UploadError("zip 内没有有效文件")

        for info in entries:
            target = files_dir / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)

    return len(entries)


def _check_upload_id(upload_id: str) -> None:
    """upload_id 基本格式校验(防路径拼接注入)"""
    if not upload_id or not isinstance(upload_id, str):
        raise UploadError("upload_id 不合法")
    if any(c not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-" for c in upload_id):
        raise UploadError("upload_id 不合法")


def load_upload_meta(upload_id: str) -> dict:
    """读取上传的 meta.json(委托存储后端)

    抛出 UploadError:upload_id 不合法或上传不存在 / 损坏
    """
    _check_upload_id(upload_id)
    return get_backend().load_meta(upload_id)


@contextmanager
def materialize_upload_files(upload_id: str) -> Iterator[Path]:
    """产出上传内容 files 目录的本地路径(上下文管理器;orchestrator 传输进沙箱用)

    local 后端:yield 真源目录,退出**不删除**(Stage 1 长期保留);
    s3 后端:下载到临时目录,yield 后退出自动清理。
    调用方应在 with 块内完成 transfer_upload_to_workspace。

    抛出 UploadError:upload_id 不合法 / 上传不存在 / files 缺失
    """
    _check_upload_id(upload_id)
    with get_backend().materialize_files(upload_id) as files_dir:
        yield files_dir


def validate_upload_for_task(upload_id: str, current_user_id) -> dict:
    """任务创建时校验上传可引用:存在 + 归属当前用户

    返回 meta dict。抛出 UploadError:
    - 上传不存在(404 语义)
    - 未登录(current_user_id 为 None)或非上传者(403 语义)
    """
    meta = load_upload_meta(upload_id)
    if current_user_id is None:
        raise UploadError("引用上传文件需要先登录")
    if str(meta.get("user_id")) != str(current_user_id):
        raise UploadError("无权引用他人的上传文件")
    return meta
