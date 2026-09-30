"""工作区文件树优化 单元测试(不连真实沙箱)。

覆盖:
- _browse_tree_local:噪声目录剪枝 / 深度上限 / 条目上限截断
- browse_tree:缓存命中 / refresh 绕过 / TTL 过期 / close_session 失效 / 无会话报错
- _list_files_sandbox:SDK 条目归一化(过滤+排序+真实大小)/ 目录不存在 / 异常回退 shell
- _read_file_sandbox:前端浏览路径合并为单条命令的输出解析 / MISSING 报错
- cleanup_expired_sessions_bg:过期销毁不阻塞请求路径
"""
import time
import uuid

import pytest

from app.tools import sandbox_tools


# ============================================================
# 通用 fake / fixture
# ============================================================


class FakeSession:
    """假 SandboxSession:run_command 返回预设输出并记录命令"""

    def __init__(self, outputs: list[str] | None = None, error: Exception | None = None):
        self.outputs = list(outputs or [])
        self.error = error
        self.commands: list[str] = []

    def run_command(self, cmd: str, timeout: int = 60, check: bool = False) -> str:
        self.commands.append(cmd)
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0) if self.outputs else ""

    def close(self) -> None:
        pass


@pytest.fixture
def task_id():
    """每个测试用唯一 task_id,避免污染全局 _sessions / _tree_cache。"""
    return f"test-tree-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """重置后台清理限流时间戳,保证 cleanup bg 测试可触发扫描。"""
    monkeypatch.setattr(sandbox_tools, "_last_cleanup_scan", 0.0)
    yield


def _register_local_session(tid: str, repo_path: str) -> None:
    sandbox_tools._sessions[tid] = {
        "session": FakeSession(),
        "repo_path": repo_path,
        "mode": "local",
    }


def _cleanup(tid: str) -> None:
    sandbox_tools._sessions.pop(tid, None)
    sandbox_tools._tree_cache.pop(tid, None)


# ============================================================
# _browse_tree_local:剪枝 / 深度 / 截断
# ============================================================


def test_browse_tree_local_prunes_noise_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")

    res = sandbox_tools._browse_tree_local(str(tmp_path), max_depth=4, max_entries=3000)
    paths = {e["path"] for e in res["entries"]}

    assert "README.md" in paths
    assert "src" in paths
    assert "src/main.py" in paths
    # 噪声目录整体剪掉(目录本身与内部文件都不出现)
    assert not any(p.startswith(".git") for p in paths)
    assert not any(p.startswith("node_modules") for p in paths)
    assert res["truncated"] is False
    assert res["max_depth"] == 4


def test_browse_tree_local_respects_max_depth(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "deep_file.txt").write_text("x", encoding="utf-8")

    res = sandbox_tools._browse_tree_local(str(tmp_path), max_depth=2, max_entries=3000)
    paths = {e["path"] for e in res["entries"]}

    assert "a" in paths
    assert "a/b" in paths
    # 深度 3 及以下不出现
    assert "a/b/c" not in paths
    assert "a/b/c/d/deep_file.txt" not in paths


def test_browse_tree_local_truncates_over_limit(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")

    res = sandbox_tools._browse_tree_local(str(tmp_path), max_depth=4, max_entries=2)
    assert res["truncated"] is True
    assert len(res["entries"]) == 2


# ============================================================
# browse_tree:缓存语义
# ============================================================


def test_browse_tree_cache_hit_and_refresh(tmp_path, task_id):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _register_local_session(task_id, str(tmp_path))
    try:
        first = sandbox_tools.browse_tree(task_id)
        paths = {e["path"] for e in first["entries"]}
        assert "a.txt" in paths

        # 缓存命中:新增文件不可见
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        cached = sandbox_tools.browse_tree(task_id)
        assert cached is first

        # refresh=True 绕过缓存
        refreshed = sandbox_tools.browse_tree(task_id, refresh=True)
        assert {e["path"] for e in refreshed["entries"]} >= {"a.txt", "b.txt"}

        # TTL 过期后自动重建
        ts, payload = sandbox_tools._tree_cache[task_id]
        sandbox_tools._tree_cache[task_id] = (
            ts - sandbox_tools._TREE_CACHE_TTL - 1, payload,
        )
        (tmp_path / "c.txt").write_text("x", encoding="utf-8")
        rebuilt = sandbox_tools.browse_tree(task_id)
        assert "c.txt" in {e["path"] for e in rebuilt["entries"]}
    finally:
        _cleanup(task_id)


def test_browse_tree_close_session_invalidates_cache(tmp_path, task_id):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _register_local_session(task_id, str(tmp_path))
    sandbox_tools.browse_tree(task_id)
    assert task_id in sandbox_tools._tree_cache

    sandbox_tools.close_session(task_id)
    assert task_id not in sandbox_tools._tree_cache
    assert task_id not in sandbox_tools._sessions


def test_browse_tree_no_session_raises(task_id):
    with pytest.raises(RuntimeError):
        sandbox_tools.browse_tree(task_id)


# ============================================================
# _list_files_sandbox:SDK 归一化 / 回退
# ============================================================


def _fake_sdk_listing():
    """session.list_directory 的归一化输出(与 SandboxSession.list_directory 一致)"""
    return [
        {"name": "main.py", "is_dir": False, "size": 123, "path": "/repo/main.py"},
        {"name": "src", "is_dir": True, "size": 0, "path": "/repo/src"},
        {"name": "node_modules", "is_dir": True, "size": 0, "path": "/repo/node_modules"},
        {"name": "Zebra.md", "is_dir": False, "size": 7, "path": "/repo/Zebra.md"},
    ]


def test_list_files_sandbox_sdk_mapping():
    class SdkSession(FakeSession):
        def list_directory(self, path, depth=None):
            return _fake_sdk_listing()

    ctx = {"session": SdkSession(), "repo_path": "/repo", "mode": "sandbox"}
    res = sandbox_tools._list_files_sandbox(ctx, "/repo", "", 200)

    names = [e["name"] for e in res["entries"]]
    # 噪声目录过滤;目录在前;名称大小写不敏感排序
    assert names == ["src", "main.py", "Zebra.md"]
    # SDK 给出真实文件大小
    sizes = {e["name"]: e["size"] for e in res["entries"]}
    assert sizes["main.py"] == 123
    assert sizes["src"] == 0


def test_list_files_sandbox_missing_dir():
    class SdkSession(FakeSession):
        def list_directory(self, path, depth=None):
            raise FileNotFoundError(f"目录不存在: {path}")

    ctx = {"session": SdkSession(), "repo_path": "/repo", "mode": "sandbox"}
    with pytest.raises(FileNotFoundError):
        sandbox_tools._list_files_sandbox(ctx, "/repo", "no_such", 200)


def test_list_files_sandbox_falls_back_to_shell(monkeypatch):
    class SdkSession(FakeSession):
        def list_directory(self, path, depth=None):
            raise RuntimeError("SDK 不可用")

    sentinel = {"entries": [], "fallback": True}
    monkeypatch.setattr(
        sandbox_tools, "_list_files_sandbox_shell", lambda *a, **k: sentinel
    )
    ctx = {"session": SdkSession(), "repo_path": "/repo", "mode": "sandbox"}
    res = sandbox_tools._list_files_sandbox(ctx, "/repo", "", 200)
    assert res is sentinel


# ============================================================
# _browse_tree_sandbox:find 输出解析 / 降级
# ============================================================


def test_browse_tree_sandbox_parses_find_output():
    session = FakeSession(outputs=["d\t\nd\tsrc\nf\tsrc/main.py\nd\t.git\n"])
    ctx = {"session": session, "repo_path": "/repo", "mode": "sandbox"}
    res = sandbox_tools._browse_tree_sandbox(ctx, "/repo", 4, 3000)

    assert res["truncated"] is False
    assert res["max_depth"] == 4
    assert res["entries"] == [
        {"path": "src", "type": "dir"},
        {"path": "src/main.py", "type": "file"},
        {"path": ".git", "type": "dir"},
    ]
    # find 命令带 maxdepth / prune / head 限流
    cmd = session.commands[0]
    assert "-maxdepth 4" in cmd
    assert "-prune" in cmd
    assert "head -n 3001" in cmd


def test_browse_tree_sandbox_truncated():
    lines = "d\t\n" + "\n".join(f"f\tf{i}.txt" for i in range(4))
    session = FakeSession(outputs=[lines])
    ctx = {"session": session, "repo_path": "/repo", "mode": "sandbox"}
    res = sandbox_tools._browse_tree_sandbox(ctx, "/repo", 4, 3)
    assert res["truncated"] is True
    assert len(res["entries"]) == 3


def test_browse_tree_sandbox_fallback_on_find_failure(monkeypatch):
    session = FakeSession(error=RuntimeError("find: command not found"))
    ctx = {"session": session, "repo_path": "/repo", "mode": "sandbox"}
    # 降级路径走 _list_files_sandbox(SDK/shell),这里直接打桩
    monkeypatch.setattr(
        sandbox_tools,
        "_list_files_sandbox",
        lambda *a, **k: {"entries": [{"name": "src", "type": "dir", "size": 0}]},
    )
    res = sandbox_tools._browse_tree_sandbox(ctx, "/repo", 4, 3000)
    assert res["max_depth"] == 1
    assert res["truncated"] is True
    assert res["entries"] == [{"path": "src", "type": "dir"}]


def test_browse_tree_sandbox_fallback_on_empty_output(monkeypatch):
    # find 报错时 stdout 为空(无 tab 分隔行)→ 同样降级
    session = FakeSession(outputs=[""])
    ctx = {"session": session, "repo_path": "/repo", "mode": "sandbox"}
    monkeypatch.setattr(
        sandbox_tools,
        "_list_files_sandbox",
        lambda *a, **k: {"entries": []},
    )
    res = sandbox_tools._browse_tree_sandbox(ctx, "/repo", 4, 3000)
    assert res["max_depth"] == 1
    assert res["entries"] == []


# ============================================================
# _read_file_sandbox:浏览路径合并单命令
# ============================================================


def test_read_file_sandbox_browse_single_command():
    session = FakeSession(outputs=["3\nline1\nline2\nline3\n"])
    ctx = {"session": session, "repo_path": "/repo", "mode": "sandbox"}
    res = sandbox_tools._read_file_sandbox(
        ctx, "/repo", "a.py", max_lines=500, offset=1, with_line_numbers=False
    )
    # 仅 1 次远程命令(存在检查+行数+截取合并)
    assert len(session.commands) == 1
    assert res["content"] == "line1\nline2\nline3"
    assert res["total_lines"] == 3
    assert res["start_line"] == 1
    assert res["end_line"] == 3
    assert res["truncated"] is False


def test_read_file_sandbox_browse_missing_file():
    session = FakeSession(outputs=["MISSING\n"])
    ctx = {"session": session, "repo_path": "/repo", "mode": "sandbox"}
    with pytest.raises(FileNotFoundError):
        sandbox_tools._read_file_sandbox(
            ctx, "/repo", "nope.py", max_lines=500, offset=1, with_line_numbers=False
        )


# ============================================================
# cleanup_expired_sessions_bg:非阻塞
# ============================================================


def test_cleanup_bg_does_not_block_request(monkeypatch, task_id):
    """过期 session 的实际销毁在后台线程,请求路径快速返回。"""
    sandbox_tools._sessions[task_id] = {
        "session": FakeSession(),
        "repo_path": "/repo",
        "mode": "local",
        "completed_at": time.time() - sandbox_tools._SESSION_TTL_AFTER_COMPLETE - 10,
    }

    def slow_close(tid):
        time.sleep(0.5)
        sandbox_tools._sessions.pop(tid, None)

    monkeypatch.setattr(sandbox_tools, "close_session", slow_close)

    start = time.monotonic()
    sandbox_tools.cleanup_expired_sessions_bg()
    elapsed = time.monotonic() - start
    assert elapsed < 0.2, f"cleanup_expired_sessions_bg 阻塞了 {elapsed:.2f}s"

    # 清理最终在后台完成
    deadline = time.monotonic() + 3
    while task_id in sandbox_tools._sessions and time.monotonic() < deadline:
        time.sleep(0.05)
    assert task_id not in sandbox_tools._sessions


def test_cleanup_bg_throttles_scan(monkeypatch, task_id):
    """限流间隔内二次调用不再扫描/起线程(过期 session 不被处理)。"""
    sandbox_tools._sessions[task_id] = {
        "session": FakeSession(),
        "repo_path": "/repo",
        "mode": "local",
        "completed_at": time.time() - sandbox_tools._SESSION_TTL_AFTER_COMPLETE - 10,
    }
    calls: list[str] = []
    monkeypatch.setattr(
        sandbox_tools, "close_session", lambda tid: calls.append(tid)
    )

    sandbox_tools.cleanup_expired_sessions_bg()
    sandbox_tools.cleanup_expired_sessions_bg()  # 限流窗口内,直接返回

    deadline = time.monotonic() + 3
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls == [task_id], "限流窗口内第二次调用不应再起清理线程"
    sandbox_tools._sessions.pop(task_id, None)
