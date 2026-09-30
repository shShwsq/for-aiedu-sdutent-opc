"""分项目记忆(per-user-per-repo, 1:N 与 users)

按 Git 仓库聚合的项目历史(已知问题/审计方向/历史发现),
注入 react_agent 的 system prompt,影响审计方向。

设计:
- 1:N 表(每用户每仓库一行),UNIQUE(user_id, repo_url_normalized)
- repo_url_normalized: 归一化后的 url(由 services/repo_url.normalize_repo_url 处理),
  保证同仓库不同写法(git@/ssh/https/.git/尾部斜杠)映射到同一行
- repo_url_raw: 用户首次输入的原始写法,展示用
- alias/note: 用户给项目起的别名/备注(可空)
- memory_content: 分项目记忆,LLM 增量合并更新
- last_summary_at: 上次自动归纳时间,便于去重与"是否需重新归纳"判断

注意:与 Task 无 FK 关联(Task.params.repo_url 是字符串,归一化后字符串匹配即可)。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "repo_url_normalized", name="uq_user_repo"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 归一化后的 repo_url(https://host/path 形式,去 .git,host 小写)
    repo_url_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    # 原始 repo_url(用户首次输入的写法,展示用)
    repo_url_raw: Mapped[str] = mapped_column(String(512), nullable=False)
    # 用户给项目起的别名(可空)
    alias: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 用户给项目的备注(可空,自由文本)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 分项目记忆(完整版,写入沙箱文件供 agent 随时 read_file 查阅;LLM 增量合并更新)
    memory_content: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )
    # 精简版记忆(LLM 生成,≤2000 字符,注入 system prompt 用;为空时注入侧回退 memory_content 截断)
    memory_summary: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )
    # 上次自动归纳写入时间(便于"是否需要重新归纳"判断 + 去重)
    last_summary_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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


def migrate_project_memory_summary() -> None:
    """幂等给 projects 加 memory_summary 列(精简版记忆,注入 system prompt 用)

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动加新列。
    启动时检查缺失列并 ALTER TABLE ADD COLUMN,保证老库平滑升级。
    全新库(create_all 已建好新列)或已迁过 → 直接返回。

    老数据 memory_summary="",注入侧回退用 memory_content 截断,行为与改动前一致。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("projects"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("projects")}
        if "memory_summary" in cols:
            return  # 已迁过
        conn.execute(
            text("ALTER TABLE projects ADD COLUMN memory_summary TEXT NOT NULL DEFAULT ''")
        )
        conn.commit()
        log.info("projects 加列: memory_summary")
