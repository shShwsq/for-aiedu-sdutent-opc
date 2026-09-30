"""集成测试:_ACPCollector 检查点触发逻辑 + orchestrator 清理逻辑。

覆盖:
- _ACPCollector._maybe_trigger_checkpoint:K 值触发条件、前 2 迭代跳过、
  仅观察模式(allow_interrupt=false)照常按 K 触发、max_interrupts 上限拒绝
- 触发时机接线:tool_call 落库后不触发,tool_result 落库后才触发
  (保证落库顺序 tool_call → tool_result → 检查点评估,
  前端检查点横线不会把工具结果切到折叠块外)
- orchestrator finally 块的清理函数:clear_interrupts + clear_interrupt_count
  在任务结束时正确清理(push + increment 后调用清理,验证队列/计数归零)

测试不连真实 DB / LLM,用 MagicMock 替换 task/db,验证 checkpoint_callback
是否被调用。

不测试 run_agent2_checkpoint 本身(涉及 LLM 流式调用,已在单元测试
覆盖配置解析 + JSON 解析)。
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.agent_checkpoint import DEFAULT_AGENT_POLICY
from app.agent_interrupt import (
    clear_interrupt_count,
    clear_interrupts,
    drain_interrupts,
    get_interrupt_count,
    has_pending_interrupts,
    increment_interrupt_count,
    push_interrupt,
)
from app.agents.acp_base import _ACPCollector


# 拦截 _add_conversation:避免真实构造 Conversation ORM 实体
# (测试环境未导入全部 models,SQLAlchemy mapper 配置会失败;
# 本测试只关心检查点触发链路,落库细节不在覆盖范围)
_DB_WRITE_PATCH = patch(
    "app.agents.acp_base._add_conversation",
    return_value=MagicMock(id=uuid.uuid4()),
)


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def mock_task():
    """构造 mock task:_ACPCollector 构造只存引用,不调用方法。"""
    task = MagicMock()
    task.id = f"test-task-{uuid.uuid4()}"
    return task


@pytest.fixture(autouse=True)
def _stub_db_write():
    """全局拦截落库(所有用例生效):_handle_tool_result 链路会经 _add_conversation。"""
    with _DB_WRITE_PATCH:
        yield


@pytest.fixture
def mock_db():
    """构造 mock db:落库已被 _stub_db_write 拦截,这里仅承接引用。"""
    return MagicMock()


@pytest.fixture
def checkpoint_callback():
    """mock checkpoint_callback:记录每次被调用时的 iteration 参数。"""
    calls: list[int] = []
    cb = MagicMock(side_effect=lambda iteration, snapshot: calls.append(iteration))
    cb.calls = calls  # type: ignore[attr-defined]
    return cb


def _make_collector(
    mock_task, mock_db, checkpoint_callback, *,
    agent_policy: dict | None = None,
    agent_type: str = "cli",
):
    """构造 _ACPCollector,默认注入 K=3 的策略。"""
    if agent_policy is None:
        agent_policy = {
            **DEFAULT_AGENT_POLICY,
            "checkpoint_interval": 3,
            "allow_interrupt": True,
            "max_interrupts_per_round": 2,
        }
    return _ACPCollector(
        mock_task, mock_db, round_idx=1,
        agent_policy=agent_policy,
        agent_type=agent_type,
        checkpoint_callback=checkpoint_callback,
    )


def _advance_iterations(collector, n: int) -> None:
    """模拟 n 个完整迭代(tool_call + tool_result 均完成)。

    运行时链路:tool_call 事件 → _handle_tool_call(记录 iteration)→
    _start_new_iteration(iteration+1)→ tool_result 事件 →
    _handle_tool_result → _maybe_trigger_checkpoint(调用所属迭代序号)。
    本辅助按该链路逐个迭代驱动,触发点为 iteration ∈ {0..n-1} 中
    满足过滤条件(K 倍数、≥ 2、未达 max_interrupts)的值。
    """
    for i in range(n):
        collector._start_new_iteration()
        tid = f"tc{i}"
        collector._pending_tool_calls[tid] = {
            "title": "t",
            "kind": "other",
            "tool_name": "工具",
            "raw_input": {},
            "input_text": "",
            "conv_id": None,
            "iteration": i,
        }
        collector._handle_tool_result(tid, {"rawOutput": "ok"})


# ============================================================
# _ACPCollector:K 值触发条件
# ============================================================

def test_collector_triggers_at_K_multiples(mock_task, mock_db, checkpoint_callback):
    """K=3 时,iteration=3,6 应触发检查点(前 2 个跳过)。"""
    collector = _make_collector(mock_task, mock_db, checkpoint_callback)

    # 跑 8 个迭代边界:_start_new_iteration 触发点在 iteration=3,6
    _advance_iterations(collector, 8)

    assert checkpoint_callback.calls == [3, 6]
    assert collector.iteration == 8


def test_collector_skips_first_2_iterations(mock_task, mock_db, checkpoint_callback):
    """K=1 时(每迭代都评估),前 2 个迭代仍跳过,从 iteration=2 开始触发。

    注:_maybe_trigger_checkpoint 用 `if iteration < 2: return`,
    所以 iteration=0,1 跳过,iteration=2,3,4,... 满足 K 倍数时触发。
    K=1 时 iteration=2,3,4,5 都触发。
    """
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 1, "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    _advance_iterations(collector, 6)

    # iteration=0,1 跳过(< 2);iteration=2,3,4,5 触发(K=1,都满足 %1==0)
    assert checkpoint_callback.calls == [2, 3, 4, 5]


def test_collector_skips_non_K_multiples(mock_task, mock_db, checkpoint_callback):
    """K=3 时,iteration=4,5 不触发(不是 3 的倍数)。"""
    collector = _make_collector(mock_task, mock_db, checkpoint_callback)
    _advance_iterations(collector, 5)

    # 只在 iteration=3 触发
    assert checkpoint_callback.calls == [3]


def test_collector_K_2_triggers_at_2_4_6(mock_task, mock_db, checkpoint_callback):
    """K=2 时,iteration=2,4,6 触发。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 2, "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    _advance_iterations(collector, 8)
    assert checkpoint_callback.calls == [2, 4, 6]


