"""agent_checkpoint 单元测试:配置解析 + JSON 解析 + 历史注入(不调 LLM,不连真实 DB)。

覆盖:
- resolve_agent_policy:用户级默认 + 任务级覆盖合并优先级
- get_effective_interval:内置/CLI 各自 K 值解析
- _parse_checkpoint_json:LLM 输出容错解析(带/不带 ```json 包裹、summary 字段)
- _extract_checkpoint_record / _build_history_section:历史评估记录提取与格式化
- DEFAULT_AGENT_POLICY 常量字段完整性
"""
import uuid
from unittest.mock import MagicMock

import pytest

from app.agent_checkpoint import (
    DEFAULT_AGENT_POLICY,
    MAX_HISTORY_RECORDS,
    ROUND_CHECKPOINT_MAX_CHARS,
    ROUND_CHECKPOINT_MAX_RECORDS,
    _build_history_section,
    _extract_checkpoint_record,
    _parse_checkpoint_json,
    build_round_checkpoint_section,
    build_tool_tail_section,
    build_tool_window_section,
    get_effective_interval,
    resolve_agent_policy,
)


# ============================================================
# 测试 fixture
# ============================================================

@pytest.fixture
def fake_task():
    """构造 fake task:user_id=None,params=None(匿名任务,只用默认值)。"""
    task = MagicMock()
    task.user_id = None
    task.params = None
    task.id = "test-task-id"
    task.user_input = "测试用户输入"
    return task


def _make_task_with_overrides(user_id=None, params=None):
    """构造 fake task,指定 user_id 和 params。"""
    task = MagicMock()
    task.user_id = user_id
    task.params = params
    task.id = "test-task-id"
    task.user_input = "测试用户输入"
    return task


def _mock_db_with_policy(policy_row=None):
    """构造 mock db:db.query(AgentPolicy).filter(...).first() 返回 policy_row。

    policy_row=None 表示用户未保存过协作策略(查无记录)。
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = policy_row
    return db


# ============================================================
# DEFAULT_AGENT_POLICY 常量
# ============================================================

def test_default_policy_has_all_required_fields():
    """DEFAULT_AGENT_POLICY 应包含所有必需字段,默认值符合设计。"""
    expected_keys = {
        "agent2_enabled",
        "max_rounds",
        "checkpoint_interval",
        "checkpoint_interval_builtin",
        "checkpoint_interval_cli",
        "allow_interrupt",
        "max_interrupts_per_round",
        "allow_verify",
        "verifier_auth_mode_default",
        "executor_command_confirm_default",
    }
    assert set(DEFAULT_AGENT_POLICY.keys()) == expected_keys
    # 关键默认值(与设计文档 / 前端 DEFAULT_POLICY 对齐)
    assert DEFAULT_AGENT_POLICY["agent2_enabled"] is True
    assert DEFAULT_AGENT_POLICY["max_rounds"] == 4
    assert DEFAULT_AGENT_POLICY["checkpoint_interval"] == 10
    assert DEFAULT_AGENT_POLICY["checkpoint_interval_builtin"] is None
    assert DEFAULT_AGENT_POLICY["checkpoint_interval_cli"] is None
    assert DEFAULT_AGENT_POLICY["allow_interrupt"] is True
    assert DEFAULT_AGENT_POLICY["max_interrupts_per_round"] == 2
    assert DEFAULT_AGENT_POLICY["allow_verify"] is False
    assert DEFAULT_AGENT_POLICY["verifier_auth_mode_default"] == "per_action"
    assert DEFAULT_AGENT_POLICY["executor_command_confirm_default"] == "always_approve"


# ============================================================
# resolve_agent_policy:优先级 DEFAULT > user > task
# ============================================================

def test_resolve_returns_defaults_for_anonymous_task(fake_task):
    """匿名任务(user_id=None)+ 无 params → 纯默认值。"""
    db = _mock_db_with_policy(policy_row=None)
    policy = resolve_agent_policy(fake_task, db)

    # 应等于默认值
    assert policy["checkpoint_interval"] == 10
    assert policy["allow_interrupt"] is True
    assert policy["max_interrupts_per_round"] == 2


def test_resolve_uses_user_level_defaults_when_no_overrides():
    """有用户级默认 + 无任务级覆盖 → 用用户级默认。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    policy_row = MagicMock()
    policy_row.to_dict.return_value = {
        "checkpoint_interval": 5,
        "allow_interrupt": False,
    }
    db = _mock_db_with_policy(policy_row=policy_row)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 5
    assert policy["allow_interrupt"] is False
    # 未覆盖的字段仍用 DEFAULT
    assert policy["max_interrupts_per_round"] == 2


