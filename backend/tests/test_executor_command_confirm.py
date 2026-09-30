"""内置 react_agent 命令确认机制单元测试(executor_command_confirm)。

覆盖:
- _classify_command:safe / normal / dangerous 分类(含复合命令)
- set_current_task + _CURRENT_EXECUTOR_COMMAND_CONFIRM ContextVar 传递
- execute_tool 自动注入 command_confirm_mode 到 run_command
- sandbox 模式 run_command:per_command + dangerous 推确认,always_approve 跳过
- local 模式 run_command:dangerous 始终推确认(无视 command_confirm_mode),safe 跳过
- 用户拒绝时返回 [用户拒绝执行此命令] + exit_code=-1

不连真实沙箱,全部用 monkeypatch 替换 create_sandbox / subprocess.run /
request_command_confirm / wait_for_command_confirm。
"""
import threading
import uuid
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.tools import sandbox_tools
from app.tools import schema as schema_module
from app.tools.schema import execute_tool, set_current_task


# ============================================================
# 公共 fixture
# ============================================================

@pytest.fixture
def task_id():
    """每个测试用唯一 task_id,避免污染全局 _sessions 字典。"""
    return f"test-confirm-{uuid.uuid4()}"


def _cleanup(tid: str) -> None:
    try:
        sandbox_tools.close_session(tid)
    except Exception:
        pass
    # 清理可能的 pending command 残留
    try:
        from app.user_interaction import clear_pending_command_confirm
        clear_pending_command_confirm(tid)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_contextvar():
    """每个测试结束复位 _CURRENT_EXECUTOR_COMMAND_CONFIRM,避免跨测试污染。"""
    yield
    schema_module._CURRENT_EXECUTOR_COMMAND_CONFIRM.set("always_approve")


@pytest.fixture
def sandbox_mode(monkeypatch):
    """强制 sandbox 模式 + mock create_sandbox 返回伪 session。"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")

    fake_session = MagicMock()
    fake_session.run_command.return_value = "output line\nEXIT_CODE:0"

    def _fake_create():
        return fake_session

    monkeypatch.setattr(sandbox_tools, "create_sandbox", _fake_create)
    return fake_session


@pytest.fixture
def local_mode(monkeypatch):
    """强制 local 模式。"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "local")
    return monkeypatch


# ============================================================
# _classify_command:命令分类
# ============================================================

def test_classify_safe_simple_command():
    """git status → ("safe", None)。"""
    level, pattern = sandbox_tools._classify_command("git status")
    assert level == "safe"
    assert pattern is None


def test_classify_safe_compound_all_safe():
    """git status && git diff → ("safe", None)。"""
    level, pattern = sandbox_tools._classify_command("git status && git diff")
    assert level == "safe"
    assert pattern is None


def test_classify_dangerous_rm_rf_root():
    """rm -rf / → ("dangerous", <pattern>)。"""
    level, pattern = sandbox_tools._classify_command("rm -rf /")
    assert level == "dangerous"
    assert pattern is not None
    assert "rm" in pattern


def test_classify_dangerous_sudo():
    """sudo apt install → ("dangerous", <sudo pattern>)。"""
    level, pattern = sandbox_tools._classify_command("sudo apt install foo")
    assert level == "dangerous"
    assert "sudo" in pattern


def test_classify_dangerous_compound_mixed():
    """git status && rm -rf / → 任一子命令危险则整体危险。"""
    level, pattern = sandbox_tools._classify_command("git status && rm -rf /")
    assert level == "dangerous"
    assert pattern is not None


def test_classify_normal_unknown_command():
    """未知命令 some-weird-bin → ("normal", None)。"""
    level, pattern = sandbox_tools._classify_command("some-weird-bin --flag")
    assert level == "normal"
    assert pattern is None


def test_classify_normal_empty_command():
    """空命令 → ("normal", None)。"""
    level, pattern = sandbox_tools._classify_command("")
    assert level == "normal"
    assert pattern is None


# ============================================================
# set_current_task + ContextVar
# ============================================================

def test_set_current_task_per_command_propagates():
    """set_current_task(executor_command_confirm="per_command") → ContextVar 反映。"""
    set_current_task(
        "task-x",
        scenario_id="general",
        executor_command_confirm="per_command",
    )
    assert schema_module._CURRENT_EXECUTOR_COMMAND_CONFIRM.get() == "per_command"


def test_set_current_task_always_approve_propagates():
    """set_current_task(executor_command_confirm="always_approve") → ContextVar 反映。"""
    set_current_task(
        "task-y",
        scenario_id="general",
        executor_command_confirm="always_approve",
    )
    assert schema_module._CURRENT_EXECUTOR_COMMAND_CONFIRM.get() == "always_approve"


