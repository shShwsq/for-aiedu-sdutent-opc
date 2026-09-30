"""agent_interrupt 单元测试:per-task 中断队列 + 打断计数器(纯内存,不连 DB)。

覆盖:
- push/drain/has_pending/clear 行为
- push 新替旧语义:队列有未 drain 项时新中断替换旧条目
- drain 后队列清空(不会重复处理)
- 不存在的 task drain 返回空列表(惰性创建)
- peek/cancel:只读副本不消费 / 原子取消与 drain 互斥
- 打断计数器:per-task + per-round 分别计数,decrement 回补不低于 0
- 清理函数:clear_interrupts / clear_interrupt_count 互不影响
"""
import uuid

import pytest

from app.agent_interrupt import (
    cancel_pending_interrupt,
    clear_interrupt_count,
    clear_interrupts,
    decrement_interrupt_count,
    drain_interrupts,
    get_interrupt_count,
    has_pending_interrupts,
    increment_interrupt_count,
    peek_pending_interrupt,
    push_interrupt,
)


@pytest.fixture
def task_id():
    """每个测试用唯一 task_id,避免污染全局 _queues / _counters 字典。"""
    return f"test-interrupt-{uuid.uuid4()}"


# ============================================================
# 队列行为:push / drain / has_pending / clear
# ============================================================

def test_drain_empty_returns_empty_list(task_id):
    """未 push 过的 task drain 应返回空列表(惰性创建队列)。"""
    assert drain_interrupts(task_id) == []
    assert has_pending_interrupts(task_id) is False


def test_push_then_drain_returns_item(task_id):
    """push 后 drain 应返回该中断条目。"""
    push_interrupt(task_id, query="q1", reason="r1", iteration=3)

    items = drain_interrupts(task_id)
    assert len(items) == 1
    assert items[0]["query"] == "q1"
    assert items[0]["reason"] == "r1"
    assert items[0]["iteration"] == 3
    # created_at 应是 ISO 字符串
    assert "T" in items[0]["created_at"]


def test_push_replaces_undrained_item(task_id):
    """新替旧:队列有未 drain 的旧中断时,新 push 直接替换旧条目。

    后一次检查点评估快照更新,旧指令通常已过时;drain 只拿到最新的。
    """
    push_interrupt(task_id, query="q-old", reason="r-old", iteration=3)
    push_interrupt(task_id, query="q-new", reason="r-new", iteration=6)

    items = drain_interrupts(task_id)
    assert len(items) == 1
    assert items[0]["query"] == "q-new"
    assert items[0]["reason"] == "r-new"
    assert items[0]["iteration"] == 6


def test_push_after_drain_is_fresh_append(task_id):
    """drain 清空后再次 push 是新增(无可替换项),不受新替旧影响。"""
    push_interrupt(task_id, query="q1", reason="r1", iteration=3)
    drain_interrupts(task_id)

    push_interrupt(task_id, query="q2", reason="r2", iteration=6)
    items = drain_interrupts(task_id)
    assert len(items) == 1
    assert items[0]["query"] == "q2"


def test_drain_clears_queue(task_id):
    """drain 后队列清空,再次 drain 返回空。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    assert has_pending_interrupts(task_id) is True

    first = drain_interrupts(task_id)
    assert len(first) == 1

    assert has_pending_interrupts(task_id) is False
    second = drain_interrupts(task_id)
    assert second == []


def test_has_pending_reflects_push_state(task_id):
    """has_pending 应反映 push 前后的状态。"""
    assert has_pending_interrupts(task_id) is False
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    assert has_pending_interrupts(task_id) is True


def test_clear_interrupts_removes_queue(task_id):
    """clear_interrupts 应清空队列并移除 task 注册。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    push_interrupt(task_id, query="q2", reason="r2", iteration=6)
    assert has_pending_interrupts(task_id) is True

    clear_interrupts(task_id)
    assert has_pending_interrupts(task_id) is False
    assert drain_interrupts(task_id) == []


def test_push_accepts_uuid_task_id(task_id):
    """push 应接受 UUID 类型 task_id(后端 task.id 是 UUID)。"""
    uid = uuid.uuid4()
    push_interrupt(uid, query="q", reason="r", iteration=3)
    # 用 str 形式也能 drain(后端有时传 str,有时传 UUID)
    items = drain_interrupts(str(uid))
    assert len(items) == 1
    assert items[0]["query"] == "q"


