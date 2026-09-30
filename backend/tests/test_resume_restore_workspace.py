"""orchestrator._restore_workspace_if_needed 单测(resume/重试共用的工作区恢复助手)。

核心约束:
- 任务未配 repo_url → 不恢复(False),不调 _prepare_repo_context
- 沙箱会话存活 → 不恢复(False),不调 _prepare_repo_context
- 会话已被回收 + 配了 repo_url → 重新 _prepare_repo_context(True),更新 stage
"""
from unittest.mock import MagicMock

import app.agents.orchestrator as orchestrator
from app.agents.orchestrator import _restore_workspace_if_needed


def _mk_task(repo_url="https://github.com/a/b"):
    task = MagicMock()
    task.id = "task-1"
    task.params = {"repo_url": repo_url}
    task.current_stage = ""
    return task


def _patch_env(monkeypatch, session_alive=True):
    """屏蔽发布/克隆副作用,只测分流逻辑(与 test_retry_failed_task 一致)。"""
    monkeypatch.setattr(orchestrator, "_publish_status", lambda _task: None)
    monkeypatch.setattr(
        orchestrator, "_prepare_repo_context",
        MagicMock(return_value=(None, "")),
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "get_workspace_info",
        lambda _tid: ({"repo_path": "/r", "mode": "local"} if session_alive else None),
    )


def test_no_repo_url_skips(monkeypatch):
    """任务未配仓库 → 无需恢复,即使会话已被回收。"""
    _patch_env(monkeypatch, session_alive=False)
    task = _mk_task(repo_url=None)
    task.params = {}
    db = MagicMock()

    restored = _restore_workspace_if_needed(task, db, "task-1", {})

    assert restored is False
    orchestrator._prepare_repo_context.assert_not_called()


def test_session_alive_skips(monkeypatch):
    """会话存活 → 不重复克隆(幂等,重试链路先克隆后 resume 也不会双重 clone)。"""
    _patch_env(monkeypatch, session_alive=True)
    task = _mk_task()
    db = MagicMock()

    restored = _restore_workspace_if_needed(task, db, "task-1", {})

    assert restored is False
    orchestrator._prepare_repo_context.assert_not_called()


def test_session_gone_reprepares(monkeypatch):
    """会话被回收 + 配了仓库 → 重新克隆恢复,更新 stage 并推送状态。"""
    _patch_env(monkeypatch, session_alive=False)
    task = _mk_task()
    db = MagicMock()
    git_tokens = {"github": "tok"}

    restored = _restore_workspace_if_needed(task, db, "task-1", git_tokens)

    assert restored is True
    orchestrator._prepare_repo_context.assert_called_once_with(
        task, db, "task-1", git_tokens,
    )
    assert "重新克隆" in task.current_stage
    db.commit.assert_called()


def test_params_none_treated_as_no_repo(monkeypatch):
    """task.params 为 None 时安全跳过(不抛 AttributeError)。"""
    _patch_env(monkeypatch, session_alive=False)
    task = _mk_task()
    task.params = None
    db = MagicMock()

    restored = _restore_workspace_if_needed(task, db, "task-1", {})

    assert restored is False
    orchestrator._prepare_repo_context.assert_not_called()
