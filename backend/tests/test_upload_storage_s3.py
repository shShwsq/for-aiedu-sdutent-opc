"""S3 存储后端单测(用内存假 boto3 客户端,免 moto 依赖)

验证 S3UploadStorage 的对象 key 组装与往返:persist 上传、load_meta 回读、
materialize_files 下载到临时目录并退出清理、list_upload_ids / delete / exists。
通过 monkeypatch 把 backend._client() 换成 FakeS3Client,不触真实网络。
"""
import io
import os

import pytest

from app.config import settings
from app.services import upload_storage
from app.services.uploads import (
    load_upload_meta,
    materialize_upload_files,
    save_upload,
)


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
        return {}


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
    from app.services.uploads import UploadError

    with pytest.raises(UploadError, match="不存在"):
        load_upload_meta("20990101000000-notexist")
