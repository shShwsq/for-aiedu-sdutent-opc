"""邮箱 token 模型

支持两类 token:
- verify_email: 邮箱验证(注册后激活账号)
- reset_password: 重置密码

设计要点:
- 独立表,不复用 User 字段。支持同用户多个待用 token(注册 + 重置同时)
- token_hash 存哈希(防数据库泄露后被直接重放)
- expires_at 控制时效
- used_at 标记已使用(用后即失效,允许审计)
"""
import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EmailTokenType(str, Enum):
    """token 类型"""

    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"


class EmailToken(Base):
    __tablename__ = "email_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # token 类型(verify_email / reset_password)
    type: Mapped[EmailTokenType] = mapped_column(
        SAEnum(EmailTokenType, name="email_token_type", values_callable=lambda e: [t.value for t in e]),
        nullable=False,
        index=True,
    )
    # token 原文只发给用户,库里存 sha256 哈希(防数据库泄露后被重放)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
