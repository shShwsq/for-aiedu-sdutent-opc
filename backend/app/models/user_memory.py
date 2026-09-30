"""全局长期记忆(per-user, 1:1)

跨项目通用的经验/约定,注入 agent2 的 system prompt。
用户可在 /memory 设置页编辑;任务完成后 agent 也可自动归纳写入(增量合并)。

设计:
- 1:1 表(user_id unique),首次保存时 get_or_create
- content: 跨项目通用记忆大段文本(用户可编辑 + agent 自动归纳增量合并)
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserMemory(Base):
    __tablename__ = "user_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,  # 1:1
        nullable=False,
    )
    # 跨项目通用长期记忆大段文本(用户可编辑 + agent 自动归纳增量合并)
    content: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
