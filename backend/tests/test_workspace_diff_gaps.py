"""workspace_diff 缺口补齐 单元测试:B1 清理前兜底 / B2 孤儿目录恢复 / B3 resume 补回。

- B1:close_session(save_diff=True) 在 pop 之前触发兜底;条件不满足/默认不触发
- B2:capture_local_diff_from_dir(真实 git)与 recover_orphan_local_workspaces
  (monkeypatch tempfile.gettempdir + SessionLocal,真实 git 抢救 + rmtree)
- B3:reapply_workspace_diff(真实 git apply 回写)
"""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import app.services.workspace_diff as wd
import app.tools.sandbox_tools as st
from app.models.task import Task
from app.models.task_artifact import TaskArtifact

# SQLAlchemy 首次 ORM 实例化会初始化全部 mapper:导入全部模型模块,
# 否则 User→UserGitBinding 等字符串关系解析失败(InvalidRequestError)
from app.models import (  # noqa: F401
    agent_policy,
    domain_event_log,
    email_token,
    practice,
    project,
    user,
    user_agent_config,
    user_git_binding,
    user_llm_config,
)


# ============================================================
# 辅助
# ============================================================


def _git(cwd, *argv, check=True):
    proc = subprocess.run(
        ["git", "-C", str(cwd), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {argv} 失败: {proc.stderr}")
    return proc.stdout


def _make_repo(path: Path) -> Path:
    """真实仓库:提交 a.txt 后再修改它 + 新增未跟踪 b.txt(两类 diff 都有)"""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "tester")
    (path / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    (path / "a.txt").write_text("hello changed\n", encoding="utf-8")
    (path / "b.txt").write_text("new file\n", encoding="utf-8")
    return path


class _FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _mk_db(task_found=True, artifact_first=None, artifact_delete_log=None):
    """构造 MagicMock db:db.query(Task) / db.query(TaskArtifact) 分别返回可控 mock"""
    db = MagicMock()
    task_q = MagicMock()
    task_q.filter.return_value.first.return_value = (
        MagicMock(id=uuid.uuid4()) if task_found else None
    )
    art_q = MagicMock()
    art_q.filter.return_value.first.return_value = artifact_first
    if artifact_delete_log is not None:
        art_q.filter.return_value.delete.side_effect = (
            lambda *a, **kw: artifact_delete_log.append(1)
        )
    db.query.side_effect = lambda model: task_q if model is Task else art_q
    return db


# ============================================================
# B1:close_session 清理前兜底
# ============================================================


def test_close_session_backstop_triggers_before_pop(monkeypatch):
    """save_diff=True 且 completed_at/repo_path 存在 → 兜底在 pop 之前被调用。"""
    task_id = uuid.uuid4().hex
    session = _FakeSession()
    st._sessions[task_id] = {
        "session": session, "repo_path": "/repo", "mode": "local",
        "completed_at": time.time(),
    }
    calls: list[str] = []
    monkeypatch.setattr(
        wd, "save_diff_best_effort_on_close", lambda tid: calls.append(tid),
    )
    st.close_session(task_id, save_diff=True)
    assert calls == [task_id]
    assert task_id not in st._sessions
    assert session.closed


def test_close_session_backstop_conditions(monkeypatch):
    """未完成(无 completed_at)或 save_diff=False(删除任务路径)都不触发兜底。"""
    calls: list[str] = []
    monkeypatch.setattr(
        wd, "save_diff_best_effort_on_close",
        lambda tid: calls.append(tid),
    )

    # 1) 未完成:即使 save_diff=True 也不触发
    tid1 = uuid.uuid4().hex
    st._sessions[tid1] = {
        "session": _FakeSession(), "repo_path": "/repo", "mode": "local",
    }
    st.close_session(tid1, save_diff=True)
    assert tid1 not in st._sessions
    assert calls == []

    # 2) 已完成但 save_diff=False(默认,删除任务路径)
    tid2 = uuid.uuid4().hex
    st._sessions[tid2] = {
        "session": _FakeSession(), "repo_path": "/repo", "mode": "local",
        "completed_at": time.time(),
    }
    st.close_session(tid2)
    assert tid2 not in st._sessions
    assert calls == []


def test_best_effort_save_skips_when_artifact_exists(monkeypatch):
    """已有 git_diff artifact(完成段已保存)→ 不重复捕获。"""
    import app.database as database

    task_uuid = uuid.uuid4()
    db = _mk_db(task_found=True, artifact_first=MagicMock())
    monkeypatch.setattr(database, "SessionLocal", lambda: db)

    capture_calls: list[str] = []
    monkeypatch.setattr(
        wd, "capture_workspace_diff",
        lambda tid: capture_calls.append(tid) or None,
    )
    wd.save_diff_best_effort_on_close(str(task_uuid))
    assert capture_calls == []  # 已有快照,未捕获
    db.add.assert_not_called()


def test_best_effort_save_writes_artifact_when_missing(monkeypatch):
    """无 artifact + 会话有修改 → 先捕获再落库(kind=git_diff)。"""
    import app.database as database

    task_uuid = uuid.uuid4()
    db = _mk_db(task_found=True, artifact_first=None)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        wd, "capture_workspace_diff",
        lambda tid: {"content": "diff --git a/x b/x", "metadata": {"files_changed": 1}},
    )
    wd.save_diff_best_effort_on_close(str(task_uuid))
    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, TaskArtifact)
    assert added.kind == "git_diff"
    assert added.content == "diff --git a/x b/x"
    db.commit.assert_called_once()


