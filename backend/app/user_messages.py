"""用户补充消息队列:用户在任务执行中/暂停中/完成后追加的指令

场景:用户在对话界面下方的输入框主动发消息(非 agent2 提问弹窗的回答)。
后端按 task.status 分发:
- running / paused:消息入队(端点推 user_message_pending 事件,前端以
  "待处理"条目展示在输入框上方,TRAE 式),react_agent 在下一迭代边界
  drain 出来注入 LLM 上下文,并在消费时刻补推 conversation 事件入流
- completed:不入队,由 API 端点直接启动新的协作 round
  (resume_audit_with_message,消息直达 agent1,不等老审查)

遗留消息兜底(未被 drain 的入队消息不被静默丢弃):
- react_agent 循环出口守卫:最终答案生成期间到达的消息 → 继续本轮处理
- orchestrator._auto_resume_leftover_messages:轮结束后仍遗留的消息
  (收尾窗口到达 / CLI 执行器无 drain 机制)→ 挪到新轮自动开启新一轮

设计要点:
- 与 user_interaction.py 的阻塞-唤醒机制(如验证动作授权)不同,这里是
  "队列"(可累积多条),而非"一次性 Event"(单动作单确认)。
  drain 时一次性取出全部并清空。
- 不需要 Event 阻塞:react_agent 在迭代边界主动 drain,有就处理,无就跳过。
  不阻塞 agent 线程,避免影响正常 ReAct 循环。
- 落库与 SSE 推送由 API 端点同步完成(确保刷新时数据库已有记录),
  本模块只负责 in-memory 队列管理。

线程安全:用 threading.Lock 保护 dict + 每个队列内部的 list。
适用单机部署;多实例部署需换 Redis 等共享存储。
"""
from __future__ import annotations

import logging
import threading
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class _UserMessageQueue:
    """单个 task 的用户补充消息队列

    队列内元素结构:
        {
            "content": str,           # 用户消息原文
            "created_at": str,         # ISO 时间(API 端点写入,用于排序)
            "message_id": str,         # 对应 Conversation.id(落库后的 UUID)
            "upload_ids": list[str],   # 本条消息附带的上传文件 ID(可空)
        }
    """

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def push(self, message: dict[str, Any]) -> None:
        """追加一条用户消息(API 端点调用)"""
        with self._lock:
            self._messages.append(message)

    def drain(self) -> list[dict[str, Any]]:
        """取出并清空所有待处理消息(react_agent 迭代边界调用)

        返回按 push 顺序排列的消息列表(空列表表示无消息)。
        """
        with self._lock:
            messages = list(self._messages)
            self._messages.clear()
        return messages

    def has_pending(self) -> bool:
        """是否有待处理消息(快速判断,不加锁内部 list 操作)"""
        with self._lock:
            return len(self._messages) > 0

    def clear(self) -> None:
        """清空队列(任务结束/重启时调用)"""
        with self._lock:
            self._messages.clear()


# 全局注册表:task_id(str)→ _UserMessageQueue
_queues: dict[str, _UserMessageQueue] = {}
_queues_lock = threading.Lock()


def _get_or_create(task_id: str) -> _UserMessageQueue:
    """获取(或创建)task 的用户消息队列"""
    with _queues_lock:
        if task_id not in _queues:
            _queues[task_id] = _UserMessageQueue()
        return _queues[task_id]


def push_user_message(
    task_id: str | UUID,
    content: str,
    *,
    message_id: str,
    created_at: str,
    upload_ids: list[str] | None = None,
) -> None:
    """追加用户消息到队列(运行中/暂停中场景使用)

    参数:
        task_id: 任务 ID
        content: 用户消息原文
        message_id: 对应 Conversation.id(已落库的 UUID 字符串)
        created_at: ISO 格式时间戳(用于排序)
        upload_ids: 本条消息附带的上传文件 ID 列表(可空;react_agent drain
            时传输进工作区 followup_uploads/ 并把路径并入注入文本)
    """
    queue = _get_or_create(str(task_id))
    queue.push({
        "content": content,
        "message_id": message_id,
        "created_at": created_at,
        "upload_ids": upload_ids or [],
    })
    logger.info(f"[task={task_id}] 用户补充消息入队(len={len(queue._messages)})")


def drain_user_messages(task_id: str | UUID) -> list[dict[str, Any]]:
    """取出并清空队列(react_agent 迭代边界调用)

    返回消息列表(空列表表示无消息)。取出后队列清空,不会重复处理。
    """
    queue = _get_or_create(str(task_id))
    messages = queue.drain()
    if messages:
        logger.info(f"[task={task_id}] 取出 {len(messages)} 条用户补充消息")
    return messages


def has_pending_messages(task_id: str | UUID) -> bool:
    """task 是否有待处理的用户消息(快速判断)"""
    queue = _get_or_create(str(task_id))
    return queue.has_pending()


def clear_user_messages(task_id: str | UUID) -> None:
    """清除 task 的用户消息队列(任务结束/重启时调用)

    注意:仅清空队列,不删除已落库的 Conversation 记录(那些是历史,需保留)。
    """
    task_id_str = str(task_id)
    with _queues_lock:
        _queues.pop(task_id_str, None)
