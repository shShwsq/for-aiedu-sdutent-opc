"""领域事件总线(进程内 hook 层)

借鉴 deepseek-harness(Cordis)的"事件即扩展点"思想:
- 领域事件 = agent 协作流程在阶段边界产生的客观事实
  (任务启动/完成/失败、checklist 确认、追问、检查点评估、验证完成等)
- 扩展代码(审计日志、统计、未来 webhook/通知)通过 subscribe() 挂载,
  不需要修改 orchestrator / agent2 主流程
- 订阅是可逆的:subscribe() 返回退订函数,调用即回滚该订阅

与 app.event_bus 的职责划分(勿混用):
- event_bus:面向前端 SSE 的传输管道,订阅者是 Queue(SSE 端点消费)
- domain_events:面向后端扩展代码的进程内 hook,订阅者是 callable
- 两者现阶段独立运行:SSE 推送继续走 event_bus 原路径,互不重复

可靠性约定:
- handler 同步调用,publish() 在调用线程内完成分发
- 单个 handler 抛异常只记日志,不影响其他 handler,更不影响主流程
- 不做跨进程分发(单机部署;多实例时再引入 Redis Pub/Sub,同 event_bus)
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

logger = logging.getLogger(__name__)


# ============================================================
# 事件类型(集中声明,埋点与订阅方共用,避免裸字符串散落)
# ============================================================

# 任务生命周期
TASK_STARTED = "task.started"            # 任务进入 RUNNING(orchestrator 启动)
TASK_COMPLETED = "task.completed"        # 任务正常完成(单/双 agent、resume 完成路径)
TASK_FAILED = "task.failed"              # 任务失败(主流程与 resume 的 except 路径)

# 协作流程边界
CHECKLIST_CONFIRMED = "checklist.confirmed"      # 用户确认覆盖度清单,已落库 task.checklist
QUESTION_RAISED = "question.raised"              # agent2 向用户发起澄清提问(弹窗)
AGENT1_ROUND_COMPLETED = "agent1.round_completed"  # agent1(执行器)完成一轮执行
VERIFIER_COMPLETED = "verifier.completed"        # verifier_agent 完成一次动态验证(含失败)

ALL_EVENT_TYPES = frozenset({
    TASK_STARTED, TASK_COMPLETED, TASK_FAILED,
    CHECKLIST_CONFIRMED, QUESTION_RAISED,
    AGENT1_ROUND_COMPLETED, VERIFIER_COMPLETED,
})


# ============================================================
# 事件对象
# ============================================================

@dataclass(frozen=True)
class DomainEvent:
    """一个领域事件:流程边界上已发生的事实(不可变)

    payload 约定:只放小型标量/字典(轮次、数量、截断后的摘要),
    不要放大会话内容或大对象,审计落库与日志都会携带它。
    """
    type: str
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "task_id": self.task_id,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
        }


Handler = Callable[[DomainEvent], None]
Unsubscribe = Callable[[], None]


# ============================================================
# 注册表(进程级单例)
# ============================================================

_subscribers: dict[str, list[Handler]] = defaultdict(list)
_wildcard_subscribers: list[Handler] = []  # 订阅所有事件(审计日志用)
_lock = threading.Lock()


def subscribe(event_type: str, handler: Handler) -> Unsubscribe:
    """订阅指定类型的领域事件,返回退订函数(可逆注册)

    用法:
        unsub = subscribe(TASK_COMPLETED, my_handler)
        ...   # 不再需要时
        unsub()  # 回滚本次订阅,不影响其他订阅者
    """
    if event_type not in ALL_EVENT_TYPES:
        raise ValueError(
            f"未知事件类型: {event_type!r},可选:{sorted(ALL_EVENT_TYPES)}"
        )
    with _lock:
        _subscribers[event_type].append(handler)

    def _unsubscribe() -> None:
        with _lock:
            try:
                _subscribers[event_type].remove(handler)
            except ValueError:
                pass  # 已退订,幂等

    return _unsubscribe


def subscribe_all(handler: Handler) -> Unsubscribe:
    """订阅所有类型的领域事件(审计/统计类订阅者用)"""
    with _lock:
        _wildcard_subscribers.append(handler)

    def _unsubscribe() -> None:
        with _lock:
            try:
                _wildcard_subscribers.remove(handler)
            except ValueError:
                pass

    return _unsubscribe


def emit(
    event_type: str,
    task_id: UUID | str | None,
    **payload: Any,
) -> DomainEvent:
    """发出一个领域事件,同步分发给所有匹配的订阅者

    在流程边界调用(orchestrator / agent2 / agent_checkpoint 等),
    handler 的异常在这里被隔离 —— 扩展挂掉绝不拖垮主流程。

    返回构造出的 DomainEvent(便于测试断言与调用方日志)。
    """
    event = DomainEvent(
        type=event_type,
        task_id=str(task_id) if task_id is not None else "",
        payload=payload,
    )
    # 先取快照再调用:handler 内退订/新增订阅不影响本轮分发
    with _lock:
        handlers = list(_subscribers.get(event_type, ()))
        handlers += _wildcard_subscribers

    for handler in handlers:
        try:
            handler(event)
        except Exception:
            logger.exception(
                f"[domain-event] 订阅者处理失败(已隔离,不影响主流程): "
                f"type={event_type} task={event.task_id} "
                f"handler={getattr(handler, '__qualname__', repr(handler))}"
            )
    return event


def subscriber_count(event_type: str | None = None) -> int:
    """当前订阅者数量(诊断/测试用)"""
    with _lock:
        if event_type is None:
            return sum(len(v) for v in _subscribers.values()) + len(_wildcard_subscribers)
        return len(_subscribers.get(event_type, ()))


def clear_all_subscribers() -> None:
    """清空所有订阅(仅测试用,业务代码勿调)"""
    with _lock:
        _subscribers.clear()
        _wildcard_subscribers.clear()
