"""用户交互管理:后台线程阻塞等待用户确认的唤醒机制

场景:
1. verifier_agent 动作授权(per_action 模式):每个 HTTP/PoC 动作
   执行前推送事件给前端弹窗,后台线程阻塞等待用户确认/拒绝
2. 危险命令确认(local 模式安全策略):LLM 执行危险命令前推送事件
   给前端弹窗,后台线程阻塞等待用户确认/拒绝

历史:此前还承载 agent2 澄清提问(pending question)与覆盖度清单确认
(pending checklist)两套阻塞机制,随"任务开始时质检评估"移除而删除。

设计:
- 每个 task 同时只有一个待处理动作(单轮确认)
- 用 threading.Event 阻塞后台线程,无限等待(用户随时回来确认)
- pending 的动作与用户决议分别存全局 dict
- 任务结束/失败时调用 cleanup 释放内存

线程安全:用 threading.Lock 保护两个 dict。
适用单机部署;多实例部署需换 Redis 等共享存储。
"""
from __future__ import annotations

import logging
import threading
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


# ============================================================
# verifier_agent 动作授权机制(per_action 模式:每个 HTTP/PoC 动作阻塞等用户确认)
# ============================================================


class _PendingVerifyAction:
    """单个 task 的待授权验证动作状态

    verifier_agent 在 per_action 模式下,每次执行 http_request / run_python_code 前:
    1. request_verify_authorization:把动作描述存起来 + 推 SSE 事件给前端弹窗
    2. wait_for_authorization:阻塞等用户确认
    3. 用户确认 → submit_verify_authorization(approved=True) → 唤醒,继续执行
       用户拒绝 → submit_verify_authorization(approved=False) → 唤醒,返回拒绝信息
    """

    def __init__(self) -> None:
        # 后台线程阻塞在此 Event 上
        self.event: threading.Event = threading.Event()
        # 用户提交的授权决议(action_id → approved bool)
        self.resolution: bool | None = None
        # 当前待授权的动作描述(供前端查询/展示)
        self.action_payload: dict[str, Any] | None = None
        self.lock = threading.Lock()


_pending_verify: dict[str, _PendingVerifyAction] = {}
_pending_verify_lock = threading.Lock()


def _get_or_create_verify(task_id: str) -> _PendingVerifyAction:
    with _pending_verify_lock:
        if task_id not in _pending_verify:
            _pending_verify[task_id] = _PendingVerifyAction()
        return _pending_verify[task_id]


def request_verify_authorization(
    task_id: str | UUID,
    action_desc: dict[str, Any],
) -> None:
    """设置待授权的验证动作并推 SSE 事件(后台线程调用)

    action_desc: {"action_id": str, "type": "http_request"|"run_python_code", ...}
    推送 verify_action 事件,前端弹窗展示动作详情让用户确认/拒绝。
    """
    task_id_str = str(task_id)
    pv = _get_or_create_verify(task_id_str)
    with pv.lock:
        pv.event = threading.Event()
        pv.resolution = None
        pv.action_payload = action_desc
    # 推送事件给前端
    from app.event_bus import publish
    publish(task_id_str, "verify_action", action_desc)
    logger.info(f"[task={task_id_str}] 等待用户授权验证动作: {action_desc.get('type', '?')}")


def wait_for_authorization(task_id: str | UUID, action_id: str) -> bool:
    """阻塞等待用户授权决议(后台线程调用)

    返回 True=用户同意执行,False=用户拒绝或任务被取消。
    """
    task_id_str = str(task_id)
    pv = _get_or_create_verify(task_id_str)
    pv.event.wait()
    with pv.lock:
        approved = pv.resolution is True
        pv.action_payload = None
    return approved


def submit_verify_authorization(
    task_id: str | UUID,
    action_id: str,
    approved: bool,
) -> bool:
    """提交用户授权决议,唤醒后台线程(API 端点调用)

    返回 True 表示成功唤醒;False 表示当前 task 没有待授权动作。
    """
    task_id_str = str(task_id)
    with _pending_verify_lock:
        pv = _pending_verify.get(task_id_str)
    if pv is None:
        return False
    with pv.lock:
        if pv.action_payload is None:
            return False
        if pv.event.is_set():
            return False
        pv.resolution = approved
    pv.event.set()
    logger.info(f"[task={task_id_str}] 用户{'同意' if approved else '拒绝'}验证动作 {action_id}")
    return True


def get_pending_verify_action(task_id: str | UUID) -> dict[str, Any] | None:
    """查询 task 当前的待授权验证动作(前端恢复弹窗用)"""
    task_id_str = str(task_id)
    with _pending_verify_lock:
        pv = _pending_verify.get(task_id_str)
    if pv is None:
        return None
    with pv.lock:
        if pv.action_payload is None:
            return None
        return dict(pv.action_payload)


