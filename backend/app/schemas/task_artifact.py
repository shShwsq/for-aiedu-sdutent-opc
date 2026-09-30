"""任务工作区产物的 Pydantic 模型(响应)

对应 app/models/task_artifact.py 的 TaskArtifact。

字段约定沿用 ResultResponse:metadata_ 字段(Python 属性名带下划线,
对应 DB 列名 metadata),前端类型也用 metadata_(与 TaskResult.metadata_ 一致),
不做 alias 映射——保持与现有 Result 响应同构。
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class TaskArtifactOut(BaseModel):
    """任务工作区产物响应

    GET /tasks/{task_id}/artifacts 返回列表,GET /tasks/{task_id}/artifacts/{id} 返回单个。
    metadata_ 示例:{"files_changed": 5, "truncated": false, "char_count": 1234}
    """

    id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    content: str
    metadata_: dict[str, Any] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
