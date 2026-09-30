"""用户交互管理:agent2 请求用户澄清时的阻塞-唤醒机制

场景:agent2 在第 0 轮初始评估时,如果认为用户意图不清晰,
可以输出 ask_user=true + questions 列表,orchestrator 把 questions
推送给前端,后台线程阻塞等待用户填答;用户提交答案后,API 端点
唤醒后台线程,把答案拼回 user_intent 重新评估。

设计:
- 每个 task 同时只能有一个待回答的问题(单轮提问)
- 用 threading.Event 阻塞后台线程,无限等待(用户随时回来填答)
- pending 的问题和已提交的答案分别存全局 dict
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


class _PendingQuestion:
    """单个 task 的待回答问题状态"""

    def __init__(self) -> None:
        # 后台线程阻塞在此 Event 上,API 提交答案时 set()
        self.event: threading.Event = threading.Event()
        # 用户提交的答案(API 端点写入,后台线程读取)
        # 结构:[{"question_id": str, "value": str | list[str]}, ...]
        self.answers: list[dict[str, Any]] | None = None
        # 问题快照(orchestrator 推送时存,API 校验/前端恢复时读)
        # 结构:{"ask_round": int, "questions": [...], "reasoning": str}
        self.question_payload: dict[str, Any] | None = None
        # 锁:保护 answers / question_payload 的读写
        self.lock = threading.Lock()


# 全局注册表:task_id(str)→ _PendingQuestion
_pending: dict[str, _PendingQuestion] = {}
_pending_lock = threading.Lock()


def _get_or_create(task_id: str) -> _PendingQuestion:
    """获取(或创建)task 的待回答问题状态"""
    with _pending_lock:
        if task_id not in _pending:
            _pending[task_id] = _PendingQuestion()
        return _pending[task_id]


def set_pending_question(
    task_id: str | UUID,
    question_payload: dict[str, Any],
) -> _PendingQuestion:
    """设置 task 的待回答问题(后台线程调用)

    在后台线程阻塞前调用:把 questions 存起来供 API/前端查询,
    同时创建新的 Event 准备 wait。

    参数:
        task_id: 任务 ID
        question_payload: {"ask_round": int, "questions": [...], "reasoning": str}

    返回:_PendingQuestion,后台线程随后调 .event.wait() 阻塞
    """
    task_id_str = str(task_id)
    pq = _get_or_create(task_id_str)
    with pq.lock:
        # 重置状态:新的一轮提问
        pq.event = threading.Event()
        pq.answers = None
        pq.question_payload = question_payload
    return pq


def wait_for_answers(task_id: str | UUID) -> list[dict[str, Any]]:
    """阻塞等待用户提交答案(后台线程调用)

    无限等待,直到 submit_answers 被调用。任务被取消时由调用方处理。
    返回答案列表;若 question_payload 被外部清除(任务取消),返回空列表。

    注意:question_payload 的清理已在 submit_answers 中完成(消除竞态),
    这里只清 answers,避免后台线程长期持有已读数据。
    """
    task_id_str = str(task_id)
    pq = _get_or_create(task_id_str)
    # 无限等待
    pq.event.wait()
    with pq.lock:
        answers = pq.answers or []
        # question_payload 已在 submit_answers 中清理,这里只清 answers
        pq.answers = None
    return answers


def submit_answers(
    task_id: str | UUID,
    answers: list[dict[str, Any]],
) -> bool:
    """提交用户答案,唤醒后台线程(API 端点调用)

    返回 True 表示成功唤醒;False 表示当前 task 没有待回答问题
    (可能用户重复提交,或任务已结束)。
    """
    task_id_str = str(task_id)
    with _pending_lock:
        pq = _pending.get(task_id_str)
    if pq is None:
        return False
    with pq.lock:
        if pq.question_payload is None:
            # 没有待回答问题
            return False
        if pq.event.is_set():
            # 已经被唤醒过(重复提交)
            return False
        pq.answers = answers
        # 立即清理 question_payload:避免 submit 返回后、后台线程唤醒清理前
        # 的时间窗口内,前端 get_pending_question 拿到旧 payload 重复弹窗。
        # (原先清理放在 wait_for_answers 里,依赖后台线程被调度,存在竞态)
        pq.question_payload = None
    pq.event.set()
    logger.info(f"[task={task_id_str}] 收到用户答案,{len(answers)} 项")
    return True


def get_pending_question(task_id: str | UUID) -> dict[str, Any] | None:
    """查询 task 当前的待回答问题(前端恢复弹窗用)

    返回 question_payload(含 ask_round/questions/reasoning);无待回答问题返回 None。
    """
    task_id_str = str(task_id)
    with _pending_lock:
        pq = _pending.get(task_id_str)
    if pq is None:
        return None
    with pq.lock:
        if pq.question_payload is None:
            return None
        # 返回副本,避免外部修改
        return dict(pq.question_payload)


def clear_pending_question(task_id: str | UUID) -> None:
    """清除 task 的待回答问题(任务结束/失败时调用)

    若后台线程还在 wait,会因 Event 永不 set 而继续阻塞——所以这里也 set() 一下,
    让 wait_for_answers 返回空列表,后台线程能继续走清理流程。
    """
    task_id_str = str(task_id)
    with _pending_lock:
        pq = _pending.pop(task_id_str, None)
    if pq is not None:
        with pq.lock:
            pq.question_payload = None
            if pq.answers is None:
                pq.answers = []
        # 唤醒可能正在阻塞的线程
        pq.event.set()


def has_pending_question(task_id: str | UUID) -> bool:
    """task 是否有待回答问题(快速判断,不加锁)"""
    task_id_str = str(task_id)
    with _pending_lock:
        pq = _pending.get(task_id_str)
    if pq is None:
        return False
    with pq.lock:
        return pq.question_payload is not None


# ============================================================
# 覆盖度清单编辑阻塞机制(场景降级后:agent2 动态生成 checklist,
# 用户可编辑确认,后台线程阻塞等待)
# ============================================================


class _PendingChecklist:
    """单个 task 的待确认覆盖度清单状态"""

    def __init__(self) -> None:
        # 后台线程阻塞在此 Event 上,API 提交编辑后 set()
        self.event: threading.Event = threading.Event()
        # 用户编辑后的 checklist(API 写入,后台线程读取)
        # None 表示用户选择"直接采用 LLM 生成结果"(不编辑)
        self.edited_checklist: list[dict[str, Any]] | None = None
        # LLM 生成的原始 checklist 快照(供前端展示/编辑)
        self.checklist_payload: list[dict[str, Any]] | None = None
        # 锁
        self.lock = threading.Lock()


# 全局注册表:task_id(str)→ _PendingChecklist
_pending_checklist: dict[str, _PendingChecklist] = {}
_pending_checklist_lock = threading.Lock()


def _get_or_create_checklist(task_id: str) -> _PendingChecklist:
    with _pending_checklist_lock:
        if task_id not in _pending_checklist:
            _pending_checklist[task_id] = _PendingChecklist()
        return _pending_checklist[task_id]


def set_pending_checklist(
    task_id: str | UUID,
    checklist: list[dict[str, Any]],
) -> _PendingChecklist:
    """设置 task 的待确认覆盖度清单(后台线程调用)

    在后台线程阻塞前调用:把 agent2 生成的 checklist 存起来供 API/前端查询。

    返回:_PendingChecklist,后台线程随后调 .event.wait() 阻塞
    """
    task_id_str = str(task_id)
    pc = _get_or_create_checklist(task_id_str)
    with pc.lock:
        pc.event = threading.Event()
        pc.edited_checklist = None
        pc.checklist_payload = checklist
    return pc


def wait_for_checklist_confirmation(
    task_id: str | UUID,
) -> list[dict[str, Any]]:
    """阻塞等待用户确认/编辑覆盖度清单(后台线程调用)

    无限等待,直到 submit_checklist 被调用。
    返回最终生效的 checklist:
      - 用户编辑过:返回编辑后的 checklist
      - 用户选择"直接采用":返回 LLM 生成的原始 checklist
    """
    task_id_str = str(task_id)
    pc = _get_or_create_checklist(task_id_str)
    pc.event.wait()
    with pc.lock:
        edited = pc.edited_checklist
        original = pc.checklist_payload or []
        # 清理 payload(已确认)
        pc.checklist_payload = None
    return edited if edited is not None else original


def submit_checklist(
    task_id: str | UUID,
    edited_checklist: list[dict[str, Any]] | None,
) -> bool:
    """提交编辑后的覆盖度清单,唤醒后台线程(API 端点调用)

    参数:
        edited_checklist: 用户编辑后的 checklist。None 表示"直接采用 LLM 生成结果"

    返回 True 表示成功唤醒;False 表示当前 task 没有待确认清单
    (可能重复提交,或任务已结束)。
    """
    task_id_str = str(task_id)
    with _pending_checklist_lock:
        pc = _pending_checklist.get(task_id_str)
    if pc is None:
        return False
    with pc.lock:
        if pc.checklist_payload is None:
            return False
        if pc.event.is_set():
            return False
        pc.edited_checklist = edited_checklist
        # 立即清理 payload,避免竞态
        pc.checklist_payload = None
    pc.event.set()
    logger.info(f"[task={task_id_str}] 收到覆盖度清单确认")
    return True


def get_pending_checklist(
    task_id: str | UUID,
) -> list[dict[str, Any]] | None:
    """查询 task 当前的待确认覆盖度清单(前端恢复弹窗用)

    返回 checklist_payload;无待确认清单返回 None。
    """
    task_id_str = str(task_id)
    with _pending_checklist_lock:
        pc = _pending_checklist.get(task_id_str)
    if pc is None:
        return None
    with pc.lock:
        if pc.checklist_payload is None:
            return None
        # 返回深拷贝,避免外部修改
        import copy
        return copy.deepcopy(pc.checklist_payload)


def clear_pending_checklist(task_id: str | UUID) -> None:
    """清除 task 的待确认覆盖度清单(任务结束/失败时调用)"""
    task_id_str = str(task_id)
    with _pending_checklist_lock:
        pc = _pending_checklist.pop(task_id_str, None)
    if pc is not None:
        with pc.lock:
            pc.checklist_payload = None
        pc.event.set()


def has_pending_checklist(task_id: str | UUID) -> bool:
    """task 是否有待确认覆盖度清单(快速判断)"""
    task_id_str = str(task_id)
    with _pending_checklist_lock:
        pc = _pending_checklist.get(task_id_str)
    if pc is None:
        return False
    with pc.lock:
        return pc.checklist_payload is not None


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
