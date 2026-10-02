"""交付物存储后端抽象(Stage 1 永久层)

两阶段上传架构中的 Stage 1:永久、可再生的交付物真源。Stage 2(把内容
传进沙箱/工作区的临时副本)见 app/tools/sandbox_tools.py:transfer_upload_to_workspace,
不经过本模块。

后端由部署级配置 settings.STORAGE_BACKEND 选择(一套部署一个后端):
- local:本地磁盘 UPLOADS_DIR/{upload_id}/{files/,meta.json}(默认;须挂持久卷)
- s3:S3 兼容对象存储(MinIO / 阿里云 OSS S3 兼容端点 / AWS S3),
  对象 key = {S3_PREFIX}{upload_id}/{files/...,meta.json}

统一目录/键布局:每个 upload_id 下含 files/(交付物树)与 meta.json(元数据)。
local 直接映射为磁盘目录,s3 映射为对象 key 前缀,语义一致。

materialize_files 返回上下文管理器:local 直接给出真实目录且退出**不删除**
(保护 Stage 1 真源);s3 下载到临时目录、退出即清理。调用方(orchestrator)
在 with 块内把目录交给 transfer_upload_to_workspace 传进沙箱。

boto3 仅在 s3 后端惰性 import:local-only 部署即使未安装 boto3 也不受影响。
"""
import json
import logging
import shutil
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings

logger = logging.getLogger(__name__)

# meta.json 文件名(存储在 {upload_id}/ 下,与 files/ 同级)
META_FILE = "meta.json"
# 交付物树子目录名
FILES_DIR = "files"


class UploadError(ValueError):
    """上传存储相关错误(不存在 / 损坏 / 后端配置缺失)。调用方转 HTTP 状态码。

    历史上定义在 app/services/uploads.py;下移到存储层以便后端直接抛出,
    uploads.py 门面 re-export 保持 `from app.services.uploads import UploadError` 不变。
    """


def _safe_relpath(rel: str) -> str | None:
    """清洗相对路径:拒绝绝对路径与 .. 穿越(防对象 key / 磁盘路径注入)

    返回规范化 posix 相对路径;非法返回 None。
    """
    name = (rel or "").replace("\\", "/").lstrip("/")
    if not name:
        return None
    parts = Path(name).parts
    if ".." in parts:
        return None
    return "/".join(parts)


class UploadStorageBackend(ABC):
    """交付物存储后端统一接口(local / s3 共用)"""

    @abstractmethod
    def staging_root(self, upload_id: str) -> tuple[Path, bool]:
        """返回写入用的本地暂存根目录 + 是否为临时目录

        调用方在返回目录下创建 files/ 落盘交付物、写 meta.json,随后调用
        persist()。is_temp=True(s3)时调用方负责在 persist 后清理该临时目录;
        is_temp=False(local)时暂存根即最终位置,不得清理。
        """

    @abstractmethod
    def persist(self, upload_id: str, staging_root: Path) -> None:
        """把已备好的暂存根(含 files/ 与 meta.json)落到后端

        local:暂存根已在 UPLOADS_DIR/{id}/ 下,no-op;s3:上传整棵树为对象。
        """

    @abstractmethod
    def load_meta(self, upload_id: str) -> dict:
        """读取 meta.json。抛出 UploadError:不存在 / 损坏。"""

    @abstractmethod
    @contextmanager
    def materialize_files(self, upload_id: str) -> Iterator[Path]:
        """产出交付物 files 目录的本地路径(上下文管理器)

        local:yield 真实目录,退出**不删除**(保护 Stage 1);
        s3:下载到临时目录,yield 后退出清理临时目录。
        抛出 UploadError:不存在。
        """

    @abstractmethod
    def list_upload_ids(self) -> list[str]:
        """列出后端内所有 upload_id(GC 遍历用)"""

    @abstractmethod
    def delete(self, upload_id: str) -> None:
        """删除某 upload 的全部内容(GC 用)。不存在时静默返回。"""

    @abstractmethod
    def exists(self, upload_id: str) -> bool:
        """upload_id 是否存在于后端"""


