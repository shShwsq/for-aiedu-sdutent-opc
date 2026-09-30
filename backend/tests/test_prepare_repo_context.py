"""orchestrator._prepare_repo_context 单测(纯函数级,mock 沙箱/DB)。

核心约束:当且仅当预克隆成功且仓库非空,才返回非空 repo_context
("[仓库已预先 clone...]"提示段的来源);空仓库 / clone 失败 /
list_files 失败都不得注入该段,避免误导 agent 在空仓库上直接开始审计。
"""
from unittest.mock import MagicMock

import app.agents.orchestrator as orchestrator
from app.agents.orchestrator import _prepare_repo_context


def _mk_task(repo_url="https://github.com/a/b"):
    task = MagicMock()
    task.id = "task-1"
    task.user_id = None
    task.params = {"repo_url": repo_url, "branch": "main"}
    return task


def _patch_env(monkeypatch):
    """屏蔽 DB / 事件总线 / 沙箱副作用,只测 repo_context 生成逻辑。"""
    monkeypatch.setattr(orchestrator, "_publish_status", lambda _task: None)
    monkeypatch.setattr(
        orchestrator, "_write_memory_files_for_task", lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        orchestrator, "_add_conversation", lambda *a, **kw: None,
    )


def test_nonempty_repo_returns_context(monkeypatch):
    """clone 成功 + 根目录有条目 → 返回非空 repo_context(含路径与结构)。"""
    _patch_env(monkeypatch)
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **kw: {"path": "/home/user/repos/b", "files_count": 2},
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "list_files",
        lambda *a, **kw: {
            "entries": [
                {"name": "src", "type": "dir"},
                {"name": "README.md", "type": "file", "size": 10},
            ],
            "total": 2, "truncated": False,
        },
    )
    repo_path, repo_context = _prepare_repo_context(
        _mk_task(), MagicMock(), "task-1",
    )
    assert repo_path == "/home/user/repos/b"
    assert "已克隆到 /home/user/repos/b" in repo_context
    assert "[目录] src" in repo_context
    assert "[文件] README.md" in repo_context


def test_empty_repo_returns_no_context(monkeypatch):
    """clone 成功但根目录为空 → repo_context 为空(不注入"已预先 clone"段)。"""
    _patch_env(monkeypatch)
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **kw: {"path": "/home/user/repos/b", "files_count": 0},
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "list_files",
        lambda *a, **kw: {"entries": [], "total": 0, "truncated": False},
    )
    repo_path, repo_context = _prepare_repo_context(
        _mk_task(), MagicMock(), "task-1",
    )
    # clone 成功:repo_path 仍返回(会话可复用);但不注入上下文
    assert repo_path == "/home/user/repos/b"
    assert repo_context == ""


def test_list_files_failure_returns_no_context(monkeypatch):
    """clone 成功但 list_files 失败(无法确认非空) → repo_context 为空。"""
    _patch_env(monkeypatch)
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **kw: {"path": "/home/user/repos/b", "files_count": 5},
    )

    def _raise(*a, **kw):
        raise RuntimeError("sandbox gone")

    monkeypatch.setattr(orchestrator.sandbox_tools, "list_files", _raise)
    repo_path, repo_context = _prepare_repo_context(
        _mk_task(), MagicMock(), "task-1",
    )
    assert repo_path == "/home/user/repos/b"
    assert repo_context == ""


def test_clone_failure_returns_no_path_no_context(monkeypatch):
    """clone 失败 → (None, ""),降级为 react_agent 自主 clone。"""
    _patch_env(monkeypatch)

    def _raise(*a, **kw):
        raise RuntimeError("network error")

    monkeypatch.setattr(
        orchestrator.sandbox_tools, "clone_repo_with_fallback", _raise,
    )
    repo_path, repo_context = _prepare_repo_context(
        _mk_task(), MagicMock(), "task-1",
    )
    assert repo_path is None
    assert repo_context == ""


def test_no_repo_url_returns_none(monkeypatch):
    """未选仓库 → (None, ""),走 react_agent 自主 clone 原流程。"""
    _patch_env(monkeypatch)
    task = _mk_task()
    task.params = {}
    repo_path, repo_context = _prepare_repo_context(task, MagicMock(), "task-1")
    assert repo_path is None
    assert repo_context == ""
