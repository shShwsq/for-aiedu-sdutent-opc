"""agent2 流式调用降级 + error_message 兜底增强测试

背景:用户发追问时,agent2 评估阶段的流式 LLM 调用失败会直接 raise,
导致整个任务 FAILED,前端只显示"未知错误"。修复:
1. agent2.py:流式调用失败重试一次,仍失败返回 degraded=true 降级结果
   (不抛异常杀死任务);orchestrator 检测到降级后 resume 首次分析直接把
   用户输入内容交给 react_agent,协作轮评估降级则收尾结束;
2. orchestrator.py / tasks.py:error_message 兜底增强——异常消息为空时
   补异常类型名(_err_detail),杜绝 UI 显示"未知错误"字面。
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
            '"followup_query": "继续检查", "done": false}',
            [], "思考链",
        )

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="task-1", round_idx=1, client=MagicMock(),
    )

    assert len(calls) == 2  # 失败后重试了一次
    assert result.get("degraded") is not True
    assert result.get("followup_query") == "继续检查"


def test_stream_fail_twice_returns_degraded(monkeypatch):
    """两次流式调用都失败 → 返回降级结果(不抛异常),保留用户原始意图。"""
    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="task-1", round_idx=1, client=MagicMock(),
    )

    assert result["degraded"] is True
    assert result["done"] is False
    assert result["followup_query"] == "审计这个仓库"  # 原始用户输入透传
    assert result["degrade_reason"] == "boom"
    assert "降级" in result["reasoning"]


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

    # _finish_resume 内部导入的归纳记忆/工作区 diff:屏蔽真实副作用
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


def test_resume_analyze_degraded_passes_user_message(monkeypatch):
    """resume 首次分析降级 → 直接把用户消息交给 react_agent。"""
    task = _mk_resume_task()
    degraded = {
        "covered": [], "missing": [], "reasoning": "流式失败已降级",
        "followup_query": "被降级覆盖", "done": False,
        "degraded": True, "degrade_reason": "boom",
    }
    done = {"covered": [], "missing": [], "reasoning": "完成",
            "followup_query": "", "done": True,
            "results": [], "grouping": None}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return degraded if len(ua_calls) == 1 else done

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_resume_env(monkeypatch, executor, _ua)

    orchestrator.resume_audit_with_message(
        task, MagicMock(), "再帮我查下依赖漏洞",
    )

    # 直接把用户输入内容交给 react_agent
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "再帮我查下依赖漏洞"
    assert task.status == TaskStatus.COMPLETED


def test_resume_collab_degraded_ends_round(monkeypatch):
    """resume 协作轮评估降级 → 不再追问,以当前进度收尾结束。"""
    task = _mk_resume_task()
    analyze = {"covered": [], "missing": [], "reasoning": "分析",
               "followup_query": "查依赖", "done": False}
    degraded = {"covered": [], "missing": [], "reasoning": "评估失败降级",
                "followup_query": "", "done": False,
                "degraded": True, "degrade_reason": "boom"}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return analyze if len(ua_calls) == 1 else degraded

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_resume_env(monkeypatch, executor, _ua)

    orchestrator.resume_audit_with_message(task, MagicMock(), "再查依赖")

    # 只跑一轮,不因降级继续追问执行
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "查依赖"
    assert task.status == TaskStatus.COMPLETED


def test_resume_except_typed_error_message(monkeypatch):
    """resume 链路异常消息为空 → error_message 补异常类型名,不再显示"未知错误"。"""
    task = _mk_resume_task()

    def _boom(*args, **kwargs):
        raise RuntimeError("")

    executor = MagicMock()
    executor.name = "builtin"
    _patch_resume_env(monkeypatch, executor, _boom)

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
    monkeypatch.setattr(orchestrator, "_record_agent2", lambda *a, **k: None)
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
    """任务开始时无 agent2 初始评估:agent1 第 1 轮直接按用户意图执行。"""
    task = _mk_dual_task()
    done = {"covered": [], "missing": [], "reasoning": "完成",
            "followup_query": "", "done": True,
            "results": [{"title": "发现", "content": "内容"}], "grouping": None}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return done

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_dual_env(monkeypatch, executor, _ua)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    # agent1 先执行:第 1 轮、followup_query=None(直接用用户意图)
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["round_idx"] == 1
    assert executor.run.call_args.kwargs["followup_query"] is None
    # agent2 首次评估发生在 agent1 执行之后(第 1 轮评估)
    assert len(ua_calls) == 1
    assert ua_calls[0]["round_idx"] == 1
    assert task.status == TaskStatus.COMPLETED


def test_dual_collab_degraded_ends_round(monkeypatch):
    """dual 协作轮评估降级 → 结束协作循环,以当前进度收尾。"""
    task = _mk_dual_task()
    normal = {"covered": [], "missing": [], "reasoning": "第 1 轮评估",
              "followup_query": "查依赖", "done": False}
    degraded = {"covered": [], "missing": [], "reasoning": "评估失败降级",
                "followup_query": "", "done": False,
                "degraded": True, "degrade_reason": "boom"}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return normal if len(ua_calls) == 1 else degraded

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_dual_env(monkeypatch, executor, _ua)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    # 第 1 轮按用户意图执行 + 第 2 轮按追问执行;降级后不再继续
    assert executor.run.call_count == 2
    assert executor.run.call_args_list[0].kwargs["followup_query"] is None
    assert executor.run.call_args_list[1].kwargs["followup_query"] == "查依赖"
    assert task.status == TaskStatus.COMPLETED


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
