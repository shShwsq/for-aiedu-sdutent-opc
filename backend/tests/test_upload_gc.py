"""交付物保留 / GC 单测(mock db + 假后端,不触真实 DB/存储)

覆盖 run_gc_once 的删除判据:
- 被任务引用:仅当关联任务全部终态且最近终态时间超过保留窗口才删
- 孤儿(无任务引用):meta.created_at 超过保留窗口才删
- 进行中任务 / 未超期(终态或孤儿)一律保留
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.models.task import TaskStatus
from app.services import upload_gc
from app.services.upload_storage import UploadError

_NOW = datetime.now(timezone.utc)


class FakeBackend:
    """假存储后端:固定 upload_id 列表 + meta,记录 delete 调用"""

    def __init__(self, ids, metas):
        self._ids = list(ids)
        self._metas = metas
        self.deleted: list[str] = []

    def list_upload_ids(self):
        return list(self._ids)

    def load_meta(self, uid):
        if uid not in self._metas:
            raise UploadError(f"上传不存在或已被清理: {uid}")
        return self._metas[uid]

    def delete(self, uid):
        self.deleted.append(uid)


def _fake_db(rows):
    db = MagicMock()
    db.execute.return_value.all.return_value = rows
    return db


@pytest.fixture
def _retention(monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_RETENTION_DAYS", 30)


def test_gc_deletes_only_expired_terminal_and_orphan(_retention, monkeypatch):
    ids = ["t-old", "t-recent", "t-run", "orphan-old", "orphan-recent"]
    metas = {
        "orphan-old": {"created_at": (_NOW - timedelta(days=40)).isoformat()},
        "orphan-recent": {"created_at": _NOW.isoformat()},
    }
    backend = FakeBackend(ids, metas)
    monkeypatch.setattr(upload_gc, "get_backend", lambda: backend)

    rows = [
        # 终态且超期(completed_at 40 天前)→ 删
        ({"upload_id": "t-old"}, TaskStatus.COMPLETED,
         _NOW - timedelta(days=40), _NOW - timedelta(days=45)),
        # 终态但未超期(5 天前)→ 留
        ({"upload_id": "t-recent"}, TaskStatus.COMPLETED,
         _NOW - timedelta(days=5), _NOW - timedelta(days=6)),
        # 进行中(未终态)→ 留
        ({"upload_id": "t-run"}, TaskStatus.RUNNING,
         None, _NOW - timedelta(days=1)),
    ]
    stats = upload_gc.run_gc_once(db=_fake_db(rows))

    assert set(backend.deleted) == {"t-old", "orphan-old"}
    assert stats["deleted"] == 2
    assert stats["kept"] == 3
    assert stats["scanned"] == 5
    assert stats["errors"] == 0


def test_gc_failed_terminal_counts_as_terminal(_retention, monkeypatch):
    """failed 也是终态:超期即删"""
    backend = FakeBackend(["f-old"], {})
    monkeypatch.setattr(upload_gc, "get_backend", lambda: backend)
    rows = [
        ({"upload_id": "f-old"}, TaskStatus.FAILED,
         _NOW - timedelta(days=31), _NOW - timedelta(days=32)),
    ]
    upload_gc.run_gc_once(db=_fake_db(rows))
    assert backend.deleted == ["f-old"]


def test_gc_keeps_when_any_referencing_task_active(_retention, monkeypatch):
    """同一 upload 被多任务引用:只要有一个未终态就保留(保守)"""
    backend = FakeBackend(["shared"], {})
    monkeypatch.setattr(upload_gc, "get_backend", lambda: backend)
    rows = [
        ({"upload_id": "shared"}, TaskStatus.COMPLETED,
         _NOW - timedelta(days=40), _NOW - timedelta(days=41)),
        ({"upload_id": "shared"}, TaskStatus.RUNNING,
         None, _NOW - timedelta(days=1)),
    ]
    upload_gc.run_gc_once(db=_fake_db(rows))
    assert backend.deleted == []


def test_gc_orphan_without_created_at_kept(_retention, monkeypatch):
    """孤儿但 meta 缺 created_at:无法判超期,保守保留"""
    backend = FakeBackend(["orphan-nodate"], {"orphan-nodate": {}})
    monkeypatch.setattr(upload_gc, "get_backend", lambda: backend)
    stats = upload_gc.run_gc_once(db=_fake_db([]))
    assert backend.deleted == []
    assert stats["kept"] == 1
