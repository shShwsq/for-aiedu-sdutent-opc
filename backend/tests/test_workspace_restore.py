"""POST /tasks/{task_id}/workspace/restore 端点测试

直接调用路由函数(mock db + sandbox_tools),覆盖:
- 权限路径:任务不存在 404 / 他人任务 403 / 匿名任务可访问
- session 存活 → 幂等直返(不重复 clone)
- 任务无 repo_url → 400
- 成功路径:clone 参数正确 + mark_task_completed 纳入 TTL 清理序列
- clone 失败 → 500
"""
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import app.routers.workspace as ws_router
import app.services.practice.generator as gen


def _task(user_id="u1", params=None):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.user_id = user_id
    t.params = (
        params if params is not None
        else {"repo_url": "https://example.com/r.git", "branch": "dev"}
    )
    return t


def _db(task):
    db = MagicMock()
    db.get.side_effect = lambda model, tid: task
    return db


def _user(uid="u1"):
    u = MagicMock()
    u.id = uid
    return u


# ============================================================
# 权限路径
# ============================================================


def test_restore_task_not_found():
    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(HTTPException) as ei:
        ws_router.restore_workspace(uuid.uuid4(), db, None)
    assert ei.value.status_code == 404


def test_restore_other_user_task_forbidden():
    task = _task(user_id="owner")
    with pytest.raises(HTTPException) as ei:
        ws_router.restore_workspace(task.id, _db(task), _user("intruder"))
    assert ei.value.status_code == 403


def test_restore_anonymous_task_allowed(monkeypatch):
    """匿名任务(user_id 为空)未登录也可恢复"""
    task = _task(user_id=None)
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo", "mode": "sandbox"},
    )
    res = ws_router.restore_workspace(task.id, _db(task), None)
    assert res["available"] is True


# ============================================================
# 业务路径
# ============================================================


def test_restore_alive_session_idempotent(monkeypatch):
    """session 仍存活:直接返回当前工作区信息,不重复 clone"""
    task = _task()
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo", "mode": "sandbox"},
    )
    clone_calls = []
    monkeypatch.setattr(
        ws_router.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **k: clone_calls.append(1),
    )
    res = ws_router.restore_workspace(task.id, _db(task), _user())
    assert res == {"available": True, "repo_path": "/repo", "mode": "sandbox"}
    assert not clone_calls


def test_restore_no_repo_url_400(monkeypatch):
    task = _task(params={})
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info", lambda tid: None,
    )
    with pytest.raises(HTTPException) as ei:
        ws_router.restore_workspace(task.id, _db(task), _user())
    assert ei.value.status_code == 400


def test_restore_success_clones_and_marks(monkeypatch):
    """成功路径:clone 参数正确 + 标记 completed 纳入 TTL 清理序列"""
    task = _task()
    state = {"info": None}
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info", lambda tid: state["info"],
    )
    clone_calls = []

    def fake_clone(repo_url, branch=None, task_id="", git_tokens=None, **kw):
        clone_calls.append((repo_url, branch, task_id, git_tokens))
        state["info"] = {"repo_path": "/repo-restored", "mode": "sandbox"}

    monkeypatch.setattr(
        ws_router.sandbox_tools, "clone_repo_with_fallback", fake_clone,
    )
    marked = []
    monkeypatch.setattr(
        ws_router.sandbox_tools, "mark_task_completed", lambda tid: marked.append(tid),
    )
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {"github": "tk"})

    res = ws_router.restore_workspace(task.id, _db(task), _user())
    assert res["available"] is True
    assert res["repo_path"] == "/repo-restored"
    assert clone_calls == [
        ("https://example.com/r.git", "dev", str(task.id), {"github": "tk"}),
    ]
    assert marked == [str(task.id)]


def test_restore_clone_failure_500(monkeypatch):
    task = _task()
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info", lambda tid: None,
    )

    def _raise(*a, **k):
        raise RuntimeError("network error")

    monkeypatch.setattr(
        ws_router.sandbox_tools, "clone_repo_with_fallback", _raise,
    )
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {})
    with pytest.raises(HTTPException) as ei:
        ws_router.restore_workspace(task.id, _db(task), _user())
    assert ei.value.status_code == 500
