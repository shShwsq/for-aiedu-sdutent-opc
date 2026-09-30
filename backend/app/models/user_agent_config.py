"""用户外部 CLI agent 配置(per-user,多行)

用户可配置多种外部 CLI agent(Qoder CLI / 未来 Aider / Goose 等),
任务创建时选择一种作为执行器。每种 agent 一行配置,凭证加密存储。

设计:
- 多行表(user_id 不唯一),UNIQUE(user_id, agent_type) 保证每用户每类 agent 一条
- credentials_encrypted:Fernet 加密的 JSON 密文,结构因 agent 类型而异
  (如 qoder_cli: {"pat":"xxx"};未来 aider: {"api_key":"xxx","base_url":"yyy"})
- is_active:是否在任务创建页可选(用户可禁用某 agent 但保留配置)
- agent_type 与 task.executor 字段值对齐,executor_agent 按 registry 派发
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserAgentConfig(Base):
    __tablename__ = "user_agent_configs"
    __table_args__ = (
        # 每用户每类 agent 只能有一条配置
        UniqueConstraint("user_id", "agent_type", name="uq_user_agent_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 智能体类型标识,与 task.executor 值对齐(如 'qoder_cli')
    # 合法值由 app.agents.registry.AGENT_REGISTRY 定义
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Fernet 加密的 JSON 密文(用 security.encrypt_secret 加密)
    # 解密后是 dict,结构因 agent 类型而异(见 registry 的 credential_fields)
    credentials_encrypted: Mapped[str] = mapped_column(
        String(4096), nullable=False, server_default=""
    )
    # 是否启用(任务创建页只展示 is_active=True 的 agent)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
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
