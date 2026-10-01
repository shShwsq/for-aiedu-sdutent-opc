"""领域事件审计日志(append-only)

由 app/services/domain_event_audit.py 的订阅者写入:
agent 协作流程各阶段边界产生的领域事件(启动/完成/失败/
轮完成/验证完成)全量落库,供事后审计与统计分析。

设计:
- append-only:只插入不更新,审计记录不可篡改
- 不加外键:任务删除后审计记录留存(审计语义优先于引用完整性)
- payload 与 emit() 时一致(小型标量/截断摘要,无大会话内容)
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DomainEventLog(Base):
    __tablename__ = "domain_event_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 事件归属任务(无外键,任务删除后留档)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    # 事件类型(app/domain_events.py 的常量,如 task.completed)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 事件负载(轮次/数量/截断摘要等小型数据)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 事件发生时间(emit 时刻,ISO 字符串解析)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 落库时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# 复合索引:按任务回放事件时间线(审计最常用查询)
Index(
    "ix_domain_event_logs_task_occurred",
    DomainEventLog.task_id,
    DomainEventLog.occurred_at,
)