# ============================================================
# _ACPCollector:触发时机接线(tool_result 后才触发)
# ============================================================

def test_checkpoint_not_triggered_at_tool_call(mock_task, mock_db, checkpoint_callback):
    """tool_call 落库 + _start_new_iteration 后不应触发检查点。

    旧实现在 _start_new_iteration 里触发,导致落库顺序为
    tool_call → 检查点评估 → tool_result,前端检查点横线会把
    工具结果切到折叠块外。现在 _start_new_iteration 不再触发。
    """
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 1,
                      "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    # 模拟迭代 2 的 tool_call 到达(满足 K=1 且 ≥2,旧实现会在此触发)
    for _ in range(3):
        collector._start_new_iteration()

    assert checkpoint_callback.calls == []


def test_checkpoint_triggered_after_tool_result_with_call_iteration(
    mock_task, mock_db, checkpoint_callback,
):
    """tool_result 落库后触发,且 iteration 用 tool_call 所属迭代序号
    (而非已 +1 的 self.iteration)。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 1,
                      "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    # 迭代 0,1 不触发(< 2);迭代 2 的 tool_result 完成后应触发且 iteration=2
    for i in range(3):
        collector._start_new_iteration()
        tid = f"tc{i}"
        collector._pending_tool_calls[tid] = {
            "title": "t", "kind": "other", "tool_name": "工具",
            "raw_input": {}, "input_text": "", "conv_id": None,
            "iteration": i,
        }
        collector._handle_tool_result(tid, {"rawOutput": "ok"})

    assert checkpoint_callback.calls == [2]
    # 触发后 pending 已清理(防止重复 completed 重复触发)
    assert "tc2" not in collector._pending_tool_calls


def test_collector_triggers_once_per_iteration_for_multiple_tools(
    mock_task, mock_db, checkpoint_callback,
):
    """同一迭代(决策回合)内多个工具结果共享迭代号,检查点只评估一次。

    回归:CLI 并行/连续工具调用不新开迭代,多个 tool_result 以相同
    iteration 命中 K 边界,旧实现每个 tool_result 都触发一次评估,
    导致同一检查点重复落库/推送(前端右侧栏重复显示)。
    """
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 3,
                      "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    # 推进到迭代 3(0,1 跳过;3 % 3 == 0 命中 K 边界)
    for _ in range(3):
        collector._start_new_iteration()
    # 迭代 3 内发起 3 个工具(模拟并行),结果依次完成
    for tid in ("tc-a", "tc-b", "tc-c"):
        collector._pending_tool_calls[tid] = {
            "title": "t", "kind": "other", "tool_name": "工具",
            "raw_input": {}, "input_text": "", "conv_id": None,
            "iteration": 3,
        }
        collector._handle_tool_result(tid, {"rawOutput": "ok"})

    # 只有第一个工具结果触发评估,同迭代后续结果被防重拦截
    assert checkpoint_callback.calls == [3]

    # 后续迭代不受影响:迭代 6 仍正常触发
    for i in range(4, 7):
        collector._start_new_iteration()
        tid = f"tc{i}"
        collector._pending_tool_calls[tid] = {
            "title": "t", "kind": "other", "tool_name": "工具",
            "raw_input": {}, "input_text": "", "conv_id": None,
            "iteration": i,
        }
        collector._handle_tool_result(tid, {"rawOutput": "ok"})
    assert checkpoint_callback.calls == [3, 6]


def test_decision_round_based_iteration(mock_task, mock_db, checkpoint_callback):
    """迭代=决策回合:文本段开始才开新迭代,无文本的连续工具调用不递增。

    模拟 codex 事件流:文本段 → 多个无文本工具调用 → 文本段:
    - 第一段思考文本(连续 chunk 只开一次迭代)→ iteration=1
    - 无文本的连续工具调用不产生新迭代,归入迭代 1
    - 第二段对话文本 → iteration=2,其工具结果用 2 触发检查点
    """
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 1,
                      "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    with patch("app.agents.acp_base.publish"):
        # 第一段思考文本:连续 chunk 只开一次迭代
        collector._handle_thinking("先看一下仓库结构。")
        collector._handle_thinking("继续读关键文件。")
        assert collector.iteration == 1
        assert collector._iter_started is True

        # 无文本的连续工具调用:结束当前段但不递增迭代
        # (模拟 __call__ 的 tool_call 分支:_flush_iteration + _handle_tool_call)
        collector._flush_iteration()
        collector._handle_tool_call({"toolCallId": "tc0", "title": "Bash", "kind": "execute"})
        collector._flush_iteration()
        collector._handle_tool_call({"toolCallId": "tc1", "title": "Read", "kind": "other"})
        assert collector.iteration == 1
        assert collector._iter_started is False

        # 两个工具结果都归入迭代 1(iteration=1 < 2,检查点跳过)
        collector._handle_tool_result("tc0", {"rawOutput": "ok"})
        collector._handle_tool_result("tc1", {"rawOutput": "ok"})
        assert checkpoint_callback.calls == []

        # 第二段对话文本 → 迭代 2
        collector._handle_text("发现关键问题,继续验证。")
        assert collector.iteration == 2

        # 迭代 2 的工具调用 → 检查点用 2 触发(K=1)
        collector._handle_tool_call({"toolCallId": "tc2", "title": "Grep", "kind": "other"})
        collector._handle_tool_result("tc2", {"rawOutput": "found"})
        assert checkpoint_callback.calls == [2]
        assert collector.iteration == 2  # 工具调用不递增迭代


def test_tool_result_persisted_with_tool_call_id(mock_task, mock_db, checkpoint_callback):
    """tool_result 落库时携带对应 tool_call 会话记录的 id。

    CLI 并行调用时 result 按完成顺序落库(不再紧跟 call),
    前端靠 tool_call_id 精确配对;conv_id 缺失时留空(前端回退相邻配对)。
    """
    collector = _make_collector(mock_task, mock_db, checkpoint_callback)
    call_conv_id = uuid.uuid4()
    collector._pending_tool_calls["tc0"] = {
        "title": "t", "kind": "other", "tool_name": "工具",
        "raw_input": {}, "input_text": "", "conv_id": call_conv_id,
        "iteration": 0,
    }
    with patch("app.agents.acp_base._add_conversation") as mock_add:
        mock_add.return_value = MagicMock(id=uuid.uuid4())
        collector._handle_tool_result("tc0", {"rawOutput": "ok"})

    assert mock_add.call_count == 1
    kwargs = mock_add.call_args.kwargs
    assert kwargs["type"] == "tool_result"
    assert kwargs["tool_call_id"] == str(call_conv_id)


def test_tool_result_without_conv_id_persisted_with_none(mock_task, mock_db, checkpoint_callback):
    """pending 无 conv_id(异常兜底)时 tool_call_id 留空,前端回退相邻配对。"""
    collector = _make_collector(mock_task, mock_db, checkpoint_callback)
    collector._pending_tool_calls["tc0"] = {
        "title": "t", "kind": "other", "tool_name": "工具",
        "raw_input": {}, "input_text": "", "conv_id": None,
        "iteration": 0,
    }
    with patch("app.agents.acp_base._add_conversation") as mock_add:
        mock_add.return_value = MagicMock(id=uuid.uuid4())
        collector._handle_tool_result("tc0", {"rawOutput": "ok"})

    assert mock_add.call_args.kwargs["tool_call_id"] is None


def test_checkpoint_snapshot_uses_finished_iteration_thinking(
    mock_task, mock_db, checkpoint_callback,
):
    """快照 thinking_summary 用已结束迭代的摘要(由 _flush_iteration 暂存),
    tool_result_summary 是当前工具的真实结果(不再是上一工具的过期结果)。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 1,
                      "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    # 迭代 0,1 跳过(<2),只跑到迭代 2
    with patch("app.agents.acp_base.publish"):
        for i in range(3):
            collector.content_buf = f"thinking-{i}"
            collector._last_tool_intent = f"intent-{i}"
            collector._iter_started = True
            collector._flush_iteration()  # 结束迭代时暂存 thinking 摘要
            tid = f"tc{i}"
            collector._pending_tool_calls[tid] = {
                "title": "t", "kind": "other", "tool_name": "工具",
                "raw_input": {}, "input_text": "", "conv_id": None,
                "iteration": i,
            }
            collector._handle_tool_result(tid, {"rawOutput": f"result-{i}"})

    assert checkpoint_callback.calls == [2]
    snapshot = checkpoint_callback.call_args[0][1]
    assert snapshot["thinking_summary"] == "thinking-2"
    assert snapshot["tool_intent"] == "intent-2"
    assert snapshot["tool_result_summary"] == "result-2"


