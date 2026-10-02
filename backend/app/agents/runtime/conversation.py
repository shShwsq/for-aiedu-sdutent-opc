"""共享对话落库 + SSE 推送(原四处各写一份,收敛为单一实现)

原状:react_agent / orchestrator / acp_base 各有一个 `_add_conversation`,
agent2 的只读/引用核查工具内联落库块(自带一份推送格式),verifier 又是
一份(且缺 id / tool_call_id 配对,前端无法把 result 与 call 配对展示)。
推送 payload 此前也有两种形状(带/不带 reasoning、带/不带 tool_call_id),
现统一为超集形状,前端兼容(缺省字段为 None)。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.event_bus import publish
from app.models.task import Conversation, Task


def record_conversation(
    db: Session,
    task: Task,
    *,
    round_idx: int,
    role: str,
    type: str,
    content: str,
    reasoning: str | None = None,
    tool_call_id: str | None = None,
    publish_event: bool = True,
    extra_payload: dict[str, Any] | None = None,
) -> Conversation:
    """落库一条对话(带 round_idx),可选推送 conversation 事件给前端 SSE

    - publish_event=False:react_agent / verifier 的 thinking 不推 SSE
      (流式卡片已展示,避免重复;迟到订阅者经 GET /tasks/{id} 快照补齐)
    - tool_call_id:仅 type=tool_result 用,对应 tool_call 记录的 id,
      前端据此配对展示(并行调用时 result 按完成顺序落库,不紧跟 call)
    - extra_payload:随事件附加的字段(如 verifier 的 verify=True)

    返回创建的 Conversation 对象(供调用方拿 id 做后续关联/更新)。
    """
    conv = Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role=role,
        type=type,
        content=content,
        reasoning=reasoning,
        tool_call_id=tool_call_id,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    if publish_event:
        publish_conversation(task, conv, extra_payload=extra_payload)
    return conv


def publish_conversation(
    task: Task,
    conv: Conversation,
    *,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    """把已落库的对话记录推给前端 SSE(消费时刻入流 / 多端同步场景)"""
    data: dict[str, Any] = {
        "id": str(conv.id),
        "round_idx": conv.round_idx,
        "role": conv.role,
        "type": conv.type,
        "content": conv.content,
        "reasoning": conv.reasoning,
        "tool_call_id": conv.tool_call_id,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
    }
    if extra_payload:
        data.update(extra_payload)
    publish(task.id, "conversation", data)
