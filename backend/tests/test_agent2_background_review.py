"""agent2 后台审查流程测试(计划:agent2 后台审查重构)

覆盖新流程的关键契约:
- agent1 结束即任务完成(推 agent1_done,总线保持打开);done 在审查后
- 审查完成:临时结果被重点与知识点整体替换,review_status=done
- 审查失败:任务仍 COMPLETED,临时结果保留,review_status=failed
- review_status 流转:running(审查开始)→ done/failed
- 审查互斥注册表:register_pending_resume 原子路由(queued/enqueue/immediate)
- 核查中追问排队:审查结束自动 resume(总线保持打开,不推 done)
- 单 agent 模式:无审查事件,review_status 不动
- suggestions 落库契约(前端解析 type=suggestions 渲染深挖卡片)
- 临时结果助手 _replace_interim_results:先清后落单条
"""
import json
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  (mapper 依赖)
import app.models.user_git_binding  # noqa: F401

import app.agents.orchestrator as orchestrator
from app.models.task import Conversation, Result, TaskStatus


# ============================================================
# 测试环境构造(参考 test_agent2_stream_degrade 的屏蔽模式)
# ============================================================


def _mk_task():
    task = MagicMock()
    task.id = "task-br"
    task.status = TaskStatus.PENDING
    task.current_stage = ""
    task.error_message = None
    task.user_input = "审计这个仓库"
    task.scenario = "general"
    task.user_id = None
    task.llm_config_id = None
    task.react_llm_config_id = None
    task.params = {}
    task.allowed_skills = None
    task.executor = "builtin"
    task.review_status = None
    return task


def _mk_review_result(**overrides):
    result = {
        "covered": ["injection"],
        "missing": [],
        "reasoning": "审查通过",
        "suggestions": [],
        "results": [
            {"title": "知识点1", "content": "说明1",
             "metadata": {"learning_note": "值得学"}},
            {"title": "知识点2", "content": "说明2",
             "metadata": {"learning_note": "也值得学"}},
        ],
        "grouping": None,
    }
    result.update(overrides)
    return result


