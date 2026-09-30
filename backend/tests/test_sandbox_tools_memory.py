"""sandbox_tools 记忆文件写入 / read_file 白名单 单元测试(local 模式,不连真实沙箱)。

覆盖:
- write_project_memory_file:local 模式写入 local_dir/.agent_memory/project_memory.md
- write_global_memory_file:local 模式写入 local_dir/.agent_memory/global_memory.md
- read_file 白名单:绝对路径 /home/user/.agent_memory/project_memory.md 能读到记忆文件
- read_file 拒绝仓库外逃逸(非白名单绝对路径 / 记忆目录穿越)
"""
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.tools import sandbox_tools

_MEMORY_PATH = "/home/user/.agent_memory/project_memory.md"


@pytest.fixture
def local_mode(monkeypatch):
    """强制 local 模式(本地文件系统),避免连真实沙箱。"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "local")
    yield


@pytest.fixture
def task_id():
    """每个测试用唯一 task_id,避免污染全局 _sessions 字典。"""
    return f"test-mem-{uuid.uuid4()}"


def _cleanup(tid: str) -> None:
    try:
        sandbox_tools.close_session(tid)
    except Exception:
        pass


# ---------- write_project_memory_file ----------

def test_write_memory_file_local_writes_file(local_mode, task_id):
    """local 模式:写入内容到 local_dir/.agent_memory/project_memory.md。"""
    try:
        content = "## Hard Constraints\n- rule A\n- rule B"
        sandbox_tools.write_project_memory_file(task_id, content)

        ctx = sandbox_tools._get_or_create_session(task_id)
        mem_file = Path(ctx["local_dir"]) / ".agent_memory" / "project_memory.md"
        assert mem_file.is_file()
        assert mem_file.read_text(encoding="utf-8") == content
    finally:
        _cleanup(task_id)


def test_write_memory_file_empty_clears_previous(local_mode, task_id):
    """写空串清空旧文件(避免看到上一个项目的记忆)。"""
    try:
        sandbox_tools.write_project_memory_file(task_id, "old content from prev project")
        sandbox_tools.write_project_memory_file(task_id, "")

        ctx = sandbox_tools._get_or_create_session(task_id)
        mem_file = Path(ctx["local_dir"]) / ".agent_memory" / "project_memory.md"
        assert mem_file.is_file()
        assert mem_file.read_text(encoding="utf-8") == ""
    finally:
        _cleanup(task_id)


# ---------- write_global_memory_file ----------

def test_write_global_memory_file_local_writes_file(local_mode, task_id):
    """local 模式:写入内容到 local_dir/.agent_memory/global_memory.md。"""
    try:
        content = "## Lessons Learned\n- lesson A"
        sandbox_tools.write_global_memory_file(task_id, content)

        ctx = sandbox_tools._get_or_create_session(task_id)
        mem_file = Path(ctx["local_dir"]) / ".agent_memory" / "global_memory.md"
        assert mem_file.is_file()
        assert mem_file.read_text(encoding="utf-8") == content
    finally:
        _cleanup(task_id)


def test_write_global_memory_file_empty_clears_previous(local_mode, task_id):
    """写空串清空旧文件(避免看到上一个用户的全局记忆)。"""
    try:
        sandbox_tools.write_global_memory_file(task_id, "old global memory")
        sandbox_tools.write_global_memory_file(task_id, "")

        ctx = sandbox_tools._get_or_create_session(task_id)
        mem_file = Path(ctx["local_dir"]) / ".agent_memory" / "global_memory.md"
        assert mem_file.is_file()
        assert mem_file.read_text(encoding="utf-8") == ""
    finally:
        _cleanup(task_id)


def test_read_file_whitelist_reads_global_memory_file(local_mode, task_id):
    """read_file 传全局记忆文件绝对路径 → 读到写入内容(白名单不受 repo_path 限制)。"""
    try:
        content = "## Tech Stack\n- FastAPI + Vue"
        sandbox_tools.write_global_memory_file(task_id, content)

        result = sandbox_tools.read_file(
            "/home/user/repos/dummy",
            "/home/user/.agent_memory/global_memory.md",
            task_id=task_id,
        )
        assert result["total_lines"] == 2
        assert "## Tech Stack" in result["content"]
        assert "- FastAPI + Vue" in result["content"]
    finally:
        _cleanup(task_id)


# ---------- read_file 白名单 ----------

def test_read_file_whitelist_reads_memory_file(local_mode, task_id):
    """read_file 传记忆文件绝对路径 → 读到写入的记忆内容(带行号 + 分页结构)。"""
    try:
        content = "## Hard Constraints\n- rule A\n- rule B"
        sandbox_tools.write_project_memory_file(task_id, content)

        # repo_path 参数对白名单路径无意义,传任意值
        result = sandbox_tools.read_file(
            "/home/user/repos/dummy", _MEMORY_PATH, task_id=task_id,
        )
        assert result["total_lines"] == 3
        assert "## Hard Constraints" in result["content"]
        assert "- rule A" in result["content"]
        assert "- rule B" in result["content"]
        assert result["truncated"] is False
    finally:
        _cleanup(task_id)


def test_read_file_whitelist_pagination(local_mode, task_id):
    """记忆文件也支持 offset/max_lines 分页(与仓库 read_file 一致体验)。"""
    try:
        content = "\n".join(f"line {i}" for i in range(1, 11))
        sandbox_tools.write_project_memory_file(task_id, content)

        result = sandbox_tools.read_file(
            "/dummy", _MEMORY_PATH, max_lines=3, offset=5, task_id=task_id,
        )
        assert result["start_line"] == 5
        assert result["end_line"] == 7
        assert result["total_lines"] == 10
        assert "line 5" in result["content"]
        assert "line 7" in result["content"]
        assert "line 8" not in result["content"]
    finally:
        _cleanup(task_id)


def test_read_file_memory_not_written_raises(local_mode, task_id):
    """记忆文件未写入(任务未 clone)→ FileNotFoundError。"""
    try:
        with pytest.raises(FileNotFoundError):
            sandbox_tools.read_file("/dummy", _MEMORY_PATH, task_id=task_id)
    finally:
        _cleanup(task_id)


# ---------- read_file 拒绝仓库外逃逸 ----------

def test_read_file_non_whitelist_absolute_path_rejected(local_mode, task_id):
    """非白名单绝对路径(如 /etc/passwd)仍受 repo_path 限制 → 抛非法路径。"""
    try:
        with pytest.raises(ValueError, match="非法路径"):
            sandbox_tools.read_file("/home/user/repos/dummy", "/etc/passwd", task_id=task_id)
    finally:
        _cleanup(task_id)


def test_read_file_memory_traversal_rejected(local_mode, task_id):
    """记忆目录内的 .. 穿越(如 /home/user/.agent_memory/../secret)→ 抛非法路径。"""
    try:
        with pytest.raises(ValueError, match="非法记忆文件路径"):
            sandbox_tools.read_file(
                "/dummy",
                "/home/user/.agent_memory/../secret",
                task_id=task_id,
            )
    finally:
        _cleanup(task_id)
