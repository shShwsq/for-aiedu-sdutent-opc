"""workspace_diff 仓库树快照单测:capture_repo_tree + save_repo_tree_artifact。

核心约束:
- git ls-files 输出逐行解析,超 _MAX_TREE_ENTRIES 截断 + truncated 标记
- session/repo_path 缺失、空仓库、git 异常 → None(调用方跳过写入)
- save 捕获为 None 时不删已有记录(保住 clone 时的保底快照);
  有新结果时先删旧再写新(task 维度唯一)
"""
from unittest.mock import MagicMock

import app.tools.sandbox_tools as sandbox_tools
from app.models.task import Task  # noqa: F401 导入以注册 ORM mapper(TaskArtifact 有关系指向它)
from app.models.task_artifact import TaskArtifact
from app.services.workspace_diff import (
    _MAX_TREE_ENTRIES,
    capture_repo_tree,
    save_repo_tree_artifact,
)


class FakeSession:
    """记录命令、预设输出/异常的沙箱会话桩。"""

    def __init__(self, output: str = "", error: Exception | None = None):
        self.output = output
        self.error = error
        self.commands: list[str] = []

    def run_command(self, cmd: str, timeout: int | None = None) -> str:
        self.commands.append(cmd)
        if self.error:
            raise self.error
        return self.output


def _mk_task():
    task = MagicMock()
    task.id = "task-1"
    return task


def _mount_session(monkeypatch, session, repo_path="/repo"):
    """把 FakeSession 挂到 sandbox_tools._sessions(monkeypatch 自动还原)。"""
    monkeypatch.setitem(
        sandbox_tools._sessions,
        "task-1",
        {"session": session, "repo_path": repo_path},
    )


# ============================================================
# capture_repo_tree
# ============================================================


def test_capture_basic(monkeypatch):
    """正常输出:逐行解析,content 拼接,file_count 正确,命令用 ls-files -co。"""
    session = FakeSession(output="README.md\nsrc/app.py\nsrc/util.py\n")
    _mount_session(monkeypatch, session)

    result = capture_repo_tree("task-1")

    assert result is not None
    assert result["content"] == "README.md\nsrc/app.py\nsrc/util.py"
    assert result["metadata"] == {"file_count": 3, "truncated": False}
    assert len(session.commands) == 1
    assert "git ls-files -co --exclude-standard" in session.commands[0]


def test_capture_truncates_over_limit(monkeypatch):
    """超过 _MAX_TREE_ENTRIES 条 → 截断 + truncated=True,file_count 为截断后条数。"""
    lines = "\n".join(f"file_{i}.py" for i in range(_MAX_TREE_ENTRIES + 2))
    _mount_session(monkeypatch, FakeSession(output=lines))

    result = capture_repo_tree("task-1")

    assert result is not None
    assert result["metadata"]["truncated"] is True
    assert result["metadata"]["file_count"] == _MAX_TREE_ENTRIES
    assert len(result["content"].splitlines()) == _MAX_TREE_ENTRIES


def test_capture_no_session(monkeypatch):
    """会话不存在(已回收)→ None。"""
    monkeypatch.delitem(sandbox_tools._sessions, "task-1", raising=False)
    assert capture_repo_tree("task-1") is None


def test_capture_no_repo_path(monkeypatch):
    """session 存在但未 clone(repo_path 为空)→ None。"""
    _mount_session(monkeypatch, FakeSession(output="a.py\n"), repo_path="")
    assert capture_repo_tree("task-1") is None


def test_capture_git_error(monkeypatch):
    """git 命令抛异常 → None(不向上传播)。"""
    _mount_session(monkeypatch, FakeSession(error=RuntimeError("git broken")))
    assert capture_repo_tree("task-1") is None


def test_capture_empty_repo(monkeypatch):
    """空仓库(输出全空行)→ None。"""
    _mount_session(monkeypatch, FakeSession(output="\n  \n"))
    assert capture_repo_tree("task-1") is None


# ============================================================
# save_repo_tree_artifact
# ============================================================


def test_save_none_capture_keeps_existing(monkeypatch):
    """捕获为 None → 静默返回,不删已有记录(保住 clone 时的保底快照)。"""
    monkeypatch.delitem(sandbox_tools._sessions, "task-1", raising=False)
    db = MagicMock()

    save_repo_tree_artifact(_mk_task(), db, "task-1")

    db.query.assert_not_called()
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_save_replaces_old_record(monkeypatch):
    """有新结果 → 先删旧 repo_tree 记录再写新,断言写入内容与元信息。"""
    _mount_session(monkeypatch, FakeSession(output="a.py\nb.py\n"))
    db = MagicMock()

    save_repo_tree_artifact(_mk_task(), db, "task-1")

    # 删旧:按 task_id + kind="repo_tree" 过滤
    db.query.assert_called_once_with(TaskArtifact)
    filters = db.query.return_value.filter.call_args[0]
    assert len(filters) == 2
    db.query.return_value.filter.return_value.delete.assert_called_once()
    # 写新:kind/content/metadata 正确
    artifact = db.add.call_args[0][0]
    assert isinstance(artifact, TaskArtifact)
    assert artifact.kind == "repo_tree"
    assert artifact.content == "a.py\nb.py"
    assert artifact.metadata_ == {"file_count": 2, "truncated": False}
    db.commit.assert_called_once()


def test_max_entries_constant_reasonable():
    """上限常量回归:防止误改成过小值导致清单失去意义。"""
    assert _MAX_TREE_ENTRIES >= 1000
