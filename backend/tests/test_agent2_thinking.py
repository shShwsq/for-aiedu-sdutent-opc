"""agent2 真实思考链落库测试(mock LLM 流式,不连真实服务)。

核心约束:agent2 的 LLM reasoning_content 需以 type=thinking 落库,
供前端刷新后还原思考卡片;结构化评估记录(evaluation)仍由 orchestrator
另行落库,两边职责不混。
"""
import json
from unittest.mock import MagicMock

from app.agents.agent2 import run_agent2
from app.models.task import Conversation


class _MockChunk:
    """模拟 LLMClient.chat_stream 产出的 chunk"""

    def __init__(
        self, reasoning_delta="", content_delta="",
        tool_call_deltas=None, finish_reason=None,
    ):
        self.reasoning_delta = reasoning_delta
        self.content_delta = content_delta
        self.tool_call_deltas = tool_call_deltas or []
        self.finish_reason = finish_reason


def _mk_client(chunks):
    client = MagicMock()
    client.chat_stream = MagicMock(return_value=iter(chunks))
    return client


def _mk_task():
    task = MagicMock()
    task.id = "task-1"
    task.verifier_enabled = False
    task.test_env_url = ""
    return task


def _eval_json(**overrides):
    """合法的 agent2 结构化输出 JSON"""
    result = {
        "covered": [],
        "missing": ["d1"],
        "reasoning": "需要先查后端",
        "followup_query": "请检查 backend 目录",
        "done": False,
    }
    result.update(overrides)
    return json.dumps(result, ensure_ascii=False)


def test_thinking_persisted_as_conversation():
    """有 reasoning 增量 → 落库一条 role=agent2/type=thinking 记录。"""
    db = MagicMock()
    chunks = [
        _MockChunk(reasoning_delta="思考第一段。"),
        _MockChunk(reasoning_delta="思考第二段。"),
        _MockChunk(content_delta=_eval_json(), finish_reason="stop"),
    ]
    result = run_agent2(
        "审查这个仓库", [],
        task_id="task-1", db=db, round_idx=1,
        client=_mk_client(chunks), task=_mk_task(),
    )
    assert result["followup_query"] == "请检查 backend 目录"

    added = [c.args[0] for c in db.add.call_args_list]
    thinkings = [
        c for c in added
        if isinstance(c, Conversation) and c.type == "thinking"
    ]
    assert len(thinkings) == 1
    conv = thinkings[0]
    assert conv.role == "agent2"
    assert conv.reasoning == "思考第一段。思考第二段。"
    assert conv.content == ""
    assert conv.round_idx == 1


def test_empty_reasoning_not_persisted():
    """模型无 reasoning 输出(非思考型) → 不落库 thinking 记录。"""
    db = MagicMock()
    chunks = [
        _MockChunk(content_delta=_eval_json(), finish_reason="stop"),
    ]
    run_agent2(
        "审查这个仓库", [],
        task_id="task-1", db=db, round_idx=1,
        client=_mk_client(chunks), task=_mk_task(),
    )
    added = [c.args[0] for c in db.add.call_args_list]
    assert not [
        c for c in added
        if isinstance(c, Conversation) and c.type == "thinking"
    ]


def test_no_db_no_task_still_returns_result():
    """db/task 缺失(兜底场景) → 不落库但评估结果正常返回。"""
    chunks = [
        _MockChunk(reasoning_delta="思考内容"),
        _MockChunk(content_delta=_eval_json(), finish_reason="stop"),
    ]
    result = run_agent2(
        "审查这个仓库", [],
        task_id="task-1", db=None, round_idx=1,
        client=_mk_client(chunks), task=None,
    )
    assert result["followup_query"] == "请检查 backend 目录"


def test_parse_failure_fallback_shows_raw_output():
    """JSON 解析失败 → 强制 done,reasoning 含输出原文(最终总结展示用)。"""
    raw = "这段不是 JSON:审计发现三个高危问题……"
    chunks = [
        _MockChunk(content_delta=raw, finish_reason="stop"),
    ]
    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "第一轮总结"}],
        task_id="task-1", db=None, round_idx=1,
        client=_mk_client(chunks), task=None,
    )
    assert result["done"] is True
    assert "agent2 输出解析失败" in result["reasoning"]
    assert "[agent2 输出原文]" in result["reasoning"]
    assert raw in result["reasoning"]


def test_parse_failure_raw_output_truncated():
    """原文超长 → 截断到上限并带截断提示,避免总结卡片过大。"""
    from app.agents.agent2 import MAX_RAW_OUTPUT_CHARS

    raw = "长" * (MAX_RAW_OUTPUT_CHARS + 2000)
    chunks = [
        _MockChunk(content_delta=raw, finish_reason="stop"),
    ]
    result = run_agent2(
        "审查这个仓库", [],
        task_id="task-1", db=None, round_idx=1,
        client=_mk_client(chunks), task=None,
    )
    reasoning = result["reasoning"]
    assert "原文过长已截断" in reasoning
    # 截断后不再携带全量原文
    assert raw not in reasoning
