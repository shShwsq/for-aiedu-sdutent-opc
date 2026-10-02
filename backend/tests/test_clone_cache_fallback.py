"""clone_repo_with_fallback 的 bare 缓存快路径 + 挂载 单元测试(mock,不连网)。

覆盖:
- local 命中:从 bare 路径本地克隆,use_depth=False
- local 未命中/ensure 异常:降级原远程候选链
- local 缓存路径的 CloneSkippedError 直接上抛(不再试远程)
- sandbox 命中:从容器内挂载路径克隆;cache_key 不匹配(LLM 克隆其他仓库)走远程
- _get_or_create_session:sandbox 模式构造只读挂载卷 + ctx 记录;local 模式写 meta
"""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.tools.sandbox_tools as st
from app.config import settings

REPO_URL = "https://github.com/foo/bar"


def _fake_local_ctx(tmp_path):
    return {"mode": "local", "local_dir": tmp_path}


# ============================================================
# local 模式:缓存快路径
# ============================================================


def test_local_cache_hit_clones_from_bare_path(monkeypatch, tmp_path):
    """ensure 命中 → _clone_repo_local 收到 bare 路径且 use_depth=False,不进远程链。"""
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))
    bare = tmp_path / "bare" / "bar.git"
    monkeypatch.setattr(st, "ensure_bare_cache", lambda *a, **kw: bare)

    calls: list[dict] = []

    def _fake_clone(ctx, url, repo_name, branch, task_id="", cancellable=False,
                    progress_callback=None, use_depth=True):
        calls.append({"url": url, "use_depth": use_depth, "branch": branch})
        return {"path": str(tmp_path / "bar"), "files_count": 1}

    monkeypatch.setattr(st, "_clone_repo_local", _fake_clone)
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)

    result = st.clone_repo_with_fallback(REPO_URL, task_id=task_id)
    assert result["path"].endswith("bar")
    assert len(calls) == 1
    assert calls[0]["url"] == str(bare)
    assert calls[0]["use_depth"] is False
    assert calls[0]["branch"] is None


def test_local_cache_hit_passes_branch(monkeypatch, tmp_path):
    """命中时 branch 透传给本地克隆(--branch 由 _clone_repo_local 拼)。"""
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))
    monkeypatch.setattr(st, "ensure_bare_cache", lambda *a, **kw: tmp_path / "b.git")

    calls: list[dict] = []

    def _fake_clone(ctx, url, repo_name, branch, **kw):
        calls.append({"branch": branch})
        return {"path": "p", "files_count": 0}

    monkeypatch.setattr(st, "_clone_repo_local", _fake_clone)
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)
    st.clone_repo_with_fallback(REPO_URL, branch="dev", task_id=task_id)
    assert calls[0]["branch"] == "dev"


def test_local_cache_miss_falls_back_to_remote(monkeypatch, tmp_path):
    """ensure 返回 None → 落入原远程候选链(收到远程 URL)。"""
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))
    monkeypatch.setattr(st, "ensure_bare_cache", lambda *a, **kw: None)

    calls: list[str] = []

    def _fake_clone(ctx, url, *a, **kw):
        calls.append(url)
        return {"path": "p", "files_count": 1}

    monkeypatch.setattr(st, "_clone_repo_local", _fake_clone)
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)
    result = st.clone_repo_with_fallback(REPO_URL, task_id=task_id)
    assert result is not None
    # 无 token:候选链首项为 SSH 形式(provider 存在),证明走的是远程链
    assert calls == ["git@github.com:foo/bar.git"]


def test_local_cache_error_falls_back(monkeypatch, tmp_path):
    """ensure 抛异常 → 仅 warning,原链照常完成。"""
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))

    def _boom(*a, **kw):
        raise RuntimeError("cache dir unwritable")

    monkeypatch.setattr(st, "ensure_bare_cache", _boom)
    monkeypatch.setattr(st, "_clone_repo_local", lambda *a, **kw: {"path": "p", "files_count": 1})
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)
    result = st.clone_repo_with_fallback(REPO_URL, task_id=task_id)
    assert result["path"] == "p"


def test_local_cache_skip_error_propagates(monkeypatch, tmp_path):
    """缓存本地克隆中用户跳过(CloneSkippedError)→ 直接上抛,不试远程协议。"""
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))
    monkeypatch.setattr(st, "ensure_bare_cache", lambda *a, **kw: tmp_path / "b.git")

    def _raise_skip(*a, **kw):
        raise st.CloneSkippedError("用户已跳过预克隆: bar")

    monkeypatch.setattr(st, "_clone_repo_local", _raise_skip)
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)
    try:
        st.clone_repo_with_fallback(REPO_URL, task_id=task_id, cancellable=True)
        raise AssertionError("应抛 CloneSkippedError")
    except st.CloneSkippedError:
        pass


# ============================================================
# sandbox 模式:挂载路径克隆 / cache_key 不匹配降级
# ============================================================


def _fake_sandbox_ctx(tmp_path=None):
    return {
        "mode": "sandbox",
        "session": MagicMock(),
        "repo_path": "",
        "cache_key": st.repo_cache_key(REPO_URL),
        "cache_mount": "/home/user/repo-cache/bar.git",
    }