def _patch_env(monkeypatch, executor, ua_side_effect):
    """屏蔽 dual 链路副作用,保留审查链真实逻辑(结果替换/事件时序)。"""
    monkeypatch.setattr(orchestrator, "resolve_agent_policy",
                        lambda *a, **k: {"agent2_enabled": True})
    monkeypatch.setattr(orchestrator, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_build_llm_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(orchestrator, "_build_react_llm_client",
                        lambda *a, **k: (MagicMock(), None))
    monkeypatch.setattr(orchestrator, "_load_git_tokens", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, "set_current_git_tokens", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "get_executor", lambda *a, **k: executor)
    monkeypatch.setattr(orchestrator, "_prepare_repo_context",
                        lambda *a, **k: (None, ""))
    monkeypatch.setattr(orchestrator, "_publish_status", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "run_agent2", ua_side_effect)

    import app.services.memory_summarize as memory_summarize
    import app.services.workspace_diff as workspace_diff
    monkeypatch.setattr(memory_summarize, "summarize_and_save_memory",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_workspace_diff_artifact",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_repo_tree_artifact",
                        lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.sandbox_tools, "mark_task_completed",
                        lambda *a, **k: None)


class _EventRecorder:
    """记录 publish/finish_task 调用顺序(时序断言用)。"""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        monkeypatch.setattr(orchestrator, "publish", self._publish)
        monkeypatch.setattr(orchestrator, "finish_task", self._finish)

    def _publish(self, task_id, event, data=None):
        self.calls.append(("publish", event, data))

    def _finish(self, task_id):
        self.calls.append(("finish",))

    def events(self, name):
        return [c for c in self.calls if c[0] == "publish" and c[1] == name]

    def index(self, marker):
        for i, c in enumerate(self.calls):
            if c == marker or (c[0] == "publish" and c[1] == marker):
                return i
        return -1


# ============================================================
# 事件时序:agent1_done → (审查) → review_done → done → finish
# ============================================================


def test_agent1_done_before_done_and_bus_stays_open(monkeypatch):
    """agent1_done 在 done 之前推送;finish_task(总线关闭)发生在审查后。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result())
    rec = _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    # agent1_done 早于 review_done,review_done 早于 done
    assert rec.index("agent1_done") != -1
    assert rec.index("agent1_done") < rec.index("review_done")
    assert rec.index("review_done") < rec.index("done")
    # finish_task(总线关闭)在 agent1_done 之后(审查期间总线保持打开)
    assert rec.index("agent1_done") < rec.index(("finish",))
    # 事件 data 契约
    assert rec.events("agent1_done")[0][2] == {"status": "completed"}
    assert rec.events("review_done")[0][2] == {"review_status": "done"}


def test_review_status_transitions_running_to_done(monkeypatch):
    """审查期间 review_status=running,完成后=done。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    observed = {}

    def _ua(*args, **kwargs):
        observed["review_status_at_review"] = task.review_status
        return _mk_review_result()

    _patch_env(monkeypatch, executor, _ua)
    _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    assert observed["review_status_at_review"] == "running"
    assert task.review_status == "done"
    assert task.status == TaskStatus.COMPLETED


# ============================================================
# 结果替换:临时结果 → 重点与知识点
# ============================================================


def test_review_replaces_interim_results_with_knowledge(monkeypatch):
    """审查完成:临时结果被 agent2 知识点整体替换(先删全部再落新)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    db = MagicMock()

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result())
    _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, db)

    results_added = [
        c.args[0] for c in db.add.call_args_list
        if isinstance(c.args[0], Result)
    ]
    # 1 条临时(agent1 summary)+ 2 条知识点(agent2)
    assert len(results_added) == 3
    assert "检查助手整理中" in results_added[0].title
    assert [r.title for r in results_added[1:]] == ["知识点1", "知识点2"]
    assert results_added[1].metadata_ == {"learning_note": "值得学"}
    # 替换 = 两次全删(临时落库时 1 次 + 审查完成时 1 次)
    delete_calls = [
        c for c in db.query.call_args_list if c.args and c.args[0] is Result
    ]
    assert len(delete_calls) == 2


def test_review_failure_keeps_interim_results(monkeypatch):
    """审查降级:任务仍 COMPLETED,临时结果保留(不新增不删除),警告审查卡落库。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    db = MagicMock()
    degraded = _mk_review_result(
        results=[], degraded=True, degrade_reason="boom",
    )

    _patch_env(monkeypatch, executor, lambda *a, **k: degraded)
    rec = _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, db)

    assert task.status == TaskStatus.COMPLETED  # 任务不因审查失败而失败
    assert task.review_status == "failed"
    # 临时结果保留:只有 agent1 summary 那 1 条,审查未增删
    results_added = [
        c.args[0] for c in db.add.call_args_list
        if isinstance(c.args[0], Result)
    ]
    assert len(results_added) == 1
    assert "检查助手整理中" in results_added[0].title
    delete_calls = [
        c for c in db.query.call_args_list if c.args and c.args[0] is Result
    ]
    assert len(delete_calls) == 1  # 仅临时结果落库时的那次全删
    # review_done 仍推送(failed 语义),done 正常终止
    assert rec.events("review_done")[0][2] == {"review_status": "failed"}
    assert rec.index("review_done") < rec.index("done")


# ============================================================
# suggestions 落库契约(前端深挖卡片的数据源)
# ============================================================


