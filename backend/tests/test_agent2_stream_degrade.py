"""agent2 流式调用降级 + error_message 兜底增强测试

背景:用户发追问时,agent2 评估阶段的流式 LLM 调用失败会直接 raise,
导致整个任务 FAILED,前端只显示"未知错误"。修复:
1. agent2.py:流式调用失败重试一次,仍失败返回 degraded=true 降级结果
   (不抛异常杀死任务);orchestrator 检测到降级后标记审查失败,
   保留 agent1 临时结果;
2. orchestrator.py / tasks.py:error_message 兜底增强——异常消息为空时
   补异常类型名(_err_detail),杜绝 UI 显示"未知错误"字面。

另覆盖:resume 直传语义——用户追加消息不经 agent2 转述,原文直接
交给 agent1 执行(followup_query 逐字等于用户消息)。
"""
import uuid
from unittest.mock import MagicMock

# 导入关系依赖模型,确保独立测试环境下 SQLAlchemy mapper 可完成配置
# (后台线程端点内创建真实 Conversation 等记录)
import app.models.task_artifact  # noqa: F401
import app.models.user_git_binding  # noqa: F401

import app.agents.orchestrator as orchestrator
import app.agents.agent2 as agent2
import app.routers.tasks as tasks_module
from app.models.task import TaskStatus


# ============================================================
# agent2 流式调用降级(重试一次 + 降级返回)
# ============================================================


def test_stream_fail_once_retry_success(monkeypatch):
    """第一次流式调用失败 → 重试一次成功,返回正常评估结果(无降级标记)。"""
    calls = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("网络抖动")
        return (
            '{"covered": [], "missing": [], "reasoning": "重试成功", '
            '"suggestions": [], "results": [], "grouping": null}',
            [], "思考链",
        )

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="task-1", round_idx=1, client=MagicMock(),
    )

    assert len(calls) == 2  # 失败后重试了一次
    assert result.get("degraded") is not True
    assert result.get("reasoning") == "重试成功"


def test_stream_fail_twice_returns_degraded_review(monkeypatch):
    """审查:两次流式调用都失败 → 降级结果(无轮次语义,审查失败标记)。"""
    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="task-1", round_idx=1, client=MagicMock(),
    )

    assert result["degraded"] is True
    assert result["degrade_reason"] == "boom"
    assert result["results"] == []          # 空 results → 调用方标记审查失败
    assert result["suggestions"] == []
    assert "降级" in result["reasoning"]
    # 无 followup_query/done 轮次语义(一次性完整审查)
    assert "followup_query" not in result
    assert "done" not in result


# ============================================================
# resume_audit_with_message:降级分流 + 错误兜底
# ============================================================


def _mk_resume_task():
    task = MagicMock()
    task.id = "task-1"
    task.status = TaskStatus.COMPLETED
    task.current_stage = ""
    task.error_message = None
    task.user_input = "审计这个仓库"
    task.scenario = "general"
    task.user_id = None
    task.llm_config_id = None
    task.params = {}
    task.allowed_skills = None
    task.verifier_enabled = False
    task.test_env_url = ""
    return task


def _patch_resume_env(monkeypatch, executor, ua_side_effect):
    """屏蔽 resume 链路的副作用,只测降级分流。"""
    monkeypatch.setattr(orchestrator, "_build_llm_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        orchestrator, "_build_react_llm_client",
        lambda *a, **k: (MagicMock(), None),
    )
    monkeypatch.setattr(orchestrator, "_load_git_tokens", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, "set_current_git_tokens", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "get_executor", lambda *a, **k: executor)
    monkeypatch.setattr(
        orchestrator, "resolve_agent_policy",
        lambda *a, **k: {"agent2_enabled": True},
    )
    monkeypatch.setattr(orchestrator, "_publish_status", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_restore_workspace_if_needed",
                        lambda *a, **k: True)  # True → 跳过记忆文件补写
    monkeypatch.setattr(orchestrator, "_load_react_summaries", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "_get_next_round_idx", lambda *a, **k: 2)
    monkeypatch.setattr(orchestrator, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "finish_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.sandbox_tools, "mark_task_completed",
                        lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "run_agent2", ua_side_effect)

    # _run_background_review 内部导入的归纳记忆/工作区 diff:屏蔽真实副作用
    import app.services.memory_summarize as memory_summarize
    import app.services.workspace_diff as workspace_diff
    monkeypatch.setattr(memory_summarize, "summarize_and_save_memory",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_workspace_diff_artifact",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_repo_tree_artifact",
                        lambda *a, **k: None)

    # publish 全量接管:丢弃所有事件(避免碰真实事件总线)
    monkeypatch.setattr(orchestrator, "publish", lambda *a, **k: None)