def test_drain_accepts_uuid_task_id(task_id):
    """drain 应接受 UUID 类型 task_id。"""
    uid = uuid.uuid4()
    push_interrupt(str(uid), query="q", reason="r", iteration=3)
    items = drain_interrupts(uid)
    assert len(items) == 1


# ============================================================
# 打断计数器:per-task + per-round
# ============================================================

def test_counter_starts_at_zero(task_id):
    """未 increment 过的 counter 应返回 0。"""
    assert get_interrupt_count(task_id, round_idx=1) == 0
    assert get_interrupt_count(task_id, round_idx=2) == 0


def test_counter_increments_per_round(task_id):
    """counter 应按 round 分别计数,互不影响。"""
    assert increment_interrupt_count(task_id, round_idx=1) == 1
    assert increment_interrupt_count(task_id, round_idx=1) == 2
    assert increment_interrupt_count(task_id, round_idx=2) == 1

    assert get_interrupt_count(task_id, round_idx=1) == 2
    assert get_interrupt_count(task_id, round_idx=2) == 1


def test_clear_interrupt_count_resets_all_rounds(task_id):
    """clear_interrupt_count 应清空所有 round 的计数。"""
    increment_interrupt_count(task_id, round_idx=1)
    increment_interrupt_count(task_id, round_idx=2)
    increment_interrupt_count(task_id, round_idx=2)

    clear_interrupt_count(task_id)
    assert get_interrupt_count(task_id, round_idx=1) == 0
    assert get_interrupt_count(task_id, round_idx=2) == 0


def test_clear_interrupts_does_not_affect_counter(task_id):
    """clear_interrupts 只清队列,不清计数器(两者独立)。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    increment_interrupt_count(task_id, round_idx=1)

    clear_interrupts(task_id)
    # 队列清空,但计数器仍在
    assert has_pending_interrupts(task_id) is False
    assert get_interrupt_count(task_id, round_idx=1) == 1


def test_clear_interrupt_count_does_not_affect_queue(task_id):
    """clear_interrupt_count 只清计数器,不清队列(两者独立)。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    increment_interrupt_count(task_id, round_idx=1)

    clear_interrupt_count(task_id)
    # 计数器清空,但队列仍在
    assert get_interrupt_count(task_id, round_idx=1) == 0
    assert has_pending_interrupts(task_id) is True


# ============================================================
# 多 task 隔离
# ============================================================

def test_queues_isolated_between_tasks():
    """不同 task 的队列应完全隔离,push/drain 互不影响。"""
    tid_a = f"task-a-{uuid.uuid4()}"
    tid_b = f"task-b-{uuid.uuid4()}"

    push_interrupt(tid_a, query="qa", reason="ra", iteration=3)
    push_interrupt(tid_b, query="qb", reason="rb", iteration=3)

    items_a = drain_interrupts(tid_a)
    assert len(items_a) == 1
    assert items_a[0]["query"] == "qa"

    items_b = drain_interrupts(tid_b)
    assert len(items_b) == 1
    assert items_b[0]["query"] == "qb"

    # 再次 drain 都为空
    assert drain_interrupts(tid_a) == []
    assert drain_interrupts(tid_b) == []


def test_counters_isolated_between_tasks():
    """不同 task 的计数器应完全隔离。"""
    tid_a = f"task-ct-a-{uuid.uuid4()}"
    tid_b = f"task-ct-b-{uuid.uuid4()}"

    increment_interrupt_count(tid_a, round_idx=1)
    increment_interrupt_count(tid_a, round_idx=1)
    increment_interrupt_count(tid_b, round_idx=1)

    assert get_interrupt_count(tid_a, round_idx=1) == 2
    assert get_interrupt_count(tid_b, round_idx=1) == 1


# ============================================================
# 队列条目扩展字段:round_idx / eval_conv_id
# ============================================================

def test_push_stores_round_idx_and_eval_conv_id(task_id):
    """push 携带的 round_idx / eval_conv_id 应随条目进入队列(取消时用)。"""
    push_interrupt(
        task_id, query="q", reason="r", iteration=4,
        round_idx=2, eval_conv_id="conv-abc",
    )
    items = drain_interrupts(task_id)
    assert items[0]["round_idx"] == 2
    assert items[0]["eval_conv_id"] == "conv-abc"