def test_set_current_task_invalid_value_defaults_to_always_approve():
    """无效值(如 "foo") → 降级为 always_approve。"""
    set_current_task(
        "task-z",
        scenario_id="general",
        executor_command_confirm="foo-invalid",
    )
    assert schema_module._CURRENT_EXECUTOR_COMMAND_CONFIRM.get() == "always_approve"


def test_set_current_task_default_param_is_always_approve():
    """不传 executor_command_confirm → 默认 always_approve。"""
    set_current_task("task-default", scenario_id="general")
    assert schema_module._CURRENT_EXECUTOR_COMMAND_CONFIRM.get() == "always_approve"


# ============================================================
# execute_tool 自动注入 command_confirm_mode
# ============================================================

def test_execute_tool_injects_command_confirm_mode_to_run_command(monkeypatch):
    """execute_tool("run_command", {...}) 自动从 ContextVar 注入 command_confirm_mode。"""
    set_current_task("task-inject", executor_command_confirm="per_command")

    captured = {}

    def _fake_run_command(command, repo_path="", timeout=60, task_id="", command_confirm_mode="always_approve"):
        captured["command_confirm_mode"] = command_confirm_mode
        return {"output": "ok", "exit_code": 0, "truncated": False}

    monkeypatch.setitem(schema_module.TOOL_FUNCTIONS, "run_command", _fake_run_command)

    execute_tool("run_command", {"command": "ls", "task_id": "task-inject"})
    assert captured["command_confirm_mode"] == "per_command"


def test_execute_tool_does_not_inject_to_other_tools(monkeypatch):
    """execute_tool 对非 run_command 工具不注入 command_confirm_mode。"""
    set_current_task("task-noinject", executor_command_confirm="per_command")

    captured = {}

    def _fake_list_files(repo_path=".", task_id=""):
        captured["args"] = "called"
        return {"entries": []}

    monkeypatch.setitem(schema_module.TOOL_FUNCTIONS, "list_files", _fake_list_files)

    execute_tool("list_files", {"repo_path": "."})
    assert captured["args"] == "called"
    # 确认没有意外把 command_confirm_mode 注入到 list_files
    assert "command_confirm_mode" not in captured


# ============================================================
# sandbox 模式 run_command:per_command + dangerous 推确认
# ============================================================

def test_sandbox_per_command_dangerous_triggers_confirm(sandbox_mode, task_id, monkeypatch):
    """sandbox + per_command + dangerous → request_command_confirm 被调用。"""
    try:
        called = {"request": False, "wait": False}

        def _fake_request(tid, desc):
            called["request"] = True
            assert desc["command"] == "rm -rf /"
            assert desc["tool"] == "run_command"

        def _fake_wait(tid, cid):
            called["wait"] = True
            return True  # 用户同意

        monkeypatch.setattr(sandbox_tools, "request_command_confirm", _fake_request)
        monkeypatch.setattr(sandbox_tools, "wait_for_command_confirm", _fake_wait)

        result = sandbox_tools.run_command(
            "rm -rf /", task_id=task_id, command_confirm_mode="per_command",
        )
        assert called["request"] is True
        assert called["wait"] is True
        # 用户同意后继续执行(session.run_command 已 mock 返回 EXIT_CODE:0)
        assert result["exit_code"] == 0
    finally:
        _cleanup(task_id)


def test_sandbox_per_command_dangerous_user_rejects(sandbox_mode, task_id, monkeypatch):
    """sandbox + per_command + dangerous + 用户拒绝 → exit_code=-1 + [用户拒绝执行此命令]。"""
    try:
        monkeypatch.setattr(sandbox_tools, "request_command_confirm", lambda tid, desc: None)
        monkeypatch.setattr(sandbox_tools, "wait_for_command_confirm", lambda tid, cid: False)

        result = sandbox_tools.run_command(
            "rm -rf /", task_id=task_id, command_confirm_mode="per_command",
        )
        assert result["exit_code"] == -1
        assert "用户拒绝" in result["output"]
        assert result["truncated"] is False
    finally:
        _cleanup(task_id)


def test_sandbox_always_approve_dangerous_skips_confirm(sandbox_mode, task_id, monkeypatch):
    """sandbox + always_approve + dangerous → 不推确认,直接执行。"""
    try:
        called = {"request": False}

        def _fail_request(tid, desc):
            called["request"] = True
            pytest.fail("always_approve 不应推确认")

        monkeypatch.setattr(sandbox_tools, "request_command_confirm", _fail_request)
        monkeypatch.setattr(sandbox_tools, "wait_for_command_confirm", lambda tid, cid: True)

        result = sandbox_tools.run_command(
            "rm -rf /", task_id=task_id, command_confirm_mode="always_approve",
        )
        assert called["request"] is False
        # 沙箱内执行,mock session 返回 EXIT_CODE:0
        assert result["exit_code"] == 0
    finally:
        _cleanup(task_id)