# ============================================================
# _ACPCollector:allow_interrupt / 无策略 禁用
# ============================================================

def test_collector_disabled_when_no_policy(mock_task, mock_db, checkpoint_callback):
    """无 agent_policy(传 None)时,检查点完全不触发。"""
    # 直接构造,不通过 _make_collector(后者会把 None 填成默认策略)
    collector = _ACPCollector(
        mock_task, mock_db, round_idx=1,
        agent_policy=None,
        agent_type="cli",
        checkpoint_callback=checkpoint_callback,
    )
    _advance_iterations(collector, 10)
    assert checkpoint_callback.calls == []


def test_collector_still_triggers_when_allow_interrupt_false(mock_task, mock_db, checkpoint_callback):
    """仅观察模式(allow_interrupt=False):评估照常按 K 触发(只记录不干预),
    且打断上限不拦评估。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 3,
                      "allow_interrupt": False, "max_interrupts_per_round": 0},
    )
    _advance_iterations(collector, 10)
    # max_interrupts=0 也拦不住:仅观察模式不 push 中断,评估不被上限拦截
    assert checkpoint_callback.calls == [3, 6, 9]


def test_collector_disabled_when_no_callback(mock_task, mock_db):
    """无 checkpoint_callback 时,_maybe_trigger_checkpoint 直接返回(不报错)。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback=None,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 3, "allow_interrupt": True},
    )
    # 不应抛异常
    _advance_iterations(collector, 6)