def test_sandbox_cache_hit_clones_from_mount(monkeypatch, tmp_path):
    """会话挂载了本仓库缓存 → 容器内从挂载路径克隆,use_depth=False。"""
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_sandbox_ctx())

    calls: list[dict] = []

    def _fake_clone(ctx, url, repo_name, branch, task_id="", cancellable=False,
                    progress_callback=None, use_depth=True):
        calls.append({"url": url, "use_depth": use_depth})
        return {"path": "/home/user/repos/bar", "files_count": 1}

    monkeypatch.setattr(st, "_clone_repo_sandbox", _fake_clone)
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)
    result = st.clone_repo_with_fallback(REPO_URL, task_id=task_id)
    assert result["path"] == "/home/user/repos/bar"
    assert calls == [{"url": "/home/user/repo-cache/bar.git", "use_depth": False}]


def test_sandbox_cache_key_mismatch_falls_back(monkeypatch, tmp_path):
    """LLM 运行中克隆其他仓库(cache_key 不匹配)→ 走原远程链。"""
    task_id = uuid.uuid4().hex
    other_url = "https://github.com/foo/other"

    def _ctx(tid, **kw):
        ctx = _fake_sandbox_ctx()
        assert st.repo_cache_key(other_url) != ctx["cache_key"]
        return ctx

    monkeypatch.setattr(st, "_get_or_create_session", _ctx)
    calls: list[str] = []
    monkeypatch.setattr(st, "_clone_repo_sandbox", lambda ctx, url, *a, **kw: calls.append(url) or {"path": "p", "files_count": 1})
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)
    result = st.clone_repo_with_fallback(other_url, task_id=task_id)
    assert result is not None
    assert calls and calls[0] != "/home/user/repo-cache/bar.git"  # 收到的是远程 URL


# ============================================================
# _get_or_create_session:挂载卷构造 / local meta 埋点
# ============================================================


def test_session_creation_mounts_readonly_volume(monkeypatch, tmp_path):
    """sandbox 模式:新建会话挂载本任务仓库的 bare 子目录(只读),ctx 记录 key/mount。"""
    task_id = uuid.uuid4().hex
    try:
        monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")
        monkeypatch.setattr(settings, "REPO_CACHE_SANDBOX_ENABLED", True)
        monkeypatch.setattr(settings, "REPO_CACHE_SANDBOX_HOST_DIR", "/data/repo_cache")
        monkeypatch.setattr(st, "ensure_bare_cache", lambda *a, **kw: tmp_path / "bar.git")
        monkeypatch.setattr(
            st, "sandbox_mount",
            lambda url: (
                f"/data/repo_cache/{st.repo_cache_key(url)}/bar.git",
                "/home/user/repo-cache/bar.git",
            ),
        )

        captured: dict = {}
        fake_session = MagicMock()

        def _fake_create(extra_volumes=None):
            captured["extra_volumes"] = extra_volumes
            return fake_session

        monkeypatch.setattr(st, "create_sandbox", _fake_create)

        ctx = st._get_or_create_session(
            task_id, repo_url=REPO_URL, branch="main", git_tokens={},
        )
        vols = captured["extra_volumes"]
        assert vols and len(vols) == 1
        host_dir, mount_path, read_only = vols[0]
        assert host_dir.startswith("/data/repo_cache/") and host_dir.endswith("bar.git")
        assert mount_path == "/home/user/repo-cache/bar.git"
        assert read_only is True
        assert ctx["cache_key"] == st.repo_cache_key(REPO_URL)
        assert ctx["cache_mount"] == mount_path
        assert ctx["session"] is fake_session
    finally:
        st._sessions.pop(task_id, None)


def test_session_creation_local_writes_meta(monkeypatch, tmp_path):
    """local 模式:新建会话写 .secondlook_meta.json(孤儿恢复埋点)。"""
    task_id = uuid.uuid4().hex
    try:
        monkeypatch.setattr(settings, "SANDBOX_MODE", "local")
        fake_session = SimpleNamespace(local_dir=str(tmp_path))
        monkeypatch.setattr(st, "create_sandbox", lambda extra_volumes=None: fake_session)

        st._get_or_create_session(task_id)
        meta_path = tmp_path / ".secondlook_meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["task_id"] == task_id
    finally:
        st._sessions.pop(task_id, None)


def test_session_reuse_ignores_repo_url(monkeypatch, tmp_path):
    """已有会话时带 repo_url 调用:直接复用,不再建挂载(容器无法追加)。"""
    task_id = uuid.uuid4().hex
    try:
        st._sessions[task_id] = {"session": MagicMock(), "repo_path": "", "mode": "local"}
        created: list[int] = []
        monkeypatch.setattr(st, "create_sandbox", lambda extra_volumes=None: created.append(1))
        ctx = st._get_or_create_session(task_id, repo_url=REPO_URL)
        assert created == []  # 未新建
        assert ctx["mode"] == "local"
    finally:
        st._sessions.pop(task_id, None)
