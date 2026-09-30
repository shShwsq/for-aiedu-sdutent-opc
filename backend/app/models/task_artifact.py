"""任务工作区产物模型

任务完成时在容器内捕获的工作区变更(diff/patch 等),1:N 挂在 Task 上。

设计:
- kind="git_diff":工作区所有修改(已跟踪 + 未跟踪)拼成的 patch 文本,
  可用 `git apply` 重建工作区(前提:未被截断)
- 与 Task 1:N,删 Task 级联删 artifacts(ondelete=CASCADE)
- metadata_ 列名 "metadata"(SQLAlchemy 保留字,Python 属性用 metadata_),
  与 Result.metadata_ 约定一致
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TaskArtifact(Base):
    """任务工作区产物(diff/patch 等)

    kind 区分产物类型,本次只实现 "git_diff";预留 "build_log"/"report" 等。
    """

    __tablename__ = "task_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 产物类型:"git_diff"(本次),预留 "build_log"/"report" 等
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # 产物正文(diff/patch 文本)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 元信息(JSONB),如 {"files_changed": 5, "truncated": false, "char_count": 1234}
    # 列名 "metadata"(SQLAlchemy 保留字,Python 属性用 metadata_)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped["Task"] = relationship(back_populates="artifacts")