def test_resolve_task_overrides_win_over_user_defaults():
    """任务级覆盖应优先于用户级默认。"""
    task = _make_task_with_overrides(
        user_id=uuid.uuid4(),
        params={"_agent_policy": {"allow_interrupt": False, "checkpoint_interval": 10}},
    )
    policy_row = MagicMock()
    policy_row.to_dict.return_value = {
        "checkpoint_interval": 5,
        "allow_interrupt": True,  # 应被任务级 False 覆盖
        "max_interrupts_per_round": 4,  # 任务级未覆盖,应保留 4
    }
    db = _mock_db_with_policy(policy_row=policy_row)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 10  # 任务级覆盖
    assert policy["allow_interrupt"] is False   # 任务级覆盖
    assert policy["max_interrupts_per_round"] == 4  # 用户级保留


def test_resolve_task_overrides_win_over_defaults_for_anonymous():
    """匿名任务 + 任务级覆盖 → 任务级覆盖 DEFAULT。"""
    task = _make_task_with_overrides(
        user_id=None,
        params={"_agent_policy": {"checkpoint_interval": 7}},
    )
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 7
    assert policy["allow_interrupt"] is True  # 未覆盖,用默认


def test_resolve_handles_saved_default_policy():
    """用户保存过策略但值等于系统默认(to_dict 返回 DEFAULT)→ 结果仍为 DEFAULT。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    policy_row = MagicMock()
    policy_row.to_dict.return_value = dict(DEFAULT_AGENT_POLICY)
    db = _mock_db_with_policy(policy_row=policy_row)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 10
    assert policy["allow_interrupt"] is True


def test_resolve_handles_no_policy_row():
    """用户无 AgentPolicy 行(first() 返回 None)→ 用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 10


def test_resolve_handles_empty_params_dict():
    """task.params = {} (空字典,非 None) → 无任务级覆盖,用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=None, params={})
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 10


def test_resolve_handles_empty_agent_policy_in_params():
    """task.params = {"_agent_policy": {}} (空覆盖) → 用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=None, params={"_agent_policy": {}})
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 10


# ============================================================
# get_effective_interval:K 值解析
# ============================================================

def test_effective_interval_uses_unified_for_builtin_when_no_specific():
    """无内置专用 K → 用统一 K 值。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_builtin": None}
    assert get_effective_interval(policy, "builtin") == 5


def test_effective_interval_uses_unified_for_cli_when_no_specific():
    """无 CLI 专用 K → 用统一 K 值。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_cli": None}
    assert get_effective_interval(policy, "cli") == 5


def test_effective_interval_uses_builtin_specific_when_set():
    """有内置专用 K → 用专用值,忽略统一值。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_builtin": 2}
    assert get_effective_interval(policy, "builtin") == 2


def test_effective_interval_uses_cli_specific_when_set():
    """有 CLI 专用 K → 用专用值,忽略统一值。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_cli": 10}
    assert get_effective_interval(policy, "cli") == 10