def test_record_agent2_review_persists_suggestions_json():
    """有建议 → type=suggestions 的 Conversation 落 JSON;无建议 → 不落。"""
    task = _mk_task()
    db = MagicMock()

    with_suggestions = _mk_review_result(
        suggestions=["深挖依赖漏洞供应链", "复核第 2 条结论的误报"],
    )
    orchestrator._record_agent2_review(db, task, 1, with_suggestions)

    added = [c.args[0] for c in db.add.call_args_list]
    suggestions_convs = [c for c in added if isinstance(c, Conversation)
                         and c.type == "suggestions"]
    assert len(suggestions_convs) == 1
    payload = json.loads(suggestions_convs[0].content)
    assert payload["suggestions"] == ["深挖依赖漏洞供应链", "复核第 2 条结论的误报"]
    # 审查结论卡 + 最终总结卡同轮落库
    assert [c.type for c in added if isinstance(c, Conversation)] == [
        "review", "suggestions", "summary",
    ]

    # 无建议:不落 suggestions 卡
    db2 = MagicMock()
    orchestrator._record_agent2_review(db2, task, 1, _mk_review_result())
    added2 = [c.args[0] for c in db2.add.call_args_list]
    assert not [
        c for c in added2 if isinstance(c, Conversation) and c.type == "suggestions"
    ]


def test_record_agent2_review_failure_card_wording():
    """审查失败 → review 卡 content 提示保留执行结果。"""
    task = _mk_task()
    db = MagicMock()
    orchestrator._record_agent2_review(
        db, task, 1, _mk_review_result(results=[], degraded=True),
    )
    added = [c.args[0] for c in db.add.call_args_list]
    review_conv = next(c for c in added
                       if isinstance(c, Conversation) and c.type == "review")
    assert "审查未完成" in review_conv.content


def test_replace_interim_results_clears_and_writes_single_row():
    """_replace_interim_results:删全部旧 Result,落 1 条临时结果。"""
    task = _mk_task()
    db = MagicMock()

    count = orchestrator._replace_interim_results(db, task, 2, "本轮总结")

    assert count == 1
    db.query(Result).filter.assert_called_once()
    db.query(Result).filter.return_value.delete.assert_called_once()
    added = [c.args[0] for c in db.add.call_args_list]
    assert len(added) == 1
    assert added[0].round_idx == 2
    assert added[0].content == "本轮总结"
    assert "检查助手整理中" in added[0].title


# ============================================================
# 审查互斥注册表:register_pending_resume 原子路由 + _seal_review
# ============================================================


def _clear_registry(task_id: str) -> None:
    """清理注册表残留(测试隔离:pending/claim/event)"""
    with orchestrator._review_lock:
        orchestrator._review_done_events.pop(task_id, None)
        orchestrator._pending_resume_messages.pop(task_id, None)
        orchestrator._resume_claims.discard(task_id)


def test_register_pending_resume_routes():
    """三路由:无审查=immediate;审查中=queued(登记);claim 窗口=enqueue。"""
    task_id = "task-route"
    try:
        # 无审查注册 → immediate
        assert orchestrator.register_pending_resume(task_id, "消息") == "immediate"

        # 审查中 → queued + 登记(含附件)
        orchestrator._mark_review_started(task_id)
        assert orchestrator.register_pending_resume(
            task_id, "深挖依赖", ["upload-1"],
        ) == "queued"
        with orchestrator._review_lock:
            pending = orchestrator._pending_resume_messages[task_id]
        assert pending == [("深挖依赖", ["upload-1"])]

        # claim 窗口(queued resume 已接管)→ enqueue(claim 优先于审查事件)
        with orchestrator._review_lock:
            orchestrator._resume_claims.add(task_id)
        assert orchestrator.register_pending_resume(task_id, "再补一条") == "enqueue"
    finally:
        _clear_registry(task_id)


def test_seal_review_takes_pending_and_claims():
    """_seal_review:注销审查事件 + 取走排队消息;有消息时登记 claim。"""
    task_id = "task-seal"
    try:
        ev = orchestrator._mark_review_started(task_id)
        orchestrator.register_pending_resume(task_id, "排队消息")

        pending = orchestrator._seal_review(task_id)

        assert pending == [("排队消息", None)]
        with orchestrator._review_lock:
            assert task_id not in orchestrator._review_done_events  # 事件已注销
            assert task_id not in orchestrator._pending_resume_messages  # 消息已取走
            assert task_id in orchestrator._resume_claims  # 已接管
        assert ev.is_set() is False  # ev 由 _finalize_review 置位,seal 不动
    finally:
        _clear_registry(task_id)