class LocalUploadStorage(UploadStorageBackend):
    """本地磁盘后端:UPLOADS_DIR/{upload_id}/{files/,meta.json}

    无状态:每次调用实时读取 settings.UPLOADS_DIR,便于测试用 monkeypatch 切换目录。
    """

    def _root(self) -> Path:
        root = Path(settings.UPLOADS_DIR)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _upload_dir(self, upload_id: str) -> Path:
        return self._root() / upload_id

    def staging_root(self, upload_id: str) -> tuple[Path, bool]:
        upload_dir = self._upload_dir(upload_id)
        (upload_dir / FILES_DIR).mkdir(parents=True, exist_ok=True)
        return upload_dir, False

    def persist(self, upload_id: str, staging_root: Path) -> None:
        # local:staging_root 即最终目录,内容已就位,无需搬运
        return None

    def load_meta(self, upload_id: str) -> dict:
        meta_path = self._upload_dir(upload_id) / META_FILE
        if not meta_path.is_file():
            raise UploadError(f"上传不存在或已被清理: {upload_id}")
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise UploadError(f"上传元数据损坏: {upload_id}") from e

    @contextmanager
    def materialize_files(self, upload_id: str) -> Iterator[Path]:
        files_dir = self._upload_dir(upload_id) / FILES_DIR
        if not files_dir.is_dir():
            raise UploadError(f"上传不存在或已被清理: {upload_id}")
        # local 后端 yield 真源目录,退出绝不删除(Stage 1 必须长期保留)
        yield files_dir

    def list_upload_ids(self) -> list[str]:
        root = self._root()
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    def delete(self, upload_id: str) -> None:
        upload_dir = self._upload_dir(upload_id)
        if upload_dir.is_dir():
            shutil.rmtree(upload_dir, ignore_errors=True)

    def exists(self, upload_id: str) -> bool:
        return self._upload_dir(upload_id).is_dir()


