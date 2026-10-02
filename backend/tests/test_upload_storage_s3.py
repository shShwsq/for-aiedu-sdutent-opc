"""S3 存储后端单测(用内存假 boto3 客户端,免 moto 依赖)

验证 S3UploadStorage 的对象 key 组装与往返:persist 上传、load_meta 回读、
materialize_files 下载到临时目录并退出清理、list_upload_ids / delete / exists,
以及工作区回退浏览的 list_files / stat_file / read_file(树合成、噪声剪枝、
路径穿越拒绝、404 转 UploadError)。
通过 monkeypatch 把 backend._client() 换成 FakeS3Client,不触真实网络。
"""
import io
import os
import zipfile

import pytest

from app.config import settings
from app.services import upload_storage
from app.services.uploads import (
    UploadError,
    load_upload_meta,
    materialize_upload_files,
    save_upload,
)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """按 {路径: 内容} 构造 zip 字节"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class FakeNotFound(Exception):
    """模拟 boto3 404/NoSuchKey(带 response 结构,供 _is_not_found 识别)"""

    def __init__(self):
        super().__init__("404 Not Found")
        self.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }


class FakeS3Client:
    """内存版 S3 客户端,仅实现 S3UploadStorage 用到的方法"""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def upload_file(self, path, bucket, key):
        with open(path, "rb") as f:
            self.store[key] = f.read()

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise FakeNotFound()
        return {"Body": io.BytesIO(self.store[Key])}

    def download_file(self, bucket, key, path):
        if key not in self.store:
            raise FakeNotFound()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(self.store[key])

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, ContinuationToken=None):
        if Delimiter == "/":
            prefixes = set()
            for k in self.store:
                if k.startswith(Prefix):
                    rest = k[len(Prefix):]
                    if "/" in rest:
                        prefixes.add(Prefix + rest.split("/")[0] + "/")
            return {
                "CommonPrefixes": [{"Prefix": p} for p in sorted(prefixes)],
                "IsTruncated": False,
            }
        contents = [{"Key": k} for k in sorted(self.store) if k.startswith(Prefix)]
        return {"Contents": contents, "IsTruncated": False}

    def delete_objects(self, Bucket, Delete, Quiet=True):
        for o in Delete["Objects"]:
            self.store.pop(o["Key"], None)

    def head_object(self, Bucket, Key):
        if Key not in self.store:
            raise FakeNotFound()
        return {"ContentLength": len(self.store[Key])}


@pytest.fixture
def s3_backend(monkeypatch):
    """切到 s3 后端并把客户端替换为内存假实现"""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "S3_PREFIX", "uploads/")
    upload_storage.reset_backend_cache()
    fake = FakeS3Client()
    backend = upload_storage.get_backend()
    monkeypatch.setattr(backend, "_client", lambda: fake)
    yield backend, fake
    upload_storage.reset_backend_cache()


def test_s3_save_uploads_tree_and_meta(s3_backend):
    """save_upload 把 files 树 + meta.json 上传为对象,key 前缀正确"""
    backend, fake = s3_backend
    meta = save_upload(b"hello world", "note.txt", "u1")
    uid = meta["upload_id"]
    assert f"uploads/{uid}/files/note.txt" in fake.store
    assert f"uploads/{uid}/meta.json" in fake.store
    assert fake.store[f"uploads/{uid}/files/note.txt"] == b"hello world"
    assert backend.exists(uid) is True


def test_s3_load_meta_roundtrip(s3_backend):
    """load_upload_meta 从对象存储回读元数据"""
    meta = save_upload(b"x", "a.txt", "u1")
    m = load_upload_meta(meta["upload_id"])
    assert m["upload_id"] == meta["upload_id"]
    assert m["filename"] == "a.txt"
    assert m["user_id"] == "u1"


def test_s3_materialize_downloads_and_cleans_temp(s3_backend):
    """materialize 下载到临时目录可读,退出后临时目录被清理"""
    meta = save_upload(b"content", "f.txt", "u1")
    uid = meta["upload_id"]
    with materialize_upload_files(uid) as files:
        assert (files / "f.txt").read_bytes() == b"content"
        temp_path = files
    # 退出 with 后临时目录已删(s3 后端不保留本地副本)
    assert not temp_path.exists()


def test_s3_list_and_delete(s3_backend):
    """list_upload_ids 列出 id;delete 清空该 upload 全部对象"""
    backend, fake = s3_backend
    meta = save_upload(b"x", "a.txt", "u1")
    uid = meta["upload_id"]
    assert uid in backend.list_upload_ids()
    backend.delete(uid)
    assert not any(k.startswith(f"uploads/{uid}/") for k in fake.store)
    assert backend.exists(uid) is False


def test_s3_load_meta_missing_raises(s3_backend):
    """不存在的 upload_id 回读 meta 抛 UploadError(经门面)"""
    with pytest.raises(UploadError, match="不存在"):
        load_upload_meta("20990101000000-notexist")


# ============================================================
# 工作区回退浏览:list_files / stat_file / read_file
# ============================================================


def test_s3_list_files_tree_and_noise_pruned(s3_backend):
    """list_files 合成中间目录条目并剪枝噪声目录(与 local 口径一致)"""
    backend, fake = s3_backend
    data = _make_zip({
        "README.md": "hi",
        "src/main.py": "print(1)",
        ".git/config": "[core]",
        "node_modules/pkg/index.js": "junk",
    })
    meta = save_upload(data, "project.zip", "u1")
    entries = backend.list_files(meta["upload_id"])
    paths = {e["path"]: e["type"] for e in entries}
    assert paths == {
        "README.md": "file",
        "src": "dir",
        "src/main.py": "file",
    }


def test_s3_list_files_missing_raises(s3_backend):
    """无任何对象的前缀(上传不存在/被 GC)抛 UploadError"""
    backend, fake = s3_backend
    with pytest.raises(UploadError, match="不存在"):
        backend.list_files("20990101000000-notexist")


def test_s3_stat_and_read_file_roundtrip(s3_backend):
    """stat_file 返回对象大小;read_file 按 key 读取内容"""
    backend, fake = s3_backend
    data = _make_zip({"src/main.py": "print(1)"})
    meta = save_upload(data, "p.zip", "u1")
    uid = meta["upload_id"]
    assert backend.stat_file(uid, "src/main.py") == 8
    assert backend.read_file(uid, "src/main.py") == b"print(1)"
    # key 组装正确(带 S3_PREFIX)
    assert fake.store[f"uploads/{uid}/files/src/main.py"] == b"print(1)"


def test_s3_read_file_missing_raises(s3_backend):
    """对象不存在(head/get 404)转 UploadError 文件不存在"""
    backend, fake = s3_backend
    meta = save_upload(b"x", "a.txt", "u1")
    uid = meta["upload_id"]
    with pytest.raises(UploadError, match="文件不存在"):
        backend.read_file(uid, "b.txt")
    with pytest.raises(UploadError, match="文件不存在"):
        backend.stat_file(uid, "b.txt")


def test_s3_read_file_traversal_rejected(s3_backend):
    """.. 穿越与空路径在 key 组装层拒绝(防读 files/ 外对象)"""
    backend, fake = s3_backend
    meta = save_upload(b"x", "a.txt", "u1")
    uid = meta["upload_id"]
    for bad in ("../meta.json", "a/../../b.txt", ""):
        with pytest.raises(UploadError, match="不合法"):
            backend.read_file(uid, bad)
        with pytest.raises(UploadError, match="不合法"):
            backend.stat_file(uid, bad)