# ============================================================
# 核查中追问排队:审查结束自动 resume(总线保持打开)
# ============================================================


def test_pending_message_auto_resumes_without_done(monkeypatch):
    """审查期间注册的追问 → 审查结束后自动 launch_resume_thread(queued),
    不推 done、不 finish_task(总线保持打开,前端 SSE 不断线)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    # 在审查执行期间(run_agent2 回调)注册排队消息,模拟核查中用户追问
    def _ua(*args, **kwargs):
        orchestrator.register_pending_resume(task.id, "深挖一下依赖漏洞")
        return _mk_review_result()

    _patch_env(monkeypatch, executor, _ua)

    launched = []
    monkeypatch.setattr(
        orchestrator, "launch_resume_thread",
        lambda tid, msg, upload_ids=None, queued=False:
            launched.append((tid, msg, upload_ids, queued)),
    )
    rec = _EventRecorder(monkeypatch)

    try:
        orchestrator.run_dual_agent_audit(task, MagicMock())
    finally:
        _clear_registry(str(task.id))

    # 自动 resume 启动:消息原文 + queued 标志
    assert launched == [(str(task.id), "深挖一下依赖漏洞", None, True)]
    # review_done 照常推送(侧栏 badge 更新),但不推 done / finish(总线打开)
    assert rec.events("review_done")
    assert not rec.events("done")
    assert rec.index(("finish",)) == -1


def test_no_pending_message_publishes_done(monkeypatch):
    """无排队消息 → 审查结束推 done + finish_task(原行为不变)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result())

    launched = []
    monkeypatch.setattr(
        orchestrator, "launch_resume_thread",
        lambda tid, msg, upload_ids=None, queued=False:
            launched.append((tid, msg, upload_ids, queued)),
    )
    rec = _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    assert launched == []  # 无自动 resume
    assert rec.index("review_done") < rec.index("done")  # done 照常
    assert rec.index("done") < rec.index(("finish",))    # finish 照常


def test_multiple_pending_messages_merged(monkeypatch):
    """多条排队消息合并为一次 resume(消息 \n\n 连接,附件去重保序)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    def _ua(*args, **kwargs):
        orchestrator.register_pending_resume(task.id, "第一条", ["u1", "u2"])
        orchestrator.register_pending_resume(task.id, "第二条", ["u2", "u3"])
        return _mk_review_result()

    _patch_env(monkeypatch, executor, _ua)

    launched = []
    monkeypatch.setattr(
        orchestrator, "launch_resume_thread",
        lambda tid, msg, upload_ids=None, queued=False:
            launched.append((tid, msg, upload_ids, queued)),
    )
    _EventRecorder(monkeypatch)

    try:
        orchestrator.run_dual_agent_audit(task, MagicMock())
    finally:
        _clear_registry(str(task.id))

    assert len(launched) == 1
    tid, msg, upload_ids, queued = launched[0]
    assert msg == "第一条\n\n第二条"
    assert upload_ids == ["u1", "u2", "u3"]  # 去重保序
    assert queued is True


# ============================================================
# 单 agent 模式:无审查,行为不变
# ============================================================


def test_single_agent_mode_no_review_events(monkeypatch):
    """agent2 禁用 → 无 agent1_done/review_done,review_status 保持 None。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    ua_called = []

    _patch_env(monkeypatch, executor, lambda *a, **k: ua_called.append(1))
    monkeypatch.setattr(
        orchestrator, "resolve_agent_policy",
        lambda *a, **k: {"agent2_enabled": False},
    )
    rec = _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    assert task.status == TaskStatus.COMPLETED
    assert task.review_status is None          # 不进入审查状态机
    assert not ua_called                        # agent2 全程未被调用
    assert not rec.events("agent1_done")
    assert not rec.events("review_done")
    assert len(rec.events("done")) == 1         # 单 agent:done 直接终止
    assert rec.index("done") < rec.index(("finish",))
