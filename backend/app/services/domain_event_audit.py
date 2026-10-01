"""领域事件审计订阅者(阶段 1 的第一个消费者,亦是扩展示范)

职责:把所有领域事件 append-only 落库到 domain_event_logs 表。
这是"事件即扩展点"的验收样例 —— 审计能力完全通过订阅挂载,
没有改动 orchestrator / agent2 任何主流程逻辑。

后续扩展(统计、webhook、通知)照同样方式 subscribe 即可。

会话管理:handler 在任务后台线程内被同步调用,不能用调用方(orchestrator)
的 db session(生命周期与线程归属都不对),必须开独立短生命周期 session,
用完即弃;落库失败只记日志(emit 已隔离异常,主流程不受影响)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.database import SessionLocal
from app.domain_events import DomainEvent, subscribe_all
from app.models.domain_event_log import DomainEventLog

logger = logging.getLogger(__name__)


def _audit_handler(event: DomainEvent) -> None:
    """领域事件落库(所有类型)"""
    # occurred_at 是 ISO 字符串,解析失败兜底当前时间(不因格式问题丢审计)
    try:
        occurred_at = datetime.fromisoformat(event.occurred_at)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    except ValueError:
        occurred_at = datetime.now(timezone.utc)

    try:
        task_uuid = _to_uuid(event.task_id)
    except ValueError:
        logger.warning(f"[audit] 非法 task_id,丢弃审计记录: {event.task_id!r}")
        return

    db = SessionLocal()
    try:
        db.add(DomainEventLog(
            task_id=task_uuid,
            event_type=event.type,
            payload=event.payload or None,
            occurred_at=occurred_at,
        ))
        db.commit()
    except Exception:
        # emit 已隔离异常,这里 rollback 防止连接带着未决事务回池
        db.rollback()
        logger.exception(
            f"[audit] 领域事件落库失败(已忽略): type={event.type} task={event.task_id}"
        )
    finally:
        db.close()


def _to_uuid(value: str):
    import uuid
    return uuid.UUID(value)


def register_audit_subscriber():
    """注册审计订阅者(应用启动时调用一次)"""
    return subscribe_all(_audit_handler)
