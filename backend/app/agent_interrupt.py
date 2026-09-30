"""agent2 中断队列:检查点评估后产生的打断指令

场景:agent2 在 react_agent 执行过程中(每 K 个迭代边界)做轻量评估,
若判断 react_agent 方向跑偏,生成追问指令入队。react_agent 在下一迭代
边界 drain 出来,作为新的 user 消息注入 LLM 上下文(软中断)。

设计要点:
- 与 user_messages.py 的区别:user_messages 存真实用户消息(优先级高),
  本队列存 agent2 检查点评估后的追问指令(优先级低)。
- drain 顺序:react_agent 迭代边界先 drain_user_messages,再 drain_interrupts,
  保证真实用户消息优先于 agent2 追问。
- 与 user_messages.py 一样,用 per-task 内存队列 + threading.Lock,
  非阻塞 drain,不影响 ReAct 循环节奏。
- 落库与 SSE 推送由 agent_checkpoint.py 完成(检查点评估时),本模块只负责
  in-memory 队列管理。

线程安全:用 threading.Lock 保护 dict + 每个队列内部的 list。
适用单机部署;多实例部署需换 Redis 等共享存储。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class _InterruptQueue:
    """单个 task 的 agent2 中断队列

    队列内元素结构:
        {
            "query": str,        # 追问指令内容(注入 react_agent 的 user 消息)
            "reason": str,       # 打断理由(落库 + 前端展示)
            "iteration": int,    # 触发打断时的迭代序号
            "round_idx": int,    # 触发打断时的协作轮次(取消时回补计数用)
            "eval_conv_id": str | None,  # 检查点评估落库记录 id(取消时追加标记用)
            "created_at": str,   # ISO 时间(用于排序)
        }
    """

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def push(self, item: dict[str, Any]) -> bool:
        """追加中断指令(检查点评估调用),新替旧语义

        队列中尚有未 drain 的旧中断时直接替换:后一次检查点评估的
        快照更新,且其评估时已参考过历史打断记录,旧指令通常已过时。
        返回 True 表示发生了替换。
        """
        with self._lock:
            replaced = len(self._items) > 0
            self._items = [item]
            return replaced

    def drain(self) -> list[dict[str, Any]]:
        """取出并清空所有待处理中断(react_agent 迭代边界调用)

        返回按 push 顺序排列的中断列表(空列表表示无中断)。
        """
        with self._lock:
            items = list(self._items)
            self._items.clear()
        return items

    def has_pending(self) -> bool:
        """是否有待处理中断(快速判断)"""
        with self._lock:
            return len(self._items) > 0

    def clear(self) -> None:
        """清空队列(任务结束/重启时调用)"""
        with self._lock:
            self._items.clear()


# 全局注册表:task_id(str)→ _InterruptQueue
_queues: dict[str, _InterruptQueue] = {}
_queues_lock = threading.Lock()


def _get_or_create(task_id: str) -> _InterruptQueue:
    """获取(或创建)task 的中断队列"""
    with _queues_lock:
        if task_id not in _queues:
            _queues[task_id] = _InterruptQueue()
        return _queues[task_id]


def push_interrupt(
    task_id: str | UUID,
    *,
    query: str,
    reason: str,
    iteration: int,
    round_idx: int = 0,
    eval_conv_id: str | None = None,
) -> None:
    """追加 agent2 中断指令到队列(新替旧)

    队列中尚有未 drain 的旧中断时,新中断直接替换旧条目(后一次评估
    快照更新,旧指令通常已过时)。drain 因此通常只返回 1 条,注入侧的
    多条拼接逻辑保留作防御性兜底。

    参数:
        task_id: 任务 ID
        query: 追问指令内容(将注入 react_agent 的 user 消息)
        reason: 打断理由(落库 + 前端展示)
        iteration: 触发打断时的迭代序号
        round_idx: 触发打断时的协作轮次(取消时回补该轮计数)
        eval_conv_id: 检查点评估落库记录 id(取消时在该记录追加已取消标记)
    """
    queue = _get_or_create(str(task_id))
    replaced = queue.push({
        "query": query,
        "reason": reason,
        "iteration": iteration,
        "round_idx": round_idx,
        "eval_conv_id": eval_conv_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info(
        f"[task={task_id}] agent2 中断入队(iteration={iteration}, "
        f"{'替换未生效旧中断' if replaced else '新增'})"
    )


def drain_interrupts(task_id: str | UUID) -> list[dict[str, Any]]:
    """取出并清空队列(react_agent 迭代边界调用)

    返回中断列表(空列表表示无中断)。取出后队列清空,不会重复处理。
    """
    queue = _get_or_create(str(task_id))
    items = queue.drain()
    if items:
        logger.info(f"[task={task_id}] 取出 {len(items)} 条 agent2 中断")
    return items


def has_pending_interrupts(task_id: str | UUID) -> bool:
    """task 是否有待处理的中断(快速判断)"""
    queue = _get_or_create(str(task_id))
    return queue.has_pending()


def peek_pending_interrupt(task_id: str | UUID) -> list[dict[str, Any]]:
    """查看未处理中断的副本(不消费;刷新页面后恢复前端 pending 态用)"""
    queue = _get_or_create(str(task_id))
    with queue._lock:  # noqa: SLF001 同模块内直接持锁取副本,避免 drain 误消费
        return list(queue._items)


def cancel_pending_interrupt(task_id: str | UUID) -> list[dict[str, Any]]:
    """原子取消:取出并清空未处理中断(用户点击取消时调用)

    与 drain_interrupts 共用同一把队列锁,二者互斥:要么取消赢
    (返回被取消的中断,执行端 drain 到空),要么注入赢(返回空列表,
    中断已生效不可取消),不会出现两头都生效。
    """
    queue = _get_or_create(str(task_id))
    items = queue.drain()
    if items:
        logger.info(
            f"[task={task_id}] 用户取消 {len(items)} 条待生效 agent2 中断"
        )
    return items


def clear_interrupts(task_id: str | UUID) -> None:
    """清除 task 的中断队列(任务结束/重启时调用)"""
    task_id_str = str(task_id)
    with _queues_lock:
        _queues.pop(task_id_str, None)


# ============================================================
# 打断计数(per-task, per-round):防止 agent2 频繁打断
# ============================================================

class _InterruptCounter:
    """单个 task 的打断计数器(按 round 分别计数)"""

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}  # round_idx → count
        self._lock = threading.Lock()

    def get(self, round_idx: int) -> int:
        with self._lock:
            return self._counts.get(round_idx, 0)

    def increment(self, round_idx: int) -> int:
        with self._lock:
            self._counts[round_idx] = self._counts.get(round_idx, 0) + 1
            return self._counts[round_idx]

    def decrement(self, round_idx: int) -> int:
        """计数 -1(用户取消打断时回补配额),不低于 0"""
        with self._lock:
            self._counts[round_idx] = max(self._counts.get(round_idx, 0) - 1, 0)
            return self._counts[round_idx]

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


_counters: dict[str, _InterruptCounter] = {}
_counters_lock = threading.Lock()


def _get_counter(task_id: str) -> _InterruptCounter:
    with _counters_lock:
        if task_id not in _counters:
            _counters[task_id] = _InterruptCounter()
        return _counters[task_id]


def get_interrupt_count(task_id: str | UUID, round_idx: int) -> int:
    """获取当前轮已打断次数"""
    return _get_counter(str(task_id)).get(round_idx)


def increment_interrupt_count(task_id: str | UUID, round_idx: int) -> int:
    """打断计数 +1,返回更新后的次数"""
    return _get_counter(str(task_id)).increment(round_idx)


def decrement_interrupt_count(task_id: str | UUID, round_idx: int) -> int:
    """打断计数 -1(用户取消打断时回补本轮配额),返回更新后的次数"""
    return _get_counter(str(task_id)).decrement(round_idx)


def clear_interrupt_count(task_id: str | UUID) -> None:
    """清除 task 的打断计数(任务结束/重启时调用)"""
    task_id_str = str(task_id)
    with _counters_lock:
        _counters.pop(task_id_str, None)
