"""Conversation 排序回归测试(纯模型自省,不连真实 DB)。

背景:get_task 接口返回的 conversations 顺序由 Task.conversations 关系的
order_by 决定。该关系曾缺失 order_by,导致 PostgreSQL 在无 ORDER BY 时返回
顺序未定义,任务完成后刷新页面时 agent2 总结等后写入记录错位显示在
react_agent 工具调用之前(运行过程中靠 SSE push 顺序是正确的,刷新后才乱)。

本测试固化排序契约:关系必须按 (round_idx, created_at) 升序,与后端内部
查询(orchestrator/react_agent/agent2 的 .order_by)口径一致。
"""
import app.models.email_token  # noqa: F401  注册全部 mapper
import app.models.project  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.user  # noqa: F401
import app.models.user_agent_config  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_llm_config  # noqa: F401
import app.models.user_memory  # noqa: F401
import app.models.user_preference  # noqa: F401
from app.models.task import Conversation, Task


def test_conversations_relationship_has_order_by():
    """Task.conversations 必须显式指定 order_by,避免 PG 无序返回。"""
    rel = Task.__mapper__.relationships["conversations"]
    order_by = rel.order_by
    assert order_by is not None, (
        "Task.conversations 缺少 order_by:PostgreSQL 无 ORDER BY 时返回顺序未定义,"
        "会导致任务完成后刷新页面对话顺序错乱"
    )
    col_names = [c.name for c in order_by]
    assert col_names == ["round_idx", "created_at"], (
        f"order_by 应为 (round_idx, created_at),实际为 {col_names}"
    )
    # 确保引用的是 conversations 表的列(防止拼错指向其它表)
    table_names = {c.table.name for c in order_by}
    assert table_names == {Conversation.__tablename__}, (
        f"order_by 列应来自 {Conversation.__tablename__} 表,实际 {table_names}"
    )