def test_resume_passes_user_message_directly(monkeypatch):
    """resume:用户消息原文直传 agent1(不经 agent2 转述),
    agent2 只在 agent1 结束后做一次后台审查。"""
    task = _mk_resume_task()
    review = {"covered": [], "missing": [], "reasoning": "审查完成",
              "suggestions": [], "results": [{"title": "发现", "content": "内容"}],
              "grouping": None}
    call_order = []  # 记录 agent1 执行与 agent2 审查的先后顺序

    def _ua(*args, **kwargs):
        call_order.append("agent2")
        return review

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(
        side_effect=lambda *a, **k: call_order.append("agent1") or ([], "总结", []),
    )

    _patch_resume_env(monkeypatch, executor, _ua)

    orchestrator.resume_audit_with_message(
        task, MagicMock(), "再帮我查下依赖漏洞",
    )

    # 用户消息逐字直传 agent1,只跑一轮
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "再帮我查下依赖漏洞"
    assert task.status == TaskStatus.COMPLETED
    # agent2 仅一次调用,且发生在 agent1 执行之后(后台审查)
    assert call_order == ["agent1", "agent2"]
    assert task.review_status == "done"


def test_resume_collab_degraded_ends_round(monkeypatch):
    """resume 后台审查降级 → review_status=failed,任务仍 COMPLETED(保留临时结果)。"""
    task = _mk_resume_task()
    degraded_review = {"covered": [], "missing": [], "reasoning": "审查失败降级",
                       "suggestions": [], "results": [], "grouping": None,
                       "degraded": True, "degrade_reason": "boom"}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return degraded_review

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_resume_env(monkeypatch, executor, _ua)

    orchestrator.resume_audit_with_message(task, MagicMock(), "再查依赖")

    # agent1 只跑一轮(用户消息直传);审查降级不触发再执行
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "再查依赖"
    assert task.status == TaskStatus.COMPLETED
    # 审查降级 → 子状态 failed(任务本身不失败)
    assert task.review_status == "failed"


def test_resume_except_typed_error_message(monkeypatch):
    """resume 链路异常消息为空 → error_message 补异常类型名,不再显示"未知错误"。"""
    task = _mk_resume_task()

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(side_effect=RuntimeError(""))
    _patch_resume_env(monkeypatch, executor, lambda *a, **k: None)

    orchestrator.resume_audit_with_message(task, MagicMock(), "消息")

    assert task.status == TaskStatus.FAILED
    assert task.error_message == "RuntimeError(无错误详情)"


# ============================================================
# run_dual_agent_audit:直接执行 + 降级分流
# ============================================================


def _mk_dual_task():
    task = MagicMock()
    task.id = "task-d"
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
    return task


def _patch_dual_env(monkeypatch, executor, ua_side_effect):
    """屏蔽 dual 链路的副作用,只测执行与降级分流。"""
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
                        lambda *a, **k: (None, ""))  # 无仓库:跳过 clone
    monkeypatch.setattr(orchestrator, "_publish_status", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_record_agent2_review", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_replace_interim_results",
                        lambda *a, **k: 1)
    monkeypatch.setattr(orchestrator, "_add_conversation", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "finish_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.sandbox_tools, "mark_task_completed",
                        lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "run_agent2", ua_side_effect)

    import app.services.memory_summarize as memory_summarize
    import app.services.workspace_diff as workspace_diff
    monkeypatch.setattr(memory_summarize, "summarize_and_save_memory",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_workspace_diff_artifact",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_repo_tree_artifact",
                        lambda *a, **k: None)

    monkeypatch.setattr(orchestrator, "publish", lambda *a, **k: None)


def test_dual_starts_with_react_agent_no_initial_eval(monkeypatch):
    """任务开始时无 agent2 初始评估:agent1 第 1 轮直接按用户意图执行,
    agent2 只在 agent1 结束后做一次后台审查。"""
    task = _mk_dual_task()
    review = {"covered": [], "missing": [], "reasoning": "审查完成",
              "suggestions": [], "results": [{"title": "发现", "content": "内容"}],
              "grouping": None}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return review

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_dual_env(monkeypatch, executor, _ua)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    # agent1 先执行:第 1 轮、followup_query=None(直接用用户意图),只此一轮
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["round_idx"] == 1
    assert executor.run.call_args.kwargs["followup_query"] is None
    # agent2 审查仅一次,发生在 agent1 执行之后(单次完整审查)
    assert len(ua_calls) == 1
    assert task.status == TaskStatus.COMPLETED
    assert task.review_status == "done"


