"""追问/重试后 SSE 重连竞态修复单测。

背景:completed 任务发追问(failed 任务重试)后,前端在 POST 响应后重连 SSE,
而 resume 后台线程可能尚未把 DB 状态从 COMPLETED/FAILED 改成 RUNNING。
若 SSE 端点按旧快照直接推 done/error 关闭连接,后续 checklist_review 等事件
虽进事件总线历史缓存却无人接收(用户需刷新页面才能恢复弹窗)。

修复组合:
1. API 端点(completed 发消息 / failed 重试)启动后台线程前同步把状态改为
   RUNNING 落库,消除 SSE 快照读到旧状态的窗口;
2. SSE 端点快照为 COMPLETED/FAILED 时,若事件总线已被 reset_task_bus
   (说明新执行已启动),不推终止事件,走正常订阅分支。
"""
import uuid
from unittest.mock import MagicMock

# 导入关系依赖模型,确保独立测试环境下 SQLAlchemy mapper 可完成配置
# (端点内创建真实 Conversation,Task/User 的 relationship 指向
# TaskArtifact/UserGitBinding,未注册时 mapper 初始化报错)
import app.models.task_artifact  # noqa: F401
import app.models.user_git_binding  # noqa: F401

import app.event_bus as event_bus
import app.routers.tasks as tasks_module
from app.models.task import TaskStatus
from app.schemas.task import SendMessageRequest


# ============================================================
# is_task_finished:总线结束标记查询
# ============================================================


def test_is_task_finished(monkeypatch):
    """总线生命周期:无记录=已结束,finish 后=True,reset 后=False。"""
    monkeypatch.setattr(event_bus, "_buses", {})
    tid = "race-task-is-finished"

    # 无总线记录(从未 publish)→ 保守视为已结束
    assert event_bus.is_task_finished(tid) is True

    # 发布过事件但未 finish → 未结束
    event_bus.publish(tid, "status", {"status": "running"})
    assert event_bus.is_task_finished(tid) is False

    # 任务完成 finish → 结束
    event_bus.finish_task(tid)
    assert event_bus.is_task_finished(tid) is True

    # resume 启动前 reset → 未结束(竞态窗口内判定依据)
    event_bus.reset_task_bus(tid)
    assert event_bus.is_task_finished(tid) is False


# ============================================================
# _should_force_close_stream:SSE 端点是否按快照直接关闭
# ============================================================


def _mk_bus_state(monkeypatch, tid: str, finished: bool):
    """构造指定结束标记的事件总线(隔离全局 _buses)。"""
    monkeypatch.setattr(event_bus, "_buses", {})
    event_bus.publish(tid, "status", {"status": "running"})
    if finished:
        event_bus.finish_task(tid)


def test_should_force_close_running_never_closes(monkeypatch):
    """运行中/等待中/暂停中快照 → 永不直接关闭(与总线状态无关)。"""
    _mk_bus_state(monkeypatch, "race-run", finished=True)
    for st in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED):
        assert tasks_module._should_force_close_stream(st, "race-run") is False


def test_should_force_close_completed_bus_finished(monkeypatch):
    """任务确实完成(总线已结束)→ 直接关闭。"""
    _mk_bus_state(monkeypatch, "race-done", finished=True)
    assert tasks_module._should_force_close_stream(TaskStatus.COMPLETED, "race-done") is True
    assert tasks_module._should_force_close_stream(TaskStatus.FAILED, "race-done") is True


def test_should_force_close_completed_bus_reset(monkeypatch):
    """resume/retry 已启动(总线已 reset,DB 状态未更新)→ 不关闭。"""
    _mk_bus_state(monkeypatch, "race-resume", finished=True)
    event_bus.reset_task_bus("race-resume")
    assert tasks_module._should_force_close_stream(TaskStatus.COMPLETED, "race-resume") is False
    assert tasks_module._should_force_close_stream(TaskStatus.FAILED, "race-resume") is False


def test_should_force_close_no_bus_record(monkeypatch):
    """无总线记录(从未 publish)→ 保守视为已结束关闭。"""
    monkeypatch.setattr(event_bus, "_buses", {})
    assert tasks_module._should_force_close_stream(TaskStatus.COMPLETED, "race-nobus") is True


# ============================================================
# 端点同步改状态:completed 发消息 / failed 重试
# ============================================================

class _FakeThread:
    """捕获线程创建与启动,不真正执行后台任务。"""

    instances: list["_FakeThread"] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self) -> None:
        _FakeThread.instances.append(self)


def _mk_db_and_task(status: TaskStatus):
    task_id = uuid.uuid4()
    task = MagicMock()
    task.id = task_id
    task.status = status
    task.user_id = None
    task.current_stage = ""
    task.error_message = "原错误信息"
    conv = MagicMock()
    conv.round_idx = 1
    conv.id = uuid.uuid4()
    db = MagicMock()
    db.get.return_value = task
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = conv
    return db, task, task_id


