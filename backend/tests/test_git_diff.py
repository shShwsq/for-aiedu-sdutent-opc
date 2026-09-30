"""git_diff 工具测试(local 模式 + 临时 git 仓库)"""
import subprocess
import uuid

import pytest

from app.config import settings
from app.tools import sandbox_tools


@pytest.fixture
def local_mode(monkeypatch):
    """强制 local 模式,避免连真实沙箱。"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "local")
    yield


@pytest.fixture
def task_id():
    """每个测试用唯一 task_id,避免污染全局 _sessions 字典。"""
    return f"test-gitdiff-{uuid.uuid4()}"


def _cleanup(tid: str) -> None:
    try:
        sandbox_tools.close_session(tid)
    except Exception:
        pass


def _git(repo: str, *args: str) -> None:
    """在临时仓库执行 git 命令(commit 用 -c 传身份,不写任何 git config)"""
    cmd = ["git", "-C", repo] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"git {' '.join(args)} 失败: {result.stderr}"


def _commit(repo: str, filename: str, content: str, message: str) -> None:
    with open(f"{repo}/{filename}", "w", encoding="utf-8") as f:
        f.write(content)
    _git(repo, "add", filename)
    _git(
        repo,
        "-c", "user.email=test@example.com", "-c", "user.name=test",
        "commit", "-m", message,
    )


@pytest.fixture
def git_repo(tmp_path):
    """两次提交的临时仓库:v1 建 a.py,v2 改 a.py + 新增 b.py"""
    repo = str(tmp_path / "repo")
    import os
    os.makedirs(repo)
    _git(repo, "init", "-b", "main")
    _commit(repo, "a.py", "x = 1\n", "v1")
    _commit(repo, "a.py", "x = 2\ny = 3\n", "v2-a")
    _commit(repo, "b.py", "z = 0\n", "v2-b")
    return repo


# ---------- 基础结构化 diff ----------

def test_git_diff_default_last_commit(local_mode, git_repo, task_id):
    """默认 base=HEAD~1 head=HEAD:只看最近一次提交(b.py 新增)"""
    try:
        result = sandbox_tools.git_diff(git_repo, task_id=task_id)
        assert result["exit_code"] == 0
        assert result["total_files"] == 1
        f = result["files"][0]
        assert f["path"] == "b.py"
        assert f["additions"] == 1
        assert f["deletions"] == 0
        assert "+z = 0" in f["patch"]
    finally:
        _cleanup(task_id)


def test_git_diff_full_range(local_mode, git_repo, task_id):
    """显式区间:从首个提交到 HEAD,a.py 修改 + b.py 新增"""
    try:
        result = sandbox_tools.git_diff(
            git_repo, base="HEAD~2", head="HEAD", task_id=task_id,
        )
        assert result["exit_code"] == 0
        assert result["total_files"] == 2
        by_path = {f["path"]: f for f in result["files"]}
        assert by_path["a.py"]["additions"] == 2  # x=2, y=3
        assert by_path["a.py"]["deletions"] == 1  # x=1
        assert "-x = 1" in by_path["a.py"]["patch"]
        assert by_path["b.py"]["additions"] == 1
    finally:
        _cleanup(task_id)


def test_git_diff_stat_only_no_patch(local_mode, git_repo, task_id):
    """stat_only=True:只返回增删行数,无 patch 字段"""
    try:
        result = sandbox_tools.git_diff(
            git_repo, base="HEAD~2", head="HEAD", stat_only=True, task_id=task_id,
        )
        assert result["exit_code"] == 0
        assert result["total_files"] == 2
        for f in result["files"]:
            assert "patch" not in f
            assert f["additions"] >= 0
    finally:
        _cleanup(task_id)


def test_git_diff_file_path_filter(local_mode, git_repo, task_id):
    """file_path 过滤:只看 a.py"""
    try:
        result = sandbox_tools.git_diff(
            git_repo, base="HEAD~2", head="HEAD", file_path="a.py", task_id=task_id,
        )
        assert result["exit_code"] == 0
        assert result["total_files"] == 1
        assert result["files"][0]["path"] == "a.py"
    finally:
        _cleanup(task_id)


# ---------- 错误处理与安全 ----------

def test_git_diff_ref_option_injection_rejected(local_mode, git_repo, task_id):
    """ref 以 - 开头(选项注入)→ ValueError"""
    try:
        with pytest.raises(ValueError, match="选项注入|- 开头"):
            sandbox_tools.git_diff(git_repo, base="--output=/etc/passwd", task_id=task_id)
        with pytest.raises(ValueError, match="选项注入|- 开头"):
            sandbox_tools.git_diff(git_repo, head="-main", task_id=task_id)
    finally:
        _cleanup(task_id)


def test_git_diff_ref_whitespace_rejected(local_mode, git_repo, task_id):
    """ref 含空白字符 → ValueError"""
    try:
        with pytest.raises(ValueError, match="空白"):
            sandbox_tools.git_diff(git_repo, base="a b", task_id=task_id)
    finally:
        _cleanup(task_id)


def test_git_diff_bad_ref_returns_error(local_mode, git_repo, task_id):
    """不存在的 ref → exit_code 非 0 且附 error(不抛异常)"""
    try:
        result = sandbox_tools.git_diff(git_repo, base="no_such_branch", task_id=task_id)
        assert result["exit_code"] != 0
        assert result["error"]
        assert result["files"] == []
    finally:
        _cleanup(task_id)


# ---------- 解析器纯函数 ----------

def test_parse_numstat_binary_file():
    """二进制文件 numstat 行为 -:additions/deletions 记 0 不报错"""
    output = "-\t-\tassets/logo.png\n3\t1\tsrc/a.py\n"
    files = sandbox_tools._parse_numstat(output)
    assert files == [
        {"path": "assets/logo.png", "additions": 0, "deletions": 0},
        {"path": "src/a.py", "additions": 3, "deletions": 1},
    ]


def test_parse_diff_patches_split_and_paths():
    """按 diff --git 切块,路径从 +++ b/ 提取;删除文件从 --- a/ 提取"""
    output = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/gone.py b/gone.py\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    )
    patches = sandbox_tools._parse_diff_patches(output)
    assert set(patches.keys()) == {"x.py", "gone.py"}
    assert "+new" in patches["x.py"]