# ============================================================
# _ACPCollector:CLI vs 内置 K 值解析
# ============================================================

def test_collector_uses_cli_specific_K(mock_task, mock_db, checkpoint_callback):
    """agent_type=cli 时,优先用 checkpoint_interval_cli。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={
            **DEFAULT_AGENT_POLICY,
            "checkpoint_interval": 3,
            "checkpoint_interval_cli": 2,
            "allow_interrupt": True,
            "max_interrupts_per_round": 100,
        },
        agent_type="cli",
    )
    _advance_iterations(collector, 6)
    # K_cli=2,触发点 iteration=2,4
    assert checkpoint_callback.calls == [2, 4]


def test_collector_uses_builtin_specific_K(mock_task, mock_db, checkpoint_callback):
    """agent_type=builtin 时,优先用 checkpoint_interval_builtin。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={
            **DEFAULT_AGENT_POLICY,
            "checkpoint_interval": 3,
            "checkpoint_interval_builtin": 4,
            "allow_interrupt": True,
            "max_interrupts_per_round": 100,
        },
        agent_type="builtin",
    )
    _advance_iterations(collector, 10)
    # K_builtin=4,触发点 iteration=4,8
    assert checkpoint_callback.calls == [4, 8]


def test_collector_falls_back_to_unified_when_specific_null(mock_task, mock_db, checkpoint_callback):
    """agent_type=cli 但 checkpoint_interval_cli=None → 用统一 K。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={
            **DEFAULT_AGENT_POLICY,
            "checkpoint_interval": 3,
            "checkpoint_interval_cli": None,
            "allow_interrupt": True,
            "max_interrupts_per_round": 100,
        },
        agent_type="cli",
    )
    _advance_iterations(collector, 8)
    # 用统一 K=3,触发点 iteration=3,6
    assert checkpoint_callback.calls == [3, 6]


# ============================================================
# _ACPCollector:max_interrupts 上限(collector 内部 _interrupt_count)
# ============================================================

def test_collector_respects_max_interrupts_via_internal_count(mock_task, mock_db, checkpoint_callback):
    """collector._interrupt_count 达到 max 时,_maybe_trigger_checkpoint 跳过。

    注:实际生产中 _interrupt_count 由 _checkpoint_callback 内的全局计数器
    (increment_interrupt_count)更新,但 collector 自己的 _interrupt_count
    不会被自动更新。本测试手动模拟 collector._interrupt_count 的增长,
    验证 _maybe_trigger_checkpoint 的检查逻辑正确。
    """
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_POLICY_WITH_K1, "max_interrupts_per_round": 2},
    )
    # K=1,max=2:iteration=2,3 触发,iteration=4 应被 max 阻止
    # 但 collector._interrupt_count 不会自动增长,需要手动模拟
    # 在每次 callback 后手动增加 collector._interrupt_count
    def cb_with_increment(iteration, snapshot):
        checkpoint_callback(iteration, snapshot)
        collector._interrupt_count += 1
    collector._checkpoint_callback = cb_with_increment

    _advance_iterations(collector, 6)
    # iteration=2,3 触发(2 次),iteration=4,5 因 _interrupt_count=2 >= max=2 跳过
    assert checkpoint_callback.calls == [2, 3]


# ============================================================
# orchestrator finally 块清理逻辑(直接调用清理函数)
# ============================================================

def test_cleanup_clears_interrupts_and_count_together():
    """模拟 orchestrator finally 块:push + increment 后,clear_* 应同时清理。

    orchestrator.py 的 finally 块顺序调用:
        clear_interrupts(task.id)
        clear_interrupt_count(task.id)
    验证:push + increment 后,两个 clear 都执行,队列与计数器都归零。
    """
    task_id = f"cleanup-test-{uuid.uuid4()}"

    try:
        # 准备:push 2 次(新替旧,队列内只剩最新 1 条),计数器 round 1 有 2 次、round 2 有 1 次
        push_interrupt(task_id, query="q1", reason="r1", iteration=3)
        push_interrupt(task_id, query="q2", reason="r2", iteration=6)
        increment_interrupt_count(task_id, round_idx=1)
        increment_interrupt_count(task_id, round_idx=1)
        increment_interrupt_count(task_id, round_idx=2)

        assert has_pending_interrupts(task_id) is True
        assert get_interrupt_count(task_id, round_idx=1) == 2
        assert get_interrupt_count(task_id, round_idx=2) == 1

        # 执行清理(模拟 orchestrator finally 块)
        clear_interrupts(task_id)
        clear_interrupt_count(task_id)

        # 验证:队列与计数器都归零
        assert has_pending_interrupts(task_id) is False
        assert drain_interrupts(task_id) == []
        assert get_interrupt_count(task_id, round_idx=1) == 0
        assert get_interrupt_count(task_id, round_idx=2) == 0
    finally:
        # 兜底清理(避免测试间污染)
        clear_interrupts(task_id)
        clear_interrupt_count(task_id)


def test_cleanup_is_idempotent():
    """清理函数幂等:重复调用不报错,空队列/空计数器也能清理。"""
    task_id = f"idempotent-test-{uuid.uuid4()}"
    try:
        # 空队列/空计数器重复清理
        clear_interrupts(task_id)
        clear_interrupts(task_id)
        clear_interrupt_count(task_id)
        clear_interrupt_count(task_id)

        assert has_pending_interrupts(task_id) is False
        assert get_interrupt_count(task_id, round_idx=1) == 0
    finally:
        clear_interrupts(task_id)
        clear_interrupt_count(task_id)


def test_cleanup_one_task_does_not_affect_another():
    """清理 task A 不应影响 task B 的队列/计数器(隔离性)。"""
    task_a = f"task-a-{uuid.uuid4()}"
    task_b = f"task-b-{uuid.uuid4()}"

    try:
        # 两个 task 都有数据
        push_interrupt(task_a, query="qa", reason="ra", iteration=3)
        push_interrupt(task_b, query="qb", reason="rb", iteration=3)
        increment_interrupt_count(task_a, round_idx=1)
        increment_interrupt_count(task_b, round_idx=1)

        # 清理 task_a
        clear_interrupts(task_a)
        clear_interrupt_count(task_a)

        # task_b 不受影响
        assert has_pending_interrupts(task_a) is False
        assert has_pending_interrupts(task_b) is True
        assert get_interrupt_count(task_a, round_idx=1) == 0
        assert get_interrupt_count(task_b, round_idx=1) == 1

        # task_b 队列内容仍可 drain
        items_b = drain_interrupts(task_b)
        assert len(items_b) == 1
        assert items_b[0]["query"] == "qb"
    finally:
        clear_interrupts(task_a)
        clear_interrupts(task_b)
        clear_interrupt_count(task_a)
        clear_interrupt_count(task_b)


# ============================================================
# 辅助常量
# ============================================================

# K=1 的策略(便于测试 max_interrupts):每迭代都评估
DEFAULT_POLICY_WITH_K1 = {
    **DEFAULT_AGENT_POLICY,
    "checkpoint_interval": 1,
    "allow_interrupt": True,
}
