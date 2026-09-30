"""acp_base kimi TodoList → plan 事件单元测试

覆盖:
- _parse_todo_plan:rawInput 优先 / input_text 回退 / json_repair 容错 /
  空 todos 返回 None / status 归一(completed→done、非法→pending)
- _handle_tool_result:TodoList completed 推送 plan 事件并记录 last_todo_plan,
  非 TodoList 工具不推送
- 落库 tool_call content 首行以 [TodoList] 结尾(前端历史回放锚点)

测试数据取自真实任务日志(6c40c955 kimi code CLI TodoList_20)。
不连真实 DB:monkeypatch _add_conversation 与 publish。
"""
import json
import uuid
from unittest.mock import MagicMock

import pytest

from app.agents import acp_base
from app.agents.acp_base import _ACPCollector

# 真实日志中的 TodoList rawInput(kimi code CLI v0.35.0)
REAL_TODO_INPUT = {
    "todos": [
        {"title": "Explore plugin system: Cordis Service/Event/Waterfall and lifecycle", "status": "in_progress"},
        {"title": "Explore code quality: TS config, lint, test, AGENTS conventions", "status": "pending"},
        {"title": "Explore security: Landlock sandbox, input validation, XSS/SSRF/JWT/postMessage", "status": "pending"},
        {"title": "Explore performance: caching, queues, logging, graceful shutdown", "status": "pending"},
        {"title": "Explore tech stack: pnpm workspace, CI/CD, Docker, build/release", "status": "pending"},
        {"title": "Synthesize actionable insights and write final report", "status": "pending"},
    ]
}


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def mock_task():
    task = MagicMock()
    task.id = f"test-task-{uuid.uuid4()}"
    return task


@pytest.fixture
def captured(mock_task, monkeypatch):
    """构造 collector,mock 掉落库与 SSE,捕获 publish 事件与落库 content"""
    events: list[tuple[str, dict]] = []
    saved: list[dict] = []

    def fake_add_conversation(db, task, *, round_idx, role, type, content, **kw):
        conv = MagicMock()
        conv.id = f"conv-{uuid.uuid4()}"
        saved.append({"type": type, "content": content})
        return conv

    monkeypatch.setattr(acp_base, "_add_conversation", fake_add_conversation)
    monkeypatch.setattr(
        acp_base, "publish",
        lambda task_id, event_type, data: events.append((event_type, data)),
    )
    c = _ACPCollector(mock_task, MagicMock(db=True), round_idx=2)
    return c, events, saved


# ============================================================
# _parse_todo_plan
# ============================================================

def test_parse_raw_input_priority():
    """rawInput 结构化优先(input_text 为干扰项时仍以 rawInput 为准)"""
    steps = _ACPCollector._parse_todo_plan(REAL_TODO_INPUT, '{"todos": []}')
    assert steps is not None
    assert len(steps) == 6
    assert steps[0] == {
        "id": 1,
        "text": "Explore plugin system: Cordis Service/Event/Waterfall and lifecycle",
        "status": "in_progress",
    }
    assert all(s["status"] == "pending" for s in steps[1:])


def test_parse_input_text_fallback():
    """无 rawInput(kimi 主路径):从累积 input_text 解析"""
    steps = _ACPCollector._parse_todo_plan(None, json.dumps(REAL_TODO_INPUT))
    assert steps is not None
    assert [s["id"] for s in steps] == [1, 2, 3, 4, 5, 6]


def test_parse_json_repair_tolerant():
    """input_text 轻微破损(尾逗号)时 json_repair 容错"""
    broken = '{"todos": [{"title": "a", "status": "done"},]}'
    steps = _ACPCollector._parse_todo_plan(None, broken)
    assert steps == [{"id": 1, "text": "a", "status": "done"}]


def test_parse_empty_todos_returns_none():
    """查询模式(无 todos)/清空模式(空数组)不产生 plan"""
    assert _ACPCollector._parse_todo_plan({}, "") is None
    assert _ACPCollector._parse_todo_plan(None, "{}") is None
    assert _ACPCollector._parse_todo_plan({"todos": []}, "") is None
    assert _ACPCollector._parse_todo_plan(None, '{"todos": []}') is None


def test_parse_status_normalization():
    """completed → done;非法状态 → pending"""
    raw = {"todos": [
        {"title": "a", "status": "completed"},
        {"title": "b", "status": "weird"},
        {"title": "c"},
    ]}
    steps = _ACPCollector._parse_todo_plan(raw, "")
    assert steps is not None
    assert [s["status"] for s in steps] == ["done", "pending", "pending"]


def test_parse_skips_invalid_items():
    """无 title / 非 dict 项跳过;全部无效返回 None"""
    raw = {"todos": [{"status": "pending"}, "not-dict", {"title": "ok", "status": "done"}]}
    steps = _ACPCollector._parse_todo_plan(raw, "")
    assert steps == [{"id": 1, "text": "ok", "status": "done"}]
    assert _ACPCollector._parse_todo_plan({"todos": [{"status": "pending"}]}, "") is None