def test_dual_collab_degraded_ends_round(monkeypatch):
    """dual 后台审查降级 → review_status=failed,任务仍 COMPLETED,
    agent1 只跑一轮(多轮由用户驱动,降级不触发再执行)。"""
    task = _mk_dual_task()
    degraded_review = {"covered": [], "missing": [], "reasoning": "审查失败降级",
                       "suggestions": [], "results": [], "grouping": None,
                       "degraded": True, "degrade_reason": "boom"}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return degraded_review

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_dual_env(monkeypatch, executor, _ua)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    # 初始运行只有 1 轮 agent1(协作循环已移除)
    assert executor.run.call_count == 1
    assert executor.run.call_args.kwargs["followup_query"] is None
    assert task.status == TaskStatus.COMPLETED
    # 审查降级 → 子状态 failed(任务本身不失败,保留临时结果)
    assert task.review_status == "failed"


# ============================================================
# 纯对话轮:本轮无工具调用 → 跳过后台审查与重下游
# ============================================================


def _mk_no_tool_db():
    """db mock:_round_has_tool_calls 的 query→filter→first 链返回 None
    (即本轮没有任何 tool_call 记录)。"""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    return db


def test_dual_conversation_round_skips_review(monkeypatch):
    """初始运行纯对话轮(如问候,无工具调用)→ agent2 不被调用,任务直接完成。"""
    task = _mk_dual_task()
    ua_calls = []

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "你好!很高兴帮你分析代码。", []))

    _patch_dual_env(monkeypatch, executor, lambda *a, **k: ua_calls.append(1))

    orchestrator.run_dual_agent_audit(task, _mk_no_tool_db())

    executor.run.assert_called_once()
    assert not ua_calls  # 纯对话轮不触发 agent2 审查
    assert task.status == TaskStatus.COMPLETED
    assert "纯对话" in task.current_stage


def test_resume_conversation_round_skips_review(monkeypatch):
    """resume 纯对话轮(追问直接回答,无工具调用)→ 跳过审查/练习题/记忆,
    保留既有结果与上一轮审查状态。"""
    task = _mk_resume_task()
    task.review_status = "done"  # 上一轮(分析轮)的审查状态
    ua_calls = []

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "这个函数用于校验入参。", []))

    _patch_resume_env(monkeypatch, executor, lambda *a, **k: ua_calls.append(1))

    orchestrator.resume_audit_with_message(task, _mk_no_tool_db(), "这个函数是干嘛的?")

    # 用户消息仍逐字直传 agent1
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "这个函数是干嘛的?"
    assert not ua_calls  # 纯对话轮不触发 agent2 审查
    assert task.status == TaskStatus.COMPLETED
    assert "纯对话" in task.current_stage
    # 既有审查状态保留(未被置为 running/failed)
    assert task.review_status == "done"


# ============================================================
# _err_detail:error_message 兜底增强
# ============================================================


def test_err_detail_empty_message():
    """异常消息为空 → 补异常类型名;有消息 → 原样返回。"""
    assert orchestrator._err_detail(RuntimeError("boom")) == "boom"
    assert orchestrator._err_detail(RuntimeError()) == "RuntimeError(无错误详情)"
    assert orchestrator._err_detail(ValueError("")) == "ValueError(无错误详情)"


def test_resume_background_except_typed_error_message(monkeypatch):
    """tasks.py 后台线程 except:空消息异常 → error_message 补异常类型名。"""
    task_id = str(uuid.uuid4())
    task = MagicMock()
    task.id = uuid.UUID(task_id)
    task.status = TaskStatus.RUNNING
    task.error_message = None
    db = MagicMock()
    db.get.return_value = task

    monkeypatch.setattr(tasks_module, "SessionLocal", lambda: db)

    def _boom(*a, **k):
        raise RuntimeError("")

    monkeypatch.setattr(tasks_module, "resume_audit_with_message", _boom)
    error_events = []
    monkeypatch.setattr(
        tasks_module, "publish",
        lambda tid, event, data: error_events.append(data) if event == "error" else None,
    )
    monkeypatch.setattr(tasks_module, "finish_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks_module, "clear_pause_state", lambda *a, **k: None)

    tasks_module._run_resume_in_background(task_id, "消息")

    assert task.status == TaskStatus.FAILED
    assert task.error_message == "RuntimeError(无错误详情)"
    assert error_events
    assert error_events[0]["error_message"] == "RuntimeError(无错误详情)"