def _patch_endpoint_env(monkeypatch):
    """屏蔽端点副作用:总线/推送/日志/线程。"""
    monkeypatch.setattr(tasks_module, "reset_task_bus", lambda *a, **k: None)
    monkeypatch.setattr(tasks_module, "publish", lambda *a, **k: None)
    monkeypatch.setattr(tasks_module, "perf_log", lambda *a, **k: None)
    _FakeThread.instances = []
    monkeypatch.setattr(tasks_module.threading, "Thread", _FakeThread)


def test_submit_message_completed_syncs_running(monkeypatch):
    """completed 追问:返回响应前同步落库 RUNNING,再启动后台线程。"""
    db, task, task_id = _mk_db_and_task(TaskStatus.COMPLETED)
    _patch_endpoint_env(monkeypatch)

    resp = tasks_module.submit_task_message(
        task_id, SendMessageRequest(content="追加检查依赖漏洞"), db, None,
    )

    # 状态已同步为 RUNNING(消除 SSE 快照读到 COMPLETED 的窗口)
    assert task.status == TaskStatus.RUNNING
    assert task.current_stage == "用户追加消息,重启执行"
    # 线程启动且响应 accepted
    assert len(_FakeThread.instances) == 1
    assert _FakeThread.instances[0].kwargs["name"] == f"task-{task_id}-resume"
    assert resp.accepted is True


def test_submit_message_running_no_thread(monkeypatch):
    """running 追问:只入队,不启动 resume 线程、不改状态。"""
    db, task, task_id = _mk_db_and_task(TaskStatus.RUNNING)
    _patch_endpoint_env(monkeypatch)

    resp = tasks_module.submit_task_message(
        task_id, SendMessageRequest(content="补充要求"), db, None,
    )

    assert len(_FakeThread.instances) == 0
    assert task.status == TaskStatus.RUNNING  # 原样保持
    assert resp.accepted is True


def test_retry_syncs_running(monkeypatch):
    """failed 重试:返回响应前同步落库 RUNNING,再启动后台线程。"""
    db, task, task_id = _mk_db_and_task(TaskStatus.FAILED)
    _patch_endpoint_env(monkeypatch)

    resp = tasks_module.retry_failed_task_endpoint(task_id, db, None)

    assert task.status == TaskStatus.RUNNING
    # error_message 保留:retry_failed_task 需在进入时读取真实失败原因拼进续跑消息
    assert task.error_message == "原错误信息"
    assert task.current_stage == "重试失败任务,恢复执行"
    assert len(_FakeThread.instances) == 1
    assert _FakeThread.instances[0].kwargs["name"] == f"task-{task_id}-retry"
    assert resp.accepted is True


def test_retry_non_failed_rejected(monkeypatch):
    """非 failed 状态重试 → accepted=False,不启动线程。"""
    db, task, task_id = _mk_db_and_task(TaskStatus.COMPLETED)
    _patch_endpoint_env(monkeypatch)

    resp = tasks_module.retry_failed_task_endpoint(task_id, db, None)

    assert len(_FakeThread.instances) == 0
    assert resp.accepted is False


# ============================================================
# 后台线程早期崩溃兜底:置 FAILED + publish error + finish_task
# ============================================================


def _mk_background_crash_env(monkeypatch, executor_name):
    """构造后台线程早期崩溃环境:主函数抛异常,捕获 publish/finish。"""
    task = MagicMock()
    task.id = uuid.uuid4()
    task.status = TaskStatus.RUNNING
    task.error_message = None
    task.current_stage = ""
    db = MagicMock()
    db.get.return_value = task
    monkeypatch.setattr(tasks_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(tasks_module, executor_name, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("早期崩溃")))
    monkeypatch.setattr(tasks_module, "clear_pause_state", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(tasks_module, "publish", lambda tid, etype, data: events.append((etype, data)))
    finished = []
    monkeypatch.setattr(tasks_module, "finish_task", lambda tid: finished.append(tid))
    return task, db, events, finished


def test_resume_background_crash_publishes_error(monkeypatch):
    """resume 线程在主 try 块前崩溃 → 置 FAILED 并推 error + finish_task。"""
    task, _, events, finished = _mk_background_crash_env(
        monkeypatch, "resume_audit_with_message",
    )

    tasks_module._run_resume_in_background(str(task.id), "追问内容")

    assert task.status == TaskStatus.FAILED
    assert task.error_message == "早期崩溃"
    assert events and events[0][0] == "error"
    assert events[0][1]["error_message"] == "早期崩溃"
    assert len(finished) == 1


def test_retry_background_crash_publishes_error(monkeypatch):
    """retry 线程早期崩溃 → 置 FAILED 并推 error + finish_task。"""
    task, _, events, finished = _mk_background_crash_env(
        monkeypatch, "retry_failed_task",
    )

    tasks_module._run_retry_in_background(str(task.id))

    assert task.status == TaskStatus.FAILED
    assert task.error_message == "早期崩溃"
    assert events and events[0][0] == "error"
    assert len(finished) == 1
