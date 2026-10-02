"""任务交付物上传服务单元测试

覆盖 services/uploads.save_upload 及周边:
- zip 正常解压存树 / Mac 噪音(__MACOSX、.DS_Store)跳过
- 单文件原样存(basename 清洗)
- zip-slip 路径穿越 / 绝对路径 / 非 UTF-8 文件名拒绝
- 大小限制(单文件 / 解压总量,用 monkeypatch 缩小阈值)
- 条目数上限 / 空 zip / 非 zip 内容伪装 .zip 拒绝
- 归属校验(他人上传 / 不存在 / 非法 upload_id)
"""
import io
import zipfile

import pytest

from app.config import settings
from app.services.upload_storage import get_backend, reset_backend_cache
from app.services.uploads import (
    UploadError,
    load_upload_meta,
    materialize_upload_files,
    save_upload,
    validate_upload_for_task,
)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """按 {路径: 内容} 构造 zip 字节"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _uploads_dir(tmp_path, monkeypatch):
    """每个用例用独立的上传目录,不污染真实数据目录"""
    uploads_dir = tmp_path / "uploads_data"
    monkeypatch.setattr(settings, "UPLOADS_DIR", str(uploads_dir))
    # 后端缓存与用例隔离(防其他测试文件的 s3 单例串场;local 后端无状态)
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    reset_backend_cache()
    return uploads_dir


# ============================================================
# 正常路径:zip / 单文件
# ============================================================

def test_save_zip_extracts_tree():
    """zip 解压成树,目录结构与文件数正确记录"""
    data = _make_zip({
        "src/main.py": "print(1)",
        "dir/sub/a.txt": "hello",
    })
    meta = save_upload(data, "project.zip", "u1")
    assert meta["kind"] == "zip"
    assert meta["file_count"] == 2
    assert meta["size"] == len(data)
    with materialize_upload_files(meta["upload_id"]) as files:
        assert (files / "src" / "main.py").read_text() == "print(1)"
        assert (files / "dir" / "sub" / "a.txt").read_text() == "hello"
    # meta.json 可回读
    m = load_upload_meta(meta["upload_id"])
    assert m["filename"] == "project.zip"
    assert m["user_id"] == "u1"


def test_save_zip_skips_mac_noise():
    """__MACOSX 资源分叉与 .DS_Store 跳过,不计入 file_count"""
    data = _make_zip({
        "README.md": "hi",
        "__MACOSX/README.md": "junk",
        ".DS_Store": "junk",
        "__MACOSX/dir/.DS_Store": "junk",
    })
    meta = save_upload(data, "noise.zip", "u1")
    assert meta["file_count"] == 1
    with materialize_upload_files(meta["upload_id"]) as files:
        assert (files / "README.md").read_text() == "hi"
        assert not (files / "__MACOSX").exists()
        assert not (files / ".DS_Store").exists()


def test_save_single_file():
    """单文件原样存,kind=file"""
    meta = save_upload(b"contract body", "contract.docx", "u1")
    assert meta["kind"] == "file"
    assert meta["file_count"] == 1
    with materialize_upload_files(meta["upload_id"]) as files:
        assert (files / "contract.docx").read_bytes() == b"contract body"


def test_save_single_file_strips_path_components():
    """客户端传带路径的文件名只保留 basename(防落到 files 外)"""
    meta = save_upload(b"x", "a/b/c.py", "u1")
    with materialize_upload_files(meta["upload_id"]) as files:
        assert list(files.iterdir())[0].name == "c.py"


# ============================================================
# 非法输入:zip-slip / 编码 / 空内容
# ============================================================

def test_zip_slip_rejected():
    """.. 穿越条目拒绝"""
    data = _make_zip({"../evil.txt": "x"})
    with pytest.raises(UploadError, match="路径穿越"):
        save_upload(data, "evil.zip", "u1")


def test_absolute_path_rejected():
    """绝对路径条目拒绝"""
    data = _make_zip({"/etc/evil.txt": "x"})
    with pytest.raises(UploadError, match="绝对路径"):
        save_upload(data, "evil.zip", "u1")


def test_bad_zip_bytes_rejected():
    """非 zip 内容伪装 .zip 后缀拒绝"""
    with pytest.raises(UploadError, match="不是合法的 zip"):
        save_upload(b"definitely not zip", "fake.zip", "u1")


def test_empty_zip_rejected():
    """只有目录条目(无文件)的 zip 拒绝"""
    data = _make_zip({"only-dir/": ""})
    with pytest.raises(UploadError, match="没有有效文件"):
        save_upload(data, "empty.zip", "u1")


def test_empty_content_rejected():
    """空上传体拒绝"""
    with pytest.raises(UploadError, match="为空"):
        save_upload(b"", "a.txt", "u1")


# ============================================================
# 大小 / 条目数限制(monkeypatch 缩小阈值)
# ============================================================

def test_zip_single_file_size_limit(monkeypatch):
    """zip 内单文件超限拒绝"""
    monkeypatch.setattr(settings, "UPLOAD_MAX_SINGLE_FILE_MB", 0)
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILE_MB", 1)
    data = _make_zip({"big.bin": b"x" * 1024})
    with pytest.raises(UploadError, match="单文件超过大小上限"):
        save_upload(data, "big.zip", "u1")


def test_zip_total_extract_size_limit(monkeypatch):
    """解压总大小超限拒绝(防解压炸弹)"""
    monkeypatch.setattr(settings, "UPLOAD_MAX_EXTRACT_MB", 0)
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILE_MB", 1)
    data = _make_zip({"a.bin": b"a" * 1024, "b.bin": b"b" * 1024})
    with pytest.raises(UploadError, match="总大小超过上限"):
        save_upload(data, "bomb.zip", "u1")


def test_zip_entry_count_limit(monkeypatch):
    """条目数超限拒绝"""
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILES", 1)
    data = _make_zip({"a.txt": b"a", "b.txt": b"b"})
    with pytest.raises(UploadError, match="文件数超过上限"):
        save_upload(data, "many.zip", "u1")


def test_upload_body_size_limit(monkeypatch):
    """上传本体超过 UPLOAD_MAX_FILE_MB 拒绝"""
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILE_MB", 0)
    with pytest.raises(UploadError, match="大小上限"):
        save_upload(b"x" * 1024, "big.bin", "u1")


# ============================================================
# 引用校验(任务创建时)
# ============================================================

def test_validate_upload_owner_ok():
    """上传者本人可引用"""
    meta = save_upload(b"x", "a.txt", "u1")
    m = validate_upload_for_task(meta["upload_id"], "u1")
    assert m["upload_id"] == meta["upload_id"]


def test_validate_upload_rejects_other_user():
    """他人上传不可引用"""
    meta = save_upload(b"x", "a.txt", "u1")
    with pytest.raises(UploadError, match="无权引用"):
        validate_upload_for_task(meta["upload_id"], "u2")


def test_validate_upload_rejects_anonymous():
    """未登录不可引用"""
    meta = save_upload(b"x", "a.txt", "u1")
    with pytest.raises(UploadError, match="登录"):
        validate_upload_for_task(meta["upload_id"], None)


def test_validate_upload_not_found():
    """不存在的 upload_id 拒绝"""
    with pytest.raises(UploadError, match="不存在"):
        validate_upload_for_task("20990101000000-notexist", "u1")


def test_upload_id_injection_rejected():
    """非法字符的 upload_id(路径拼接注入)拒绝"""
    with pytest.raises(UploadError, match="不合法"):
        with materialize_upload_files("../uploads_data"):
            pass
    with pytest.raises(UploadError, match="不合法"):
        with materialize_upload_files("a/b"):
            pass


def test_materialize_local_keeps_stage1_after_exit():
    """local 后端:materialize 退出后 Stage 1 真源仍在(不能被误删)"""
    meta = save_upload(b"keep me", "a.txt", "u1")
    upload_id = meta["upload_id"]
    with materialize_upload_files(upload_id) as files:
        assert (files / "a.txt").read_bytes() == b"keep me"
    # 退出 with 后再次进入仍可读到同一真源(未被删除)
    with materialize_upload_files(upload_id) as files2:
        assert (files2 / "a.txt").read_bytes() == b"keep me"
    assert load_upload_meta(upload_id)["upload_id"] == upload_id


# ============================================================
# 工作区回退浏览:local 后端 list_files / stat_file / read_file
# ============================================================


def test_local_list_files_tree_and_noise_pruned():
    """list_files 返回含中间目录的树条目,噪声目录剪枝(与沙箱树同口径)"""
    data = _make_zip({
        "README.md": "hi",
        "src/main.py": "print(1)",
        "src/util/helper.py": "x",
        ".git/config": "[core]",
        "node_modules/pkg/index.js": "junk",
    })
    meta = save_upload(data, "project.zip", "u1")
    entries = get_backend().list_files(meta["upload_id"])
    paths = {e["path"]: e["type"] for e in entries}
    assert paths["README.md"] == "file"
    assert paths["src"] == "dir"
    assert paths["src/main.py"] == "file"
    assert paths["src/util"] == "dir"
    assert paths["src/util/helper.py"] == "file"
    # .git / node_modules 剪枝
    assert not any(
        ".git" in p or "node_modules" in p for p in paths
    ), paths


def test_local_list_files_missing_upload():
    """不存在的上传 list_files 抛 UploadError(回退树按已清理占位)"""
    with pytest.raises(UploadError, match="不存在"):
        get_backend().list_files("20990101000000-notexist")


def test_local_stat_and_read_file_roundtrip():
    """stat_file / read_file 按相对路径读写,子目录路径正常"""
    data = _make_zip({"src/main.py": "print(1)"})
    m1 = save_upload(b"hello", "a.txt", "u1")
    m2 = save_upload(data, "p.zip", "u1")
    backend = get_backend()
    assert backend.stat_file(m1["upload_id"], "a.txt") == 5
    assert backend.read_file(m1["upload_id"], "a.txt") == b"hello"
    assert backend.read_file(m2["upload_id"], "src/main.py") == b"print(1)"
    assert backend.stat_file(m2["upload_id"], "src/main.py") == 8
    # 反斜杠分隔符归一为 posix
    assert backend.read_file(m2["upload_id"], "src\\main.py") == b"print(1)"


def test_local_read_file_missing_or_dir():
    """文件不存在 / 路径是目录:抛 UploadError(端点转 404)"""
    data = _make_zip({"src/main.py": "x"})
    meta = save_upload(data, "p.zip", "u1")
    backend = get_backend()
    with pytest.raises(UploadError, match="文件不存在"):
        backend.read_file(meta["upload_id"], "b.txt")
    with pytest.raises(UploadError, match="文件不存在"):
        backend.read_file(meta["upload_id"], "src")  # 目录不可读


def test_local_read_file_traversal_rejected():
    """.. 穿越与空路径拒绝(防读 files/ 外的 meta.json 等)"""
    meta = save_upload(b"x", "a.txt", "u1")
    backend = get_backend()
    for bad in ("../meta.json", "a/../../b.txt", ""):
        with pytest.raises(UploadError, match="不合法"):
            backend.read_file(meta["upload_id"], bad)
        with pytest.raises(UploadError, match="不合法"):
            backend.stat_file(meta["upload_id"], bad)
