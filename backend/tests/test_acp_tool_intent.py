"""acp_base 工具意图生成单元测试

覆盖:
- _build_tool_intent_detail 的 Read/Grep/Glob 分支(CLI 原生浏览工具可读 intent)
- _handle_tool_call 对 Kimi 风格 title 前缀(read:/terminal:/search:)的归一化:
  无 rawInput 时从 title + locations 提取目标,避免 read: 被误判成"执行命令"

不连真实 DB:monkeypatch _add_conversation 与 publish。
"""
import json
import uuid
from unittest.mock import MagicMock

import pytest

from app.agents import acp_base
from app.agents.acp_base import _ACPCollector


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def mock_task():
    task = MagicMock()
    task.id = f"test-task-{uuid.uuid4()}"
    return task


@pytest.fixture
def collector(mock_task, monkeypatch):
    """构造 collector,mock 掉落库与 SSE 推送,捕获落库 content"""
    saved = {}

    def fake_add_conversation(db, task, *, round_idx, role, type, content):
        conv = MagicMock()
        conv.id = f"conv-{uuid.uuid4()}"
        saved["content"] = content
        saved["type"] = type
        return conv

    monkeypatch.setattr(acp_base, "_add_conversation", fake_add_conversation)
    monkeypatch.setattr(acp_base, "publish", lambda *a, **kw: None)
    c = _ACPCollector(mock_task, MagicMock(), round_idx=1)
    c._saved = saved  # type: ignore[attr-defined]
    return c


# ============================================================
# _build_tool_intent_detail:Read/Grep/Glob 分支
# ============================================================

def test_read_intent():
    intent, detail = _ACPCollector._build_tool_intent_detail(
        "Read", {"file_path": "/home/user/repos/proj/app.py", "limit": 400}, "",
    )
    assert intent == "读取文件 /home/user/repos/proj/app.py [Read]"
    assert json.loads(detail)["limit"] == 400


def test_grep_intent():
    intent, _ = _ACPCollector._build_tool_intent_detail(
        "Grep", {"pattern": "TODO", "path": "/x"}, "",
    )
    assert intent == "搜索代码: TODO [Grep]"


def test_glob_intent():
    intent, _ = _ACPCollector._build_tool_intent_detail(
        "Glob", {"pattern": "tests/**/*"}, "",
    )
    assert intent == "查找文件: tests/**/* [Glob]"


# ============================================================
# _handle_tool_call:Kimi 风格 title 前缀归一化
# ============================================================

def test_kimi_read_title_normalized(collector):
    """read: /path + locations → Read 意图(而非误判成'执行命令')"""
    collector._handle_tool_call({
        "toolCallId": "tc-1",
        "kind": "read",
        "title": "read: /home/user/repos/proj/scripts/cli.py",
        "locations": [{"path": "/home/user/repos/proj/scripts/cli.py"}],
        "sessionUpdate": "tool_call",
    })
    content = collector._saved["content"]  # type: ignore[attr-defined]
    first_line = content.split("\n", 1)[0]
    assert first_line == "读取文件 /home/user/repos/proj/scripts/cli.py [Read]"
    # detail 带 file_path,前端紧凑化可用
    detail = json.loads(content.split("\n", 1)[1])
    assert detail["file_path"] == "/home/user/repos/proj/scripts/cli.py"


def test_kimi_read_title_without_locations(collector):
    """无 locations 时从 title 提取路径"""
    collector._handle_tool_call({
        "toolCallId": "tc-2",
        "kind": "read",
        "title": "read: /x/y.md",
        "sessionUpdate": "tool_call",
    })
    first_line = collector._saved["content"].split("\n", 1)[0]  # type: ignore[attr-defined]
    assert first_line == "读取文件 /x/y.md [Read]"


def test_kimi_terminal_title_normalized(collector):
    """terminal: cmd → Bash 意图带命令(而非空'执行命令')"""
    collector._handle_tool_call({
        "toolCallId": "tc-3",
        "kind": "execute",
        "title": "terminal: git log --oneline -20",
        "sessionUpdate": "tool_call",
    })
    first_line = collector._saved["content"].split("\n", 1)[0]  # type: ignore[attr-defined]
    assert first_line == "执行: git log --oneline -20 [Bash]"


def test_kimi_search_title_normalized(collector):
    collector._handle_tool_call({
        "toolCallId": "tc-4",
        "kind": "search",
        "title": "search: password|secret",
        "sessionUpdate": "tool_call",
    })
    first_line = collector._saved["content"].split("\n", 1)[0]  # type: ignore[attr-defined]
    assert first_line == "搜索代码: password|secret [Grep]"


def test_qoder_read_raw_input(collector):
    """Qoder Read(rawInput 完整)→ 可读 intent 替代'调用 Read'"""
    collector._handle_tool_call({
        "toolCallId": "tc-5",
        "kind": "read",
        "title": "Read /home/user/repos/proj/CONTEXT.md",
        "rawInput": {"file_path": "/home/user/repos/proj/CONTEXT.md"},
        "_meta": {"qoder": {"toolName": "Read"}},
        "sessionUpdate": "tool_call",
    })
    first_line = collector._saved["content"].split("\n", 1)[0]  # type: ignore[attr-defined]
    assert first_line == "读取文件 /home/user/repos/proj/CONTEXT.md [Read]"