def test_effective_interval_builtin_specific_does_not_affect_cli():
    """内置专用 K 不影响 CLI(CLI 应 fallback 到统一值)。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_builtin": 2, "checkpoint_interval_cli": None}
    assert get_effective_interval(policy, "cli") == 5


def test_effective_interval_falls_back_to_default_10_when_missing():
    """policy 完全缺 checkpoint_interval → 用默认值 10。"""
    policy = {}
    assert get_effective_interval(policy, "builtin") == 10
    assert get_effective_interval(policy, "cli") == 10


def test_effective_interval_handles_zero_specific_as_falsy():
    """specific=0 时(falsy)应回退到统一值(与实现 `or base_k` 行为一致)。

    注意:这是设计取舍——0 不能作为有效 K(K=0 会让评估永远不触发),
    所以与 None 同等处理。如果业务需要支持 K=0,需改实现。
    """
    policy = {"checkpoint_interval": 5, "checkpoint_interval_builtin": 0}
    # 0 falsy → 回退到 base_k=5
    assert get_effective_interval(policy, "builtin") == 5


# ============================================================
# _parse_checkpoint_json:LLM 输出容错
# ============================================================

def test_parse_plain_json_interrupt_true():
    """纯 JSON(无 markdown 包裹)解析正常。"""
    content = '{"interrupt": true, "reason": "方向跑偏", "query": "请转向 X"}'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is True
    assert result["reason"] == "方向跑偏"
    assert result["query"] == "请转向 X"


def test_parse_plain_json_interrupt_false():
    """interrupt=false 时 query 应为 None(即使 LLM 输出空字符串)。"""
    content = '{"interrupt": false, "reason": "继续", "query": ""}'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is False
    assert result["reason"] == "继续"
    assert result["query"] is None


def test_parse_json_with_markdown_fence():
    """LLM 输出带 ```json ... ``` 包裹时应正确解析。"""
    content = '```json\n{"interrupt": true, "reason": "r", "query": "q"}\n```'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is True
    assert result["query"] == "q"


def test_parse_json_with_plain_fence():
    """LLM 输出带 ``` ... ``` 包裹(无 json 标记)时应正确解析。"""
    content = '```\n{"interrupt": false, "reason": "r", "query": null}\n```'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is False
    assert result["query"] is None


def test_parse_json_with_extra_whitespace():
    """LLM 输出前后有空白时应正确解析。"""
    content = '\n\n  {"interrupt": true, "reason": "r", "query": "q"}  \n\n'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is True
    assert result["query"] == "q"


def test_parse_json_missing_interrupt_defaults_to_false():
    """LLM 输出缺 interrupt 字段时默认为 False(安全降级)。"""
    content = '{"reason": "r", "query": null}'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is False
    assert result["reason"] == "r"


def test_parse_json_missing_reason_defaults_to_empty():
    """LLM 输出缺 reason 字段时默认为空字符串。"""
    content = '{"interrupt": false, "query": null}'
    result = _parse_checkpoint_json(content)
    assert result["reason"] == ""


def test_parse_json_null_query_becomes_none():
    """LLM 输出 query=null 时解析为 Python None。"""
    content = '{"interrupt": false, "reason": "r", "query": null}'
    result = _parse_checkpoint_json(content)
    assert result["query"] is None


def test_parse_json_truthy_non_bool_interrupt_coerced_to_bool():
    """interrupt 字段非 bool(如 1/0/"true")时强制转换为 bool。"""
    # truthy 值转为 True
    assert _parse_checkpoint_json('{"interrupt": 1, "reason": "r"}')["interrupt"] is True
    assert _parse_checkpoint_json('{"interrupt": "true", "reason": "r"}')["interrupt"] is True
    # falsy 值转为 False
    assert _parse_checkpoint_json('{"interrupt": 0, "reason": "r"}')["interrupt"] is False
    assert _parse_checkpoint_json('{"interrupt": "", "reason": "r"}')["interrupt"] is False


def test_parse_invalid_json_raises():
    """无效 JSON 应抛出异常(由调用方 run_agent2_checkpoint 捕获并降级)。"""
    with pytest.raises(Exception):
        _parse_checkpoint_json("not a json at all")


# ============================================================
# _parse_checkpoint_json:summary 字段(评估摘要)
# ============================================================

def test_parse_summary_field_present():
    """LLM 输出带 summary 字段时应正常解析。"""
    content = '{"interrupt": false, "reason": "r", "query": null, "summary": "正在分析认证模块"}'
    result = _parse_checkpoint_json(content)
    assert result["summary"] == "正在分析认证模块"


def test_parse_summary_missing_falls_back_to_reason():
    """缺 summary 字段(旧格式输出)时回退到 reason。"""
    content = '{"interrupt": false, "reason": "方向正确", "query": null}'
    result = _parse_checkpoint_json(content)
    assert result["summary"] == "方向正确"


def test_parse_summary_truncated_to_100_chars():
    """summary 超长时截断到 100 字符(控制历史注入体积)。"""
    long_summary = "长" * 200
    content = f'{{"interrupt": false, "reason": "r", "query": null, "summary": "{long_summary}"}}'
    result = _parse_checkpoint_json(content)
    assert len(result["summary"]) == 100


# ============================================================
# _extract_checkpoint_record / _build_history_section:历史注入
# ============================================================

def _make_checkpoint_conv(content: str, reasoning: str):
    """构造 fake 检查点评估 Conversation(只需 content/reasoning 两个属性)。"""
    conv = MagicMock()
    conv.content = content
    conv.reasoning = reasoning
    return conv


def test_extract_record_interrupt_true():
    """打断记录:iteration 从 content 前缀解析,字段完整。"""
    conv = _make_checkpoint_conv(
        "[检查点评估 · 第1轮迭代20] 打断\n理由:x\n追问指令:y",
        '{"interrupt": true, "reason": "跑偏", "query": "转向", "summary": "已打断"}',
    )
    record = _extract_checkpoint_record(conv)
    assert record is not None
    assert record["iteration"] == 20
    assert record["interrupt"] is True
    assert record["query"] == "转向"
    assert record["summary"] == "已打断"


def test_extract_record_skips_unparseable_reasoning():
    """reasoning 解析失败的兜底记录应返回 None(跳过不注入)。"""
    conv = _make_checkpoint_conv(
        "[检查点评估 · 第1轮迭代10] 继续\n理由:解析失败",
        "not a json",  # 兜底记录的 reasoning 是原文
    )
    assert _extract_checkpoint_record(conv) is None


def test_extract_record_handles_bad_iteration_prefix():
    """content 前缀无法解析 iteration 时,iteration=None 不影响其余字段。"""
    conv = _make_checkpoint_conv(
        "[检查点评估] 无迭代号",
        '{"interrupt": false, "reason": "r", "query": null}',
    )
    record = _extract_checkpoint_record(conv)
    assert record is not None
    assert record["iteration"] is None


def test_build_history_section_empty():
    """无历史记录时返回空串(不注入段落)。"""
    assert _build_history_section([]) == ""


def test_build_history_section_formats_records():
    """继续/打断两种记录的格式化,含打断指令。"""
    records = [
        {"iteration": 10, "interrupt": False, "reason": "方向正确", "query": None, "summary": "分析认证中"},
        {"iteration": 20, "interrupt": True, "reason": "跑偏", "query": "转向授权", "summary": "已打断"},
    ]
    section = _build_history_section(records)
    assert section.startswith("你之前的评估记录:")
    assert "[迭代10] 继续 | 摘要:分析认证中" in section
    assert "[迭代20] 打断 | 摘要:已打断 | 指令:转向授权" in section


def test_build_history_section_marks_cancelled_interrupt():
    """被用户取消的打断在历史中明确标注,避免 agent2 误以为指令已下发。"""
    records = [
        {"iteration": 20, "interrupt": True, "cancelled": True,
         "reason": "跑偏", "query": "转向授权", "summary": "已打断"},
    ]
    section = _build_history_section(records)
    assert "[迭代20] 打断(用户已取消,指令未下发) | 摘要:已打断 | 指令:转向授权" in section


def test_extract_record_cancelled_flag():
    """评估记录末尾带取消标记时,提取的 record cancelled=True;无标记为 False。"""
    from app.agent_checkpoint import INTERRUPT_CANCEL_MARKER

    reasoning = '{"interrupt": true, "reason": "x", "query": "y", "summary": "s"}'
    cancelled_conv = _make_checkpoint_conv(
        "[检查点评估 · 第1轮迭代20] 打断\n理由:x\n追问指令:y\n" + INTERRUPT_CANCEL_MARKER,
        reasoning,
    )
    record = _extract_checkpoint_record(cancelled_conv)
    assert record is not None
    assert record["cancelled"] is True

    normal_conv = _make_checkpoint_conv(
        "[检查点评估 · 第1轮迭代20] 打断\n理由:x\n追问指令:y",
        reasoning,
    )
    assert _extract_checkpoint_record(normal_conv)["cancelled"] is False


def test_build_history_section_summary_falls_back_to_reason():
    """summary 为空时回退展示 reason。"""
    records = [
        {"iteration": 10, "interrupt": False, "reason": "方向正确", "query": None, "summary": ""},
    ]
    section = _build_history_section(records)
    assert "摘要:方向正确" in section


def test_build_history_section_keeps_only_recent_records():
    """超过 MAX_HISTORY_RECORDS 时只保留最近 N 条。"""
    records = [
        {"iteration": i, "interrupt": False, "reason": f"r{i}", "query": None, "summary": f"s{i}"}
        for i in range(1, MAX_HISTORY_RECORDS + 4)
    ]
    section = _build_history_section(records)
    # 最早的几条被丢弃,最近的保留
    assert f"s{MAX_HISTORY_RECORDS + 3}" in section
    assert "s1" not in section
    assert "s2" not in section


# ============================================================
# run_agent2_checkpoint:仅观察模式(不调真实 LLM,mock 流式调用)
# ============================================================

def _run_checkpoint_with_llm_output(
    fake_task, llm_output: str, *, allow_interrupt: bool, reasoning: str = "",
):
    """辅助:mock 流式调用/落库/历史加载,直接驱动 run_agent2_checkpoint。

    reasoning:模拟检查点评估的思考链(流式调用第二个返回值)。
    返回 (result, messages, conv_cls):conv_cls 为 patch 后的 Conversation 类,
    供断言思考链落库行为(真实实例化会触发 mapper 初始化,测试环境不可用)。
    """
    from unittest.mock import patch

    captured = {}
    db = MagicMock()

    def fake_stream(client, messages, **kwargs):
        captured["messages"] = messages
        return llm_output, reasoning

    with patch("app.agent_checkpoint._stream_checkpoint_llm", side_effect=fake_stream), \
         patch("app.agent_checkpoint._record_checkpoint"), \
         patch("app.agent_checkpoint._load_checkpoint_history", return_value=[]), \
         patch("app.agent_checkpoint.Conversation") as conv_cls:
        import app.agent_checkpoint as acp
        result = acp.run_agent2_checkpoint(
            fake_task, db, round_idx=1, iteration=10,
            react_snapshot={
                "thinking_summary": "t", "tool_intent": "i",
                "tool_result_summary": "r", "plan_status": [],
            },
            client=MagicMock(),
            allow_interrupt=allow_interrupt,
        )
    return result, captured.get("messages", []), conv_cls


def test_checkpoint_observe_mode_downgrades_interrupt(fake_task):
    """仅观察模式:模型未遵守提示仍输出 interrupt=true 时强制降级,reason 标注未干预。"""
    llm_output = '{"interrupt": true, "reason": "方向跑偏", "query": "转向 X", "summary": "跑偏"}'
    result, _, _ = _run_checkpoint_with_llm_output(fake_task, llm_output, allow_interrupt=False)
    assert result["interrupt"] is False
    assert "仅观察模式" in result["reason"]


def test_checkpoint_allowed_mode_keeps_interrupt(fake_task):
    """可打断模式:interrupt=true 照常保留(对照组)。"""
    llm_output = '{"interrupt": true, "reason": "方向跑偏", "query": "转向 X", "summary": "跑偏"}'
    result, _, _ = _run_checkpoint_with_llm_output(fake_task, llm_output, allow_interrupt=True)
    assert result["interrupt"] is True
    assert result["query"] == "转向 X"


def test_checkpoint_observe_mode_prompt_contains_note(fake_task):
    """仅观察模式:user 消息含观察模式提示;可打断模式不含。"""
    llm_output = '{"interrupt": false, "reason": "ok", "query": null, "summary": "s"}'
    _, messages_obs, _ = _run_checkpoint_with_llm_output(fake_task, llm_output, allow_interrupt=False)
    _, messages_allow, _ = _run_checkpoint_with_llm_output(fake_task, llm_output, allow_interrupt=True)
    assert "仅观察模式" in messages_obs[1]["content"]
    assert "仅观察模式" not in messages_allow[1]["content"]


def test_checkpoint_thinking_persisted(fake_task):
    """思考链非空时落库为 role=agent2/type=thinking,content 带检查点前缀(供侧栏还原)。"""
    llm_output = '{"interrupt": false, "reason": "ok", "query": null, "summary": "s"}'
    _, _, conv_cls = _run_checkpoint_with_llm_output(
        fake_task, llm_output, allow_interrupt=True, reasoning="先核对用户意图…",
    )
    assert conv_cls.call_count == 1
    kwargs = conv_cls.call_args.kwargs
    assert kwargs["role"] == "agent2"
    assert kwargs["type"] == "thinking"
    assert kwargs["content"] == "[检查点评估 · 第1轮迭代10]"
    assert kwargs["reasoning"] == "先核对用户意图…"


def test_checkpoint_thinking_empty_not_persisted(fake_task):
    """思考链为空(非思考模型)时不落库 thinking 记录。"""
    llm_output = '{"interrupt": false, "reason": "ok", "query": null, "summary": "s"}'
    _, _, conv_cls = _run_checkpoint_with_llm_output(fake_task, llm_output, allow_interrupt=True)
    assert conv_cls.call_count == 0


# ============================================================
# build_round_checkpoint_section:完整评估注入本轮检查点观察
# ============================================================

def test_round_checkpoint_section_empty_when_no_records(fake_task):
    """本轮无检查点记录 → 返回空串(不注入段落)。"""
    from unittest.mock import patch

    db = MagicMock()
    with patch("app.agent_checkpoint._load_checkpoint_history", return_value=[]):
        assert build_round_checkpoint_section(db, fake_task.id, round_idx=1) == ""


def test_round_checkpoint_section_formats_records(fake_task):
    """继续/打断记录正确格式化,标题为检查点观察段。"""
    from unittest.mock import patch

    records = [
        {"iteration": 5, "interrupt": False, "reason": "方向正确", "query": None, "summary": "分析认证中"},
        {"iteration": 10, "interrupt": True, "reason": "跑偏", "query": "转向授权", "summary": "已打断"},
    ]
    db = MagicMock()
    with patch("app.agent_checkpoint._load_checkpoint_history", return_value=records):
        section = build_round_checkpoint_section(db, fake_task.id, round_idx=1)
    assert section.startswith("[本轮执行中的检查点观察")
    assert "[迭代5] 继续 | 摘要:分析认证中" in section
    assert "[迭代10] 打断 | 摘要:已打断 | 指令:转向授权" in section


def test_round_checkpoint_section_truncates_beyond_limits(fake_task):
    """超条数/字符上限时从最早丢弃,最近的保留。"""
    from unittest.mock import patch

    records = [
        {
            "iteration": i, "interrupt": False, "query": None,
            "reason": "", "summary": f"摘要内容{i}" + "x" * 200,
        }
        for i in range(1, ROUND_CHECKPOINT_MAX_RECORDS + 6)
    ]
    db = MagicMock()
    with patch("app.agent_checkpoint._load_checkpoint_history", return_value=records):
        section = build_round_checkpoint_section(db, fake_task.id, round_idx=1)
    # 条数裁剪:最早的几条被丢
    assert f"摘要内容1x" not in section
    # 字符裁剪后总长不超限(含标题与换行,放宽 500 字符余量)
    assert len(section) <= ROUND_CHECKPOINT_MAX_CHARS + 500
    # 最近一条必在
    assert f"摘要内容{ROUND_CHECKPOINT_MAX_RECORDS + 5}" in section


# ============================================================
# build_tool_window_section / build_tool_tail_section:工具调用窗口
# ============================================================

def _make_tool_conv(type_: str, content: str, created_at=None):
    """构造 fake react_agent tool_call/tool_result Conversation。"""
    conv = MagicMock()
    conv.type = type_
    conv.content = content
    conv.created_at = created_at
    return conv


def _mock_tool_window_db(convs):
    """mock db:无 boundary 时 db.query().filter().order_by().all() 返回 convs。"""
    db = MagicMock()
    q1 = db.query.return_value.filter.return_value
    q1.order_by.return_value.all.return_value = convs
    return db


def test_tool_window_formats_intent_and_truncates_result():
    """tool_call 只取首行意图(参数 JSON 被剥离),tool_result 截断。"""
    convs = [
        _make_tool_conv("tool_call", "搜索 SQL 注入相关代码\n{\"pattern\": \"execute\"}"),
        _make_tool_conv("tool_result", "匹配结果" + "x" * 500),
    ]
    db = _mock_tool_window_db(convs)
    section = build_tool_window_section(db, "t", 1, None, title="[窗口]")
    assert section.startswith("[窗口]")
    assert "- 搜索 SQL 注入相关代码" in section
    assert '"pattern"' not in section  # 参数详情被剥离
    assert "结果摘要:" in section
    assert "[...truncated...]" in section


def test_tool_window_empty_when_no_convs():
    """窗口内无任何工具调用 → 返回空串。"""
    db = _mock_tool_window_db([])
    assert build_tool_window_section(db, "t", 1, None, title="[窗口]") == ""


def test_tool_window_drops_earliest_when_max_calls_exceeded():
    """tool_call 条数超 max_calls 时从最早丢弃,并清理孤立结果。"""
    convs = []
    for i in range(1, 5):  # 4 个 tool_call,各带结果
        convs.append(_make_tool_conv("tool_call", f"意图{i}"))
        convs.append(_make_tool_conv("tool_result", f"结果{i}"))
    db = _mock_tool_window_db(convs)
    section = build_tool_window_section(db, "t", 1, None, title="[窗口]", max_calls=2)
    assert "意图1" not in section
    assert "意图2" not in section
    assert "结果2" not in section  # 对应 call 被丢,孤立结果一并清理
    assert "意图3" in section
    assert "意图4" in section


def test_tool_window_drops_earliest_when_max_chars_exceeded():
    """总长超 max_chars 时从最早丢弃。"""
    convs = [
        _make_tool_conv("tool_call", "早期意图" + "a" * 300),
        _make_tool_conv("tool_call", "近期意图" + "b" * 300),
    ]
    db = _mock_tool_window_db(convs)
    section = build_tool_window_section(db, "t", 1, None, title="[窗口]", max_chars=400)
    assert "早期意图" not in section
    assert "近期意图" in section


def test_tool_window_skips_empty_content():
    """content 为空的记录跳过,不产生空行。"""
    convs = [
        _make_tool_conv("tool_call", ""),
        _make_tool_conv("tool_call", "有效意图"),
    ]
    db = _mock_tool_window_db(convs)
    section = build_tool_window_section(db, "t", 1, None, title="[窗口]")
    assert "有效意图" in section
    assert section.count("\n- ") == 1


def _mock_tail_db(last_ckpt):
    """mock build_tool_tail_section 的 db:检查点查询 first() 返回 last_ckpt;
    窗口查询无 boundary 路径(q1.order_by.all)与有 boundary 路径
    (q1.filter.order_by.all)都返回工具记录。"""
    db = MagicMock()
    q1 = db.query.return_value.filter.return_value
    q1.order_by.return_value.first.return_value = last_ckpt  # 检查点查询
    tool_convs = [_make_tool_conv("tool_call", "窗口内意图")]
    q1.order_by.return_value.all.return_value = tool_convs   # 窗口查询(无 boundary)
    q1.filter.return_value.order_by.return_value.all.return_value = tool_convs  # 窗口查询(有 boundary)
    return db


def test_tool_tail_uses_checkpoint_boundary_when_present():
    """本轮有检查点 → 以最后一条检查点 created_at 为窗口起点(boundary 传入查询)。"""
    from datetime import datetime

    ckpt = MagicMock()
    ckpt.created_at = datetime(2026, 8, 14, 12, 0, 0)
    db = _mock_tail_db(ckpt)
    section = build_tool_tail_section(db, "t", 1)
    assert section.startswith("[最后一次检查点之后的工具调用明细]")
    # 验证 boundary 条件被传入(第二次 filter 调用收到 created_at 条件)
    filter_mock = db.query.return_value.filter.return_value
    assert filter_mock.filter.call_count == 1


def test_tool_tail_full_round_when_no_checkpoint():
    """本轮无检查点 → 窗口=整轮,标题切换为全部明细(截尾)。"""
    db = _mock_tail_db(None)
    section = build_tool_tail_section(db, "t", 1)
    assert section.startswith("[本轮全部工具调用明细(截尾)]")
    assert "窗口内意图" in section


# ============================================================
# 检查点评估注入工具窗口(run_agent2_checkpoint 内注入)
# ============================================================

def test_checkpoint_user_msg_contains_tool_window(fake_task):
    """检查点评估 user_msg 应包含工具窗口段落(位于历史与当前思考之间)。"""
    from unittest.mock import patch

    captured = {}

    def fake_stream(client, messages, **kwargs):
        captured["messages"] = messages
        return '{"interrupt": false, "reason": "ok", "query": null, "summary": "s"}', ""

    db = MagicMock()
    with patch("app.agent_checkpoint._stream_checkpoint_llm", side_effect=fake_stream), \
         patch("app.agent_checkpoint._record_checkpoint"), \
         patch("app.agent_checkpoint._load_checkpoint_history", return_value=[]), \
         patch("app.agent_checkpoint._build_checkpoint_tool_window",
               return_value="[上一次检查点以来的工具调用]\n- 搜索认证代码"), \
         patch("app.agent_checkpoint.Conversation"):
        import app.agent_checkpoint as acp
        acp.run_agent2_checkpoint(
            fake_task, db, round_idx=1, iteration=10,
            react_snapshot={
                "thinking_summary": "当前思考内容", "tool_intent": "i",
                "tool_result_summary": "r", "plan_status": [],
            },
            client=MagicMock(), allow_interrupt=True,
        )
    user_msg = captured["messages"][1]["content"]
    assert "[上一次检查点以来的工具调用]" in user_msg
    assert "- 搜索认证代码" in user_msg
    # 窗口位于当前思考之前
    assert user_msg.index("搜索认证代码") < user_msg.index("当前思考内容")


def test_checkpoint_tool_window_failure_degrades_gracefully(fake_task):
    """工具窗口查库失败 → 降级为空,评估照常完成不报错。"""
    from unittest.mock import patch

    captured = {}

    def fake_stream(client, messages, **kwargs):
        captured["messages"] = messages
        return '{"interrupt": false, "reason": "ok", "query": null, "summary": "s"}', ""

    db = MagicMock()
    with patch("app.agent_checkpoint._stream_checkpoint_llm", side_effect=fake_stream), \
         patch("app.agent_checkpoint._record_checkpoint"), \
         patch("app.agent_checkpoint._load_checkpoint_history", return_value=[]), \
         patch("app.agent_checkpoint._build_checkpoint_tool_window",
               side_effect=RuntimeError("db down")), \
         patch("app.agent_checkpoint.Conversation"):
        import app.agent_checkpoint as acp
        result = acp.run_agent2_checkpoint(
            fake_task, db, round_idx=1, iteration=10,
            react_snapshot={
                "thinking_summary": "t", "tool_intent": "i",
                "tool_result_summary": "r", "plan_status": [],
            },
            client=MagicMock(), allow_interrupt=True,
        )
    assert result["interrupt"] is False
    # 窗口段落未注入(注意 user_msg 本身含"最近工具调用"字样,断言用段落标题)
    assert "[上一次检查点以来的工具调用]" not in captured["messages"][1]["content"]


# ============================================================
# agent2 跨轮记忆排除检查点记录(泄漏修复)
# ============================================================

def test_agent2_history_excludes_checkpoint_records():
    """跨轮记忆不应包含检查点评估记录(其 reasoning 是原始 JSON,属噪声)。"""
    from app.agents.agent2 import _build_agent2_history

    ckpt_conv = MagicMock()
    ckpt_conv.content = "[检查点评估 · 第1轮迭代10] 继续\n理由:ok"
    ckpt_conv.reasoning = '{"interrupt": false, "reason": "ok", "summary": "s"}'
    ckpt_conv.round_idx = 1

    normal_conv = MagicMock()
    normal_conv.content = "评估正文"
    normal_conv.reasoning = "已覆盖: [a]\n未覆盖: []\n判断: → 宣布完成"
    normal_conv.round_idx = 1

    class _FakeQuery:
        def filter(self, *a):
            return self

        def order_by(self, *a):
            return self

        def all(self):
            return [ckpt_conv, normal_conv]

    class _FakeDb:
        def query(self, *a):
            return _FakeQuery()

    history = _build_agent2_history(_FakeDb(), "task-id", current_round_idx=2)
    assert "已覆盖: [a]" in history  # 正常评估记录保留(取 reasoning)
    assert '"interrupt"' not in history  # 检查点原始 JSON 被排除
    assert "检查点评估" not in history


# ============================================================
# 完整评估注入检查点段 + 工具尾部(run_agent2 round≥1)
# ============================================================

def test_run_agent2_injects_checkpoint_and_tail_sections():
    """round≥1 完整评估:user_msg 含本轮检查点观察 + 工具调用明细两段。"""
    from unittest.mock import patch

    from app.agents.agent2 import run_agent2

    captured = {}

    class _FakeChunk:
        reasoning_delta = ""
        content_delta = None
        tool_call_deltas = []
        finish_reason = "stop"

    def _chunks():
        c1 = _FakeChunk()
        c1.content_delta = '{"covered": [], "missing": [], "reasoning": "r", '
        c2 = _FakeChunk()
        c2.content_delta = '"followup_query": "", "done": true}'
        return [c1, c2]

    client = MagicMock()

    def fake_chat_stream(messages, **kw):
        captured["messages"] = messages
        return _chunks()

    client.chat_stream.side_effect = fake_chat_stream

    with patch("app.agent_checkpoint.build_round_checkpoint_section",
               return_value="[本轮执行中的检查点观察]\n- [迭代5] 继续 | 摘要:s"), \
         patch("app.agent_checkpoint.build_tool_tail_section",
               return_value="[最后一次检查点之后的工具调用明细]\n- 读取配置文件"):
        result = run_agent2(
            "审计仓库",
            [{"round": 1, "summary": "发现了注入问题"}],
            task_id="task-1", db=MagicMock(), round_idx=1, client=client,
        )

    user_msg = captured["messages"][1]["content"]
    assert "[本轮执行中的检查点观察]" in user_msg
    assert "[最后一次检查点之后的工具调用明细]" in user_msg
    assert "- 读取配置文件" in user_msg
    assert result["done"] is True


def test_run_agent2_section_failure_degrades_gracefully():
    """检查点/窗口注入失败 → 降级跳过,评估主流程不受影响。"""
    from unittest.mock import patch

    from app.agents.agent2 import run_agent2

    captured = {}

    class _FakeChunk:
        reasoning_delta = ""
        content_delta = None
        tool_call_deltas = []
        finish_reason = "stop"

    def _chunks():
        c = _FakeChunk()
        c.content_delta = '{"covered": [], "missing": [], "reasoning": "r", "followup_query": "q", "done": false}'
        return [c]

    client = MagicMock()

    def fake_chat_stream(messages, **kw):
        captured["messages"] = messages
        return _chunks()

    client.chat_stream.side_effect = fake_chat_stream

    with patch("app.agent_checkpoint.build_round_checkpoint_section",
               side_effect=RuntimeError("db down")):
        result = run_agent2(
            "审计仓库",
            [{"round": 1, "summary": "发现了注入问题"}],
            task_id="task-1", db=MagicMock(), round_idx=1, client=client,
        )

    assert result["done"] is False
    assert "检查点观察" not in captured["messages"][1]["content"]


# ============================================================
# 检查点评估注入 checklist(理解用户意图的完整边界)
# ============================================================

_SAMPLE_CHECKLIST = [
    {"id": "auth", "name": "认证与授权", "description": "登录与会话",
     "checklist": ["登录绕过", "会话固定"]},
    {"id": "injection", "name": "注入类漏洞", "description": "",
     "checklist": ["SQL 注入", "命令注入"]},
    {"id": "config", "name": "配置安全", "description": "", "checklist": []},
]


def test_build_checklist_section_empty_when_no_checklist():
    """无 checklist(None/空列表) → 返回空串(不注入段落)。"""
    from app.agent_checkpoint import _build_checklist_section

    assert _build_checklist_section(None) == ""
    assert _build_checklist_section([]) == ""


def test_build_checklist_section_formats_dimensions():
    """维度名+子项格式化进段落;无子项的维度只有名称;不含 description。"""
    from app.agent_checkpoint import _build_checklist_section

    section = _build_checklist_section(_SAMPLE_CHECKLIST)
    assert section.startswith("已确认的覆盖度清单")
    assert "认证与授权:登录绕过; 会话固定" in section
    assert "注入类漏洞:SQL 注入; 命令注入" in section
    assert "- 配置安全" in section
    assert "登录与会话" not in section  # description 不注入


def test_build_checklist_section_truncates_over_limit():
    """总长超上限时从末尾丢弃维度,保留标题 + 部分维度。"""
    from app.agent_checkpoint import MAX_CHECKLIST_CHARS, _build_checklist_section

    big = [
        {"id": f"d{i}", "name": f"维度{i}", "checklist": [f"检查项{'x' * 80}_{j}" for j in range(3)]}
        for i in range(30)
    ]
    section = _build_checklist_section(big)
    assert section
    assert len(section) <= MAX_CHECKLIST_CHARS + 2  # +\n\n 尾缀
    assert "维度0" in section
    assert "维度29" not in section


def test_checkpoint_user_msg_contains_checklist(fake_task):
    """task.checklist 存在时,检查点评估 user 消息包含清单段落(位于意图之后)。"""
    fake_task.checklist = _SAMPLE_CHECKLIST
    llm_output = '{"interrupt": false, "reason": "ok", "query": null, "summary": "s"}'
    _, messages, _ = _run_checkpoint_with_llm_output(fake_task, llm_output, allow_interrupt=True)
    user_msg = messages[1]["content"]
    assert "已确认的覆盖度清单" in user_msg
    assert "认证与授权:登录绕过; 会话固定" in user_msg
    # 位置:在用户意图之后、轮次信息之前
    assert user_msg.index("已确认的覆盖度清单") < user_msg.index("当前协作轮次")


def test_checkpoint_user_msg_no_checklist_section_when_absent(fake_task):
    """task.checklist 为 None(如第 0 轮尚未确认) → 不注入清单段落。"""
    fake_task.checklist = None
    llm_output = '{"interrupt": false, "reason": "ok", "query": null, "summary": "s"}'
    _, messages, _ = _run_checkpoint_with_llm_output(fake_task, llm_output, allow_interrupt=True)
    assert "已确认的覆盖度清单" not in messages[1]["content"]
