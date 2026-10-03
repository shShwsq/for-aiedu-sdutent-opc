"""agent2 真实思考链落库测试(mock LLM 流式,不连真实服务)。

核心约束:agent2 的 LLM reasoning_content 需以 type=thinking 落库,
供前端刷新后还原思考卡片;结构化审查记录(review)仍由 orchestrator
另行落库,两边职责不混。

另覆盖:agent1 轮次 summary 注入 agent2 prompt 的长度上限
(单条截断 + 总量滑动窗口,多轮 resume 后不无界增长)。
"""
import json
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  单跑本文件时注册 TaskArtifact,Task mapper 才能初始化
import app.agents.agent2 as agent2
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
    """合法的 agent2 结构化输出 JSON(审查模式)"""
    result = {
        "covered": [],
        "missing": ["d1"],
        "reasoning": "需要先查后端",
        "suggestions": [],
        "results": [{"title": "发现", "content": "后端存在未校验输入"}],
        "grouping": None,
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
    assert result["results"][0]["title"] == "发现"

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
    assert result["results"][0]["title"] == "发现"


def test_parse_failure_fallback_shows_raw_output():
    """审查 JSON 解析失败 → parse_failed=True(审查失败,保留临时结果),
    reasoning 含输出原文供回查。"""
    raw = "这段不是 JSON:审计发现三个高危问题……"
    chunks = [
        _MockChunk(content_delta=raw, finish_reason="stop"),
    ]
    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "第一轮总结"}],
        task_id="task-1", db=None, round_idx=1,
        client=_mk_client(chunks), task=None,
    )
    assert result["parse_failed"] is True
    assert result["results"] == []  # 不落空 results(调用方保留临时结果)
    assert "agent2 审查输出解析失败" in result["reasoning"]
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


# ============================================================
# 轮次 summary 注入上限(单条截断 + 总量滑动窗口)
# ============================================================


def _capture_user_msg(monkeypatch):
    """接管 _stream_agent2_llm,捕获发往 LLM 的 user 消息。"""
    captured = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        captured.append(messages[1]["content"])
        return ('{"covered": [], "missing": [], "reasoning": "ok", '
                '"suggestions": [], "results": [], "grouping": null}', [], "")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)
    return captured


def test_summary_injection_truncated_per_round(monkeypatch):
    """单条 summary 超 MAX_HISTORY_MSG_CHARS → 截断后注入。"""
    from app.agents.agent2 import MAX_HISTORY_MSG_CHARS

    captured = _capture_user_msg(monkeypatch)

    long_summary = "长" * (MAX_HISTORY_MSG_CHARS + 500)
    run_agent2(
        "审查这个仓库", [{"round": 1, "summary": long_summary}],
        task_id="task-1", db=None, round_idx=1, client=MagicMock(), task=None,
    )

    user_msg = captured[0]
    assert "长" * 100 in user_msg                      # 内容保留(截断非丢弃)
    assert "长" * (MAX_HISTORY_MSG_CHARS + 1) not in user_msg  # 但不超上限


def test_summary_injection_total_sliding_window(monkeypatch):
    """多轮 summary 总量超 MAX_HISTORY_TOTAL_CHARS → 丢弃最早轮,头部带省略标记。"""
    from app.agents.agent2 import MAX_HISTORY_TOTAL_CHARS

    captured = _capture_user_msg(monkeypatch)

    # 20 轮 × ~1400 字符 ≈ 28000,超 12000 上限 → 只保留最近若干轮
    summaries = [
        {"round": i, "summary": f"第{i}轮结论: " + "x" * 1380}
        for i in range(1, 21)
    ]
    assert sum(len(s["summary"]) for s in summaries) > MAX_HISTORY_TOTAL_CHARS

    run_agent2(
        "审查这个仓库", summaries,
        task_id="task-1", db=None, round_idx=20, client=MagicMock(), task=None,
    )

    user_msg = captured[0]
    assert "已省略" in user_msg            # 头部省略标记
    assert "### 第 20 轮 agent1 自然语言总结" in user_msg   # 最近轮保留
    assert "### 第 1 轮 agent1 自然语言总结" not in user_msg  # 最早轮被丢弃
    # summary 段总量受控(粗略:不含意图/指令头的 x 字符总量不超上限)
    assert user_msg.count("x") <= MAX_HISTORY_TOTAL_CHARS