# ============================================================
# _handle_tool_result:TodoList completed 推送 plan
# ============================================================

def test_todolist_completed_publishes_plan(captured):
    c, events, _ = captured
    # tool_call(title=TodoList → tool_name 推断为 TodoList)
    c._handle_tool_call({
        "toolCallId": "1:TodoList_20",
        "kind": "other",
        "title": "TodoList",
        "sessionUpdate": "tool_call",
    })
    # kimi 参数经 in_progress 累积
    c._accumulate_tool_input("1:TodoList_20", json.dumps(REAL_TODO_INPUT))
    events.clear()

    c._handle_tool_result("1:TodoList_20", {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "1:TodoList_20",
        "status": "completed",
        "rawOutput": "Todo list updated.",
    })

    plan_events = [d for t, d in events if t == "plan"]
    assert len(plan_events) == 1
    assert plan_events[0]["round_idx"] == 2
    assert len(plan_events[0]["steps"]) == 6
    assert plan_events[0]["steps"][0]["status"] == "in_progress"
    # last_todo_plan 已记录(供收尾续接 current_plan)
    assert c.last_todo_plan == plan_events[0]["steps"]
    # pending 已清理
    assert "1:TodoList_20" not in c._pending_tool_calls


def test_todolist_completed_with_raw_input(captured):
    """completed update 直接携带 rawInput 时同样解析"""
    c, events, _ = captured
    c._handle_tool_call({
        "toolCallId": "1:TodoList_9",
        "kind": "other",
        "title": "TodoList",
        "sessionUpdate": "tool_call",
    })
    events.clear()
    c._handle_tool_result("1:TodoList_9", {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "1:TodoList_9",
        "status": "completed",
        "rawInput": REAL_TODO_INPUT,
        "rawOutput": "Todo list updated.",
    })
    plan_events = [d for t, d in events if t == "plan"]
    assert len(plan_events) == 1
    assert len(plan_events[0]["steps"]) == 6


def test_todolist_query_mode_no_plan(captured):
    """查询模式(input 无 todos)completed 不推 plan"""
    c, events, _ = captured
    c._handle_tool_call({
        "toolCallId": "1:TodoList_30",
        "kind": "other",
        "title": "TodoList",
        "sessionUpdate": "tool_call",
    })
    c._accumulate_tool_input("1:TodoList_30", "{}")
    events.clear()
    c._handle_tool_result("1:TodoList_30", {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "1:TodoList_30",
        "status": "completed",
        "rawOutput": "Todo list is empty.",
    })
    assert [t for t, _ in events if t == "plan"] == []
    assert c.last_todo_plan is None


def test_non_todolist_tool_no_plan(captured):
    """普通工具 completed 不触发 plan 推送"""
    c, events, _ = captured
    c._handle_tool_call({
        "toolCallId": "1:Read_26",
        "kind": "read",
        "title": "read: /home/user/repos/proj/tsconfig.base.json",
        "locations": [{"path": "/home/user/repos/proj/tsconfig.base.json"}],
        "sessionUpdate": "tool_call",
    })
    events.clear()
    c._handle_tool_result("1:Read_26", {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "1:Read_26",
        "status": "completed",
        "rawOutput": "1\t{...}",
    })
    assert [t for t, _ in events if t == "plan"] == []


def test_todolist_tool_call_content_anchor(captured):
    """落库 tool_call content 首行以 [TodoList] 结尾(前端回放锚点),detail 为入参 JSON

    completed 时的补全更新走 db.query(...).update() 路径,从 mock db 捕获。
    """
    c, _, saved = captured
    updated: dict = {}

    def fake_update(values):
        updated.update(values)
        return 1

    c.db.query.return_value.filter.return_value.update = fake_update

    c._handle_tool_call({
        "toolCallId": "1:TodoList_20",
        "kind": "other",
        "title": "TodoList",
        "sessionUpdate": "tool_call",
    })
    c._accumulate_tool_input("1:TodoList_20", json.dumps(REAL_TODO_INPUT))
    c._handle_tool_result("1:TodoList_20", {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "1:TodoList_20",
        "status": "completed",
        "rawOutput": "Todo list updated.",
    })
    tool_calls = [s for s in saved if s["type"] == "tool_call"]
    assert len(tool_calls) == 1
    # completed 时以累积 input_text 补全后的最终 content
    content = updated["content"]
    first_line, detail = content.split("\n", 1)
    assert first_line.endswith("[TodoList]")
    assert json.loads(detail) == REAL_TODO_INPUT


# ============================================================
# _format_plan_reminder:格式中立措辞
# ============================================================

def test_plan_reminder_format_neutral():
    reminder = acp_base._format_plan_reminder(
        [{"id": 1, "text": "步骤一", "status": "done"}],
    )
    assert "[done] 步骤一" in reminder
    # 不再强制 <plan> 输出,兼容 CLI 原生计划工具(TodoList)
    assert "TodoList" in reminder or "计划工具" in reminder
