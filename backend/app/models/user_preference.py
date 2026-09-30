"""User Profile (per-user, 1:1)

用户级的稳定倾向(自由文本 Markdown),注入 agent2 的 system prompt,
影响评判标准与 checklist 生成。

设计:
- 1:1 表(user_id unique),用户首次保存时 get_or_create
- user_profile: 自由文本 Markdown(用户在记忆管理页编辑),注入 agent2

agent 策略配置已拆到独立表 agent_policies(见 models/agent_policy.py),
练习设置已拆到独立表 practice_settings(见 models/practice.py),
老数据分别由 migrate_agent_policy_table() / migrate_practice_settings_table() 迁移。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,  # 1:1
        nullable=False,
    )
    # 自由文本 Markdown(用户在记忆管理页编辑,注入 agent2)
    user_profile: Mapped[str] = mapped_column(
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


def migrate_user_preference_columns() -> None:
    """幂等迁移 user_preferences:删遗留 preferences 列,custom_prompt 改名 user_profile。

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动改列。
    启动时检查并 ALTER TABLE,保证老库平滑升级。
    全新库(create_all 已按新 model 建好所有列)或已迁过 → 直接返回。

    老数据:custom_prompt 原值经 RENAME 平滑保留为 user_profile;preferences 列直接删除
    (代码已不再读写,内容无副作用)。
    agent_policy 列的迁移(拷入 agent_policies 独立表后删列)见
    agent_policy.migrate_agent_policy_table(),在本函数之后执行;
    auto_generate_practice 列的迁移见 practice.migrate_practice_settings_table()。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("user_preferences"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("user_preferences")}
        # 1) 删遗留 preferences 列(老库才有;代码已不再读写)
        if "preferences" in cols:
            conn.execute(
                text("ALTER TABLE user_preferences DROP COLUMN preferences")
            )
            log.info("user_preferences 删列: preferences")
        # 2) custom_prompt 改名 user_profile(老库才有;全新库已是 user_profile)
        if "custom_prompt" in cols and "user_profile" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE user_preferences "
                    "RENAME COLUMN custom_prompt TO user_profile"
                )
            )
            log.info("user_preferences 改名: custom_prompt -> user_profile")
        conn.commit()