class S3UploadStorage(UploadStorageBackend):
    """S3 兼容对象存储后端(MinIO / 阿里云 OSS S3 兼容端点 / AWS S3)

    对象布局:{S3_PREFIX}{upload_id}/files/{relpath} 与 {S3_PREFIX}{upload_id}/meta.json。
    boto3 客户端惰性创建并缓存于实例(_client())。
    """

    def __init__(self) -> None:
        self._s3 = None

    def _client(self):
        if self._s3 is None:
            try:
                import boto3  # 惰性 import:local-only 部署无需安装
            except ImportError as e:  # pragma: no cover - 依赖缺失路径
                raise UploadError(
                    "STORAGE_BACKEND=s3 需要安装 boto3(pip install boto3)"
                ) from e
            kwargs: dict = {}
            if settings.S3_ENDPOINT_URL:
                kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL
            if settings.S3_REGION:
                kwargs["region_name"] = settings.S3_REGION
            if settings.S3_ACCESS_KEY_ID:
                kwargs["aws_access_key_id"] = settings.S3_ACCESS_KEY_ID
            if settings.S3_SECRET_ACCESS_KEY:
                kwargs["aws_secret_access_key"] = settings.S3_SECRET_ACCESS_KEY
            self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _bucket(self) -> str:
        if not settings.S3_BUCKET:
            raise UploadError("STORAGE_BACKEND=s3 需要配置 S3_BUCKET")
        return settings.S3_BUCKET

    def _prefix(self) -> str:
        p = settings.S3_PREFIX or ""
        if p and not p.endswith("/"):
            p += "/"
        return p

    def _upload_prefix(self, upload_id: str) -> str:
        return f"{self._prefix()}{upload_id}/"

    def staging_root(self, upload_id: str) -> tuple[Path, bool]:
        tmp = Path(tempfile.mkdtemp(prefix=f"upload_staging_{upload_id}_"))
        (tmp / FILES_DIR).mkdir(parents=True, exist_ok=True)
        return tmp, True

    def persist(self, upload_id: str, staging_root: Path) -> None:
        client = self._client()
        bucket = self._bucket()
        base = self._upload_prefix(upload_id)
        for p in sorted(staging_root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(staging_root).as_posix()
            client.upload_file(str(p), bucket, f"{base}{rel}")

    def load_meta(self, upload_id: str) -> dict:
        client = self._client()
        key = f"{self._upload_prefix(upload_id)}{META_FILE}"
        try:
            resp = client.get_object(Bucket=self._bucket(), Key=key)
        except Exception as e:
            if _is_not_found(e):
                raise UploadError(f"上传不存在或已被清理: {upload_id}") from None
            raise UploadError(f"读取上传元数据失败: {upload_id}") from e
        try:
            body = resp["Body"].read()
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as e:
            raise UploadError(f"上传元数据损坏: {upload_id}") from e

    @contextmanager
    def materialize_files(self, upload_id: str) -> Iterator[Path]:
        client = self._client()
        bucket = self._bucket()
        files_prefix = f"{self._upload_prefix(upload_id)}{FILES_DIR}/"
        tmp = Path(tempfile.mkdtemp(prefix=f"upload_files_{upload_id}_"))
        count = 0
        try:
            for obj in self._list_objects(files_prefix):
                rel = _safe_relpath(obj["Key"][len(files_prefix):])
                if not rel:
                    continue
                target = tmp / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(bucket, obj["Key"], str(target))
                count += 1
            if count == 0:
                raise UploadError(f"上传不存在或已被清理: {upload_id}")
            yield tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _list_objects(self, prefix: str) -> list[dict]:
        """列出 prefix 下所有对象(处理分页)"""
        client = self._client()
        bucket = self._bucket()
        out: list[dict] = []
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            out.extend(resp.get("Contents", []) or [])
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
        return out

    def list_upload_ids(self) -> list[str]:
        client = self._client()
        bucket = self._bucket()
        prefix = self._prefix()
        ids: set[str] = set()
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/"}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            for cp in resp.get("CommonPrefixes", []) or []:
                p = cp.get("Prefix", "")
                uid = p[len(prefix):].rstrip("/")
                if uid:
                    ids.add(uid)
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
        return sorted(ids)

    def delete(self, upload_id: str) -> None:
        client = self._client()
        bucket = self._bucket()
        prefix = self._upload_prefix(upload_id)
        objs = self._list_objects(prefix)
        if not objs:
            return
        # delete_objects 单次上限 1000
        for i in range(0, len(objs), 1000):
            batch = objs[i:i + 1000]
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": o["Key"]} for o in batch], "Quiet": True},
            )

    def exists(self, upload_id: str) -> bool:
        client = self._client()
        key = f"{self._upload_prefix(upload_id)}{META_FILE}"
        try:
            client.head_object(Bucket=self._bucket(), Key=key)
            return True
        except Exception as e:
            if _is_not_found(e):
                return False
            raise UploadError(f"检查上传是否存在失败: {upload_id}") from e


def _is_not_found(exc: Exception) -> bool:
    """判断 boto3 异常是否为 404 / NoSuchKey(不同 S3 实现返回结构不一)"""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = str((resp.get("Error") or {}).get("Code", ""))
        status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchKey", "NotFound", "NoSuchBucket") or status == 404:
            return True
    return exc.__class__.__name__ in ("NoSuchKey", "ClientError") and "404" in str(exc)


# ---- 后端工厂(按 STORAGE_BACKEND 缓存单例;local 后端无状态、实时读 settings)----
_backend_cache: dict[str, UploadStorageBackend] = {}


def get_backend() -> UploadStorageBackend:
    """返回当前配置的存储后端单例

    抛出 ValueError:STORAGE_BACKEND 取值非法(既非 local 也非 s3)
    """
    name = (settings.STORAGE_BACKEND or "local").strip().lower()
    if name not in _backend_cache:
        if name == "local":
            _backend_cache[name] = LocalUploadStorage()
        elif name == "s3":
            _backend_cache[name] = S3UploadStorage()
        else:
            raise ValueError(f"未知的 STORAGE_BACKEND: {name!r}(仅支持 local / s3)")
    return _backend_cache[name]


def reset_backend_cache() -> None:
    """清空后端缓存(测试切换 STORAGE_BACKEND / S3_* 后调用)"""
    _backend_cache.clear()