def test_best_effort_save_invalid_task_id(monkeypatch):
    """非法 UUID 的 task_id(不可能是任务)→ 静默返回。"""
    wd.save_diff_best_effort_on_close("not-a-uuid")


# ============================================================
# B2:孤儿目录恢复
# ============================================================


def test_capture_local_diff_from_dir(tmp_path):
    """宿主机捕获:已跟踪修改 + 未跟踪 new-file patch 都进结果。"""
    repo = _make_repo(tmp_path / "repo")
    result = wd.capture_local_diff_from_dir(repo)
    assert result is not None
    assert "a.txt" in result["content"]
    assert "b.txt" in result["content"]  # 未跟踪文件拼成 new file patch
    assert "new file mode 100644" in result["content"]
    assert result["metadata"]["files_changed"] == 2


def test_capture_local_diff_from_dir_no_repo(tmp_path):
    """无 .git 子目录 → None。"""
    d = tmp_path / "plain"
    d.mkdir()
    assert wd.capture_local_diff_from_dir(d) is None


def test_recover_orphan_salvages_diff_and_cleans(tmp_path, monkeypatch):
    """有 meta 的孤儿目录:任务存在 → 抢救 diff 落库 → 目录删除。"""
    import app.database as database

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(wd.tempfile, "gettempdir", lambda: str(tmp_root))
    task_uuid = uuid.uuid4()
    db = _mk_db(task_found=True, artifact_first=None)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)

    orphan = tmp_root / "sandbox_local_test1"
    repo = _make_repo(orphan / "repo")
    (orphan / ".secondlook_meta.json").write_text(
        json.dumps({"task_id": str(task_uuid), "created_at": time.time()}),
        encoding="utf-8",
    )

    handled = wd.recover_orphan_local_workspaces()
    assert handled == 1
    assert not orphan.exists()  # 抢救后清理
    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, TaskArtifact)
    assert added.kind == "git_diff"
    assert added.metadata_["recovered"] is True
    assert "a.txt" in added.content


def test_recover_orphan_task_missing_still_cleans(tmp_path, monkeypatch):
    """meta 指向不存在的任务 → 不落库,但目录仍删除(防泄漏)。"""
    import app.database as database

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(wd.tempfile, "gettempdir", lambda: str(tmp_root))
    db = _mk_db(task_found=False)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)

    orphan = tmp_root / "sandbox_local_test2"
    _make_repo(orphan / "repo")
    (orphan / ".secondlook_meta.json").write_text(
        json.dumps({"task_id": str(uuid.uuid4())}), encoding="utf-8",
    )
    handled = wd.recover_orphan_local_workspaces()
    assert handled == 1
    assert not orphan.exists()
    db.add.assert_not_called()


def test_recover_orphan_without_meta(tmp_path, monkeypatch):
    """无 meta:新目录保留(meta 写入窗口期),超龄目录删除。"""
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(wd.tempfile, "gettempdir", lambda: str(tmp_root))

    fresh = tmp_root / "sandbox_local_fresh"
    fresh.mkdir()
    old = tmp_root / "sandbox_local_old"
    old.mkdir()
    long_ago = time.time() - 7200
    os.utime(old, (long_ago, long_ago))

    handled = wd.recover_orphan_local_workspaces(max_age_no_meta=3600.0)
    assert handled == 1
    assert fresh.exists()
    assert not old.exists()


# ============================================================
# B3:resume 补回
# ============================================================


def test_reapply_workspace_diff_restores_changes(tmp_path):
    """已保存 patch 应用到恢复后的干净仓库:文件内容还原。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "a.txt").write_text("line1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    # 构造真实 patch:修改 → git diff → 还原
    (repo / "a.txt").write_text("line1\nline2-changed\n", encoding="utf-8")
    patch_text = _git(repo, "diff")
    assert "line2-changed" in patch_text
    _git(repo, "checkout", "--", "a.txt")

    task_id = uuid.uuid4().hex
    try:
        st._sessions[task_id] = {
            "session": _FakeSession(), "repo_path": str(repo),
            "mode": "local", "local_dir": str(tmp_path),
        }
        artifact = MagicMock()
        artifact.content = patch_text
        artifact.metadata_ = {"truncated": False}
        db = _mk_db(task_found=True, artifact_first=artifact)
        task = MagicMock()
        task.id = uuid.uuid4()

        wd.reapply_workspace_diff(task, db, task_id)
        assert (repo / "a.txt").read_text(encoding="utf-8") == "line1\nline2-changed\n"
    finally:
        st._sessions.pop(task_id, None)


def test_reapply_skips_truncated_patch(tmp_path):
    """截断的 patch 不可 apply → 跳过,文件不被改动。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("line1\n", encoding="utf-8")

    task_id = uuid.uuid4().hex
    try:
        st._sessions[task_id] = {
            "session": _FakeSession(), "repo_path": str(repo),
            "mode": "local", "local_dir": str(tmp_path),
        }
        artifact = MagicMock()
        artifact.content = "truncated junk..."
        artifact.metadata_ = {"truncated": True}
        db = _mk_db(task_found=True, artifact_first=artifact)
        wd.reapply_workspace_diff(MagicMock(), db, task_id)
        assert (repo / "a.txt").read_text(encoding="utf-8") == "line1\n"
    finally:
        st._sessions.pop(task_id, None)