def test_push_defaults_round_idx_and_eval_conv_id(task_id):
    """未传 round_idx / eval_conv_id 时兜底 0 / None(兼容旧调用)。"""
    push_interrupt(task_id, query="q", reason="r", iteration=4)
    items = drain_interrupts(task_id)
    assert items[0]["round_idx"] == 0
    assert items[0]["eval_conv_id"] is None


# ============================================================
# peek / cancel:刷新恢复与用户取消
# ============================================================

def test_peek_returns_copy_without_consuming(task_id):
    """peek 返回队列副本且不消费:多次 peek 一致,之后 drain 仍能取到。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)

    first = peek_pending_interrupt(task_id)
    second = peek_pending_interrupt(task_id)
    assert len(first) == 1 and len(second) == 1
    assert first[0]["query"] == "q"
    # 副本与队列内部列表不是同一对象(防外部误改)
    assert first is not second

    assert has_pending_interrupts(task_id) is True
    assert len(drain_interrupts(task_id)) == 1


def test_peek_empty_returns_empty_list(task_id):
    """无待处理中断时 peek 返回空列表。"""
    assert peek_pending_interrupt(task_id) == []


def test_cancel_returns_items_and_clears_queue(task_id):
    """cancel 应返回被取消的中断并清空队列,后续 drain 为空。"""
    push_interrupt(
        task_id, query="q", reason="r", iteration=4,
        round_idx=2, eval_conv_id="conv-abc",
    )

    cancelled = cancel_pending_interrupt(task_id)
    assert len(cancelled) == 1
    assert cancelled[0]["query"] == "q"
    assert cancelled[0]["round_idx"] == 2
    assert cancelled[0]["eval_conv_id"] == "conv-abc"

    # 取消后执行端 drain 到空(不会重复注入)
    assert has_pending_interrupts(task_id) is False
    assert drain_interrupts(task_id) == []
    assert cancel_pending_interrupt(task_id) == []


def test_cancel_empty_returns_empty_list(task_id):
    """无待处理中断(或已被 drain 注入)时 cancel 返回空列表。"""
    assert cancel_pending_interrupt(task_id) == []

    # 已 drain 注入的场景:cancel 竞态输,同样返回空
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    drain_interrupts(task_id)
    assert cancel_pending_interrupt(task_id) == []


def test_cancel_and_drain_are_mutually_exclusive(task_id):
    """cancel 与 drain 只能有一方拿到中断(共用队列锁)。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)

    # 模拟取消赢:cancel 后 drain 必为空
    assert len(cancel_pending_interrupt(task_id)) == 1
    assert drain_interrupts(task_id) == []

    # 模拟注入赢:drain 后 cancel 必为空
    push_interrupt(task_id, query="q2", reason="r2", iteration=6)
    assert len(drain_interrupts(task_id)) == 1
    assert cancel_pending_interrupt(task_id) == []


# ============================================================
# 打断计数回补(decrement)
# ============================================================

def test_decrement_restores_quota(task_id):
    """取消打断后 decrement 应回补本轮配额。"""
    increment_interrupt_count(task_id, round_idx=1)
    increment_interrupt_count(task_id, round_idx=1)
    assert get_interrupt_count(task_id, round_idx=1) == 2

    assert decrement_interrupt_count(task_id, round_idx=1) == 1
    assert get_interrupt_count(task_id, round_idx=1) == 1


def test_decrement_clamped_at_zero(task_id):
    """decrement 不应把计数降到负数(防御重复取消)。"""
    assert decrement_interrupt_count(task_id, round_idx=1) == 0
    assert get_interrupt_count(task_id, round_idx=1) == 0


def test_decrement_only_affects_own_round(task_id):
    """decrement 只影响指定 round,其他 round 计数不变。"""
    increment_interrupt_count(task_id, round_idx=1)
    increment_interrupt_count(task_id, round_idx=2)

    decrement_interrupt_count(task_id, round_idx=1)
    assert get_interrupt_count(task_id, round_idx=1) == 0
    assert get_interrupt_count(task_id, round_idx=2) == 1