def test_sandbox_per_command_safe_skips_confirm(sandbox_mode, task_id, monkeypatch):
    """sandbox + per_command + safe 命令 → 不推确认(只 dangerous 才推)。"""
    try:
        called = {"request": False}

        def _fail_request(tid, desc):
            called["request"] = True
            pytest.fail("safe 命令不应推确认")

        monkeypatch.setattr(sandbox_tools, "request_command_confirm", _fail_request)

        result = sandbox_tools.run_command(
            "git status", task_id=task_id, command_confirm_mode="per_command",
        )
        assert called["request"] is False
        assert result["exit_code"] == 0
    finally:
        _cleanup(task_id)


def test_sandbox_per_command_normal_skips_confirm(sandbox_mode, task_id, monkeypatch):
    """sandbox + per_command + normal 命令 → 不推确认(只 dangerous 才推)。"""
    try:
        called = {"request": False}

        def _fail_request(tid, desc):
            called["request"] = True
            pytest.fail("normal 命令不应推确认")

        monkeypatch.setattr(sandbox_tools, "request_command_confirm", _fail_request)

        result = sandbox_tools.run_command(
            "some-unknown-cmd", task_id=task_id, command_confirm_mode="per_command",
        )
        assert called["request"] is False
    finally:
        _cleanup(task_id)


# ============================================================
# local 模式 run_command:dangerous 始终推确认(无视 command_confirm_mode)
# ============================================================

def test_local_dangerous_always_approve_still_confirms(local_mode, task_id, monkeypatch):
    """local + always_approve + dangerous → 仍推确认(宿主机无隔离边界,危险命令强制确认)。"""
    try:
        called = {"request": False, "wait": False}

        def _fake_request(tid, desc):
            called["request"] = True

        def _fake_wait(tid, cid):
            called["wait"] = True
            return True

        monkeypatch.setattr(sandbox_tools, "request_command_confirm", _fake_request)
        monkeypatch.setattr(sandbox_tools, "wait_for_command_confirm", _fake_wait)
        # 模拟 subprocess.run 避免实际执行危险命令
        fake_result = MagicMock(stdout="done", stderr="", returncode=0)
        monkeypatch.setattr(sandbox_tools.subprocess, "run", lambda *a, **kw: fake_result)

        result = sandbox_tools.run_command(
            "rm -rf /tmp/test", task_id=task_id, command_confirm_mode="always_approve",
        )
        assert called["request"] is True
        assert called["wait"] is True
        assert result["exit_code"] == 0
    finally:
        _cleanup(task_id)


def test_local_dangerous_per_command_confirms(local_mode, task_id, monkeypatch):
    """local + per_command + dangerous → 推确认(与 always_approve 同行为)。"""
    try:
        called = {"request": False}

        def _fake_request(tid, desc):
            called["request"] = True

        monkeypatch.setattr(sandbox_tools, "request_command_confirm", _fake_request)
        monkeypatch.setattr(sandbox_tools, "wait_for_command_confirm", lambda tid, cid: True)
        fake_result = MagicMock(stdout="ok", stderr="", returncode=0)
        monkeypatch.setattr(sandbox_tools.subprocess, "run", lambda *a, **kw: fake_result)

        sandbox_tools.run_command(
            "sudo rm -rf /", task_id=task_id, command_confirm_mode="per_command",
        )
        assert called["request"] is True
    finally:
        _cleanup(task_id)


def test_local_safe_skips_confirm(local_mode, task_id, monkeypatch):
    """local + safe 命令 → 不推确认。"""
    try:
        called = {"request": False}

        def _fail_request(tid, desc):
            called["request"] = True
            pytest.fail("safe 命令不应推确认")

        monkeypatch.setattr(sandbox_tools, "request_command_confirm", _fail_request)
        fake_result = MagicMock(stdout="", stderr="", returncode=0)
        monkeypatch.setattr(sandbox_tools.subprocess, "run", lambda *a, **kw: fake_result)

        sandbox_tools.run_command(
            "git status", task_id=task_id, command_confirm_mode="per_command",
        )
        assert called["request"] is False
    finally:
        _cleanup(task_id)


def test_local_dangerous_user_rejects(local_mode, task_id, monkeypatch):
    """local + dangerous + 用户拒绝 → 返回 [用户拒绝执行此命令]。"""
    try:
        monkeypatch.setattr(sandbox_tools, "request_command_confirm", lambda tid, desc: None)
        monkeypatch.setattr(sandbox_tools, "wait_for_command_confirm", lambda tid, cid: False)

        result = sandbox_tools.run_command(
            "rm -rf /", task_id=task_id, command_confirm_mode="per_command",
        )
        assert result["exit_code"] == -1
        assert "用户拒绝" in result["output"]
        assert result["truncated"] is False
    finally:
        _cleanup(task_id)