def clear_pending_verify_action(task_id: str | UUID) -> None:
    """清除 task 的待授权验证动作(任务结束/失败时调用)"""
    task_id_str = str(task_id)
    with _pending_verify_lock:
        pv = _pending_verify.pop(task_id_str, None)
    if pv is not None:
        with pv.lock:
            pv.action_payload = None
            pv.resolution = False
        pv.event.set()


def has_pending_verify_action(task_id: str | UUID) -> bool:
    """task 是否有待授权的验证动作(快速判断)"""
    task_id_str = str(task_id)
    with _pending_verify_lock:
        pv = _pending_verify.get(task_id_str)
    if pv is None:
        return False
    with pv.lock:
        return pv.action_payload is not None


# ============================================================
# 危险命令确认(local 模式安全策略)
# ============================================================


class _PendingCommandConfirm:
    """单个 task 的待确认危险命令状态

    local 模式下,LLM 调用 run_command / run_python_code 执行的危险命令:
    1. request_command_confirm:把命令描述存起来 + 推 SSE 事件给前端弹窗
    2. wait_for_command_confirm:阻塞等用户确认
    3. 用户确认 → submit_command_confirm(approved=True) → 唤醒,继续执行
       用户拒绝 → submit_command_confirm(approved=False) → 唤醒,返回拒绝信息
    """

    def __init__(self) -> None:
        self.event: threading.Event = threading.Event()
        self.resolution: bool | None = None
        self.command_payload: dict[str, Any] | None = None
        self.lock = threading.Lock()


_pending_command: dict[str, _PendingCommandConfirm] = {}
_pending_command_lock = threading.Lock()


def _get_or_create_command(task_id: str) -> _PendingCommandConfirm:
    with _pending_command_lock:
        if task_id not in _pending_command:
            _pending_command[task_id] = _PendingCommandConfirm()
        return _pending_command[task_id]


def request_command_confirm(
    task_id: str | UUID,
    command_desc: dict[str, Any],
) -> None:
    """设置待确认的危险命令并推 SSE 事件(后台线程调用)

    command_desc: {"command_id": str, "command": str, "tool": str, "reason": str}
    """
    task_id_str = str(task_id)
    pc = _get_or_create_command(task_id_str)
    with pc.lock:
        pc.event = threading.Event()
        pc.resolution = None
        pc.command_payload = command_desc
    from app.event_bus import publish
    publish(task_id_str, "command_confirm", command_desc)
    logger.info(f"[task={task_id_str}] 等待用户确认危险命令: {command_desc.get('command', '?')[:80]}")


def wait_for_command_confirm(task_id: str | UUID, command_id: str) -> bool:
    """阻塞等待用户确认决议(后台线程调用)

    返回 True=用户同意执行,False=用户拒绝或任务被取消。
    """
    task_id_str = str(task_id)
    pc = _get_or_create_command(task_id_str)
    pc.event.wait()
    with pc.lock:
        approved = pc.resolution is True
        pc.command_payload = None
    return approved


def submit_command_confirm(
    task_id: str | UUID,
    command_id: str,
    approved: bool,
) -> bool:
    """提交用户确认决议,唤醒后台线程(API 端点调用)

    返回 True 表示成功唤醒;False 表示当前 task 没有待确认命令。
    """
    task_id_str = str(task_id)
    with _pending_command_lock:
        pc = _pending_command.get(task_id_str)
    if pc is None:
        return False
    with pc.lock:
        if pc.command_payload is None:
            return False
        if pc.event.is_set():
            return False
        pc.resolution = approved
    pc.event.set()
    logger.info(f"[task={task_id_str}] 用户{'同意' if approved else '拒绝'}执行命令 {command_id}")
    return True


def get_pending_command_confirm(task_id: str | UUID) -> dict[str, Any] | None:
    """查询 task 当前的待确认命令(前端恢复弹窗用)"""
    task_id_str = str(task_id)
    with _pending_command_lock:
        pc = _pending_command.get(task_id_str)
    if pc is None:
        return None
    with pc.lock:
        if pc.command_payload is None:
            return None
        return dict(pc.command_payload)


def clear_pending_command_confirm(task_id: str | UUID) -> None:
    """清除 task 的待确认命令(任务结束/失败时调用)"""
    task_id_str = str(task_id)
    with _pending_command_lock:
        pc = _pending_command.pop(task_id_str, None)
    if pc is not None:
        with pc.lock:
            pc.command_payload = None
            pc.resolution = False
        pc.event.set()


def has_pending_command_confirm(task_id: str | UUID) -> bool:
    """task 是否有待确认的危险命令(快速判断)"""
    task_id_str = str(task_id)
    with _pending_command_lock:
        pc = _pending_command.get(task_id_str)
    if pc is None:
        return False
    with pc.lock:
        return pc.command_payload is not None
