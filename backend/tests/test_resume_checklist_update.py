"""追问清单更新机制单测(agent2 更新模式 + resume 链路确认环节)。

核心约束:
- 用户追问后,agent2 可输出更新后的完整 checklist;有实质变更时
  orchestrator 复用第 0 轮确认机制再次向用户确认,确认后覆写 task.checklist
- 用户确认新清单后强制 done=False,react_agent 必须跑一轮执行追问需求
- checklist 无实质变更 / retry 续跑 → 不触发确认弹窗
"""
import json
from unittest.mock import MagicMock

# 导入关系依赖模型,确保独立测试环境下 SQLAlchemy mapper 可完成配置
# (_add_conversation 创建真实 Conversation,Task/User 的 relationship
# 指向 TaskArtifact/UserGitBinding,未注册时 mapper 初始化报错)
import app.models.task_artifact  # noqa: F401
import app.models.user_git_binding  # noqa: F401

import app.agents.orchestrator as orchestrator
from app.agents.orchestrator import _checklist_changed
from app.agents.agent2 import CHECKLIST_UPDATE_SECTION, run_agent2
from app.models.task import TaskStatus


# ============================================================
# _checklist_changed:实质差异判断
# ============================================================


def _dim(dim_id, name="维度", desc="描述", items=None):
    return {
        "id": dim_id, "name": name, "description": desc,
        "checklist": items if items is not None else ["子项1"],
    }


def test_checklist_changed_identical():
    """内容完全一致 → 无变更(避免无意义确认弹窗)。"""
    old = [_dim("a"), _dim("b")]
    new = [_dim("a"), _dim("b")]
    assert _checklist_changed(old, new) is False


def test_checklist_changed_added_dimension():
    """新增维度 → 视为变更。"""
    old = [_dim("a")]
    new = [_dim("a"), _dim("new_dim", name="新维度")]
    assert _checklist_changed(old, new) is True


def test_checklist_changed_modified_items():
    """子项修改 → 视为变更。"""
    old = [_dim("a", items=["子项1"])]
    new = [_dim("a", items=["子项1", "子项2"])]
    assert _checklist_changed(old, new) is True


def test_checklist_changed_old_none():
    """旧清单为 None(理论不发生在 resume)且新清单非空 → 视为变更。"""
    assert _checklist_changed(None, [_dim("a")]) is True


# ============================================================
# run_agent2:追问清单更新模式的 prompt 组装
# ============================================================


class _MockChunk:
    def __init__(self, reasoning_delta="", content_delta="",
                 tool_call_deltas=None, finish_reason=None):
        self.reasoning_delta = reasoning_delta
        self.content_delta = content_delta
        self.tool_call_deltas = tool_call_deltas or []
        self.finish_reason = finish_reason


def _mk_client(chunks):
    client = MagicMock()
    client.chat_stream = MagicMock(return_value=iter(chunks))
    return client


def _eval_json(**overrides):
    result = {
        "covered": ["a"],
        "missing": [],
        "reasoning": "追问引入新维度",
        "followup_query": "请补充检查新维度",
        "done": False,
        "ask_user": False,
        "questions": [],
    }
    result.update(overrides)
    return json.dumps(result, ensure_ascii=False)


def _last_messages(client):
    args, kwargs = client.chat_stream.call_args
    return args[0] if args else kwargs["messages"]


def test_update_mode_prompt_and_hint_injected():
    """更新模式开启 → system prompt 附加更新规则,user_msg 附追问提示。"""
    client = _mk_client([
        _MockChunk(content_delta=_eval_json(checklist=[_dim("a"), _dim("n")]),
                   finish_reason="stop"),
    ])
    result = run_agent2(
        "原始意图\n\n[用户追加消息]\n再查下依赖漏洞",
        [{"round": 1, "summary": "第一轮总结"}],
        task_id="task-1", db=None, round_idx=2,
        client=client, ask_round=2, task=None,
        task_checklist=[_dim("a")],
        checklist_update_mode=True,
    )

    messages = _last_messages(client)
    assert CHECKLIST_UPDATE_SECTION in messages[0]["content"]
    assert "追问清单更新" in messages[1]["content"]
    # checklist 字段原样透传,供 orchestrator 判断
    assert [d["id"] for d in result["checklist"]] == ["a", "n"]


def test_update_mode_off_no_extra_prompt():
    """默认(非追问)评估 → 不附加更新规则段。"""
    client = _mk_client([
        _MockChunk(content_delta=_eval_json(), finish_reason="stop"),
    ])
    run_agent2(
        "原始意图", [{"round": 1, "summary": "第一轮总结"}],
        task_id="task-1", db=None, round_idx=2,
        client=client, ask_round=2, task=None,
        task_checklist=[_dim("a")],
    )
    messages = _last_messages(client)
    assert CHECKLIST_UPDATE_SECTION not in messages[0]["content"]
    assert "追问清单更新" not in messages[1]["content"]


# ============================================================
# resume_audit_with_message:追问清单确认环节
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
    task.checklist = [_dim("a")]
    return task


def _patch_resume_env(monkeypatch, executor, ua_side_effect,
                      confirmed=None, set_pending=None, publish_events=None):
    """屏蔽 resume 链路的副作用,只测清单确认分流。

    confirmed:wait_for_checklist_confirmation 的返回值(模拟用户提交)
    set_pending / publish_events:捕获清单推送的列表
    """
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

    # publish 全量接管:仅捕获 checklist_review,其余丢弃(避免碰真实事件总线)
    def _pub(tid, event, data):
        if event == "checklist_review" and publish_events is not None:
            publish_events.append(data)
    monkeypatch.setattr(orchestrator, "publish", _pub)

    if set_pending is not None:
        monkeypatch.setattr(
            orchestrator, "set_pending_checklist",
            lambda tid, cl: set_pending.append(cl),
        )
    if confirmed is not None:
        monkeypatch.setattr(
            orchestrator, "wait_for_checklist_confirmation",
            lambda tid: confirmed,
        )


def test_resume_checklist_update_triggers_confirmation(monkeypatch):
    """追问引入新维度 → 推送确认,用户确认后覆写 checklist 并强制执行一轮。"""
    task = _mk_resume_task()
    new_checklist = [_dim("a"), _dim("dep_vuln", name="依赖漏洞")]

    ua_results = [
        # 首次分析:LLM 输出更新后 checklist(即使同时 done=true 也必须执行)
        {"covered": ["a"], "missing": [], "reasoning": "追问引入新维度",
         "followup_query": "请检查依赖漏洞", "done": True,
         "ask_user": False, "checklist": new_checklist},
        # 协作轮评估:react_agent 跑完后宣布完成
        {"covered": ["a", "dep_vuln"], "missing": [], "reasoning": "全部覆盖",
         "followup_query": "", "done": True, "ask_user": False,
         "results": [], "grouping": None},
    ]
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return ua_results[len(ua_calls) - 1]

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "第二轮总结", []))

    set_pending, publish_events = [], []
    _patch_resume_env(
        monkeypatch, executor, _ua,
        confirmed=new_checklist,
        set_pending=set_pending, publish_events=publish_events,
    )

    orchestrator.resume_audit_with_message(
        task, MagicMock(), "再帮我查下依赖漏洞",
    )

    # 首次分析启用更新模式;协作轮评估不启用(仅分析追问时判断维度变更)
    assert ua_calls[0]["checklist_update_mode"] is True
    assert ua_calls[1].get("checklist_update_mode", False) is False
    # 推送了待确认清单 + checklist_review 事件
    assert set_pending and set_pending[0] == new_checklist
    assert publish_events and publish_events[0]["checklist"] == new_checklist
    # 确认后覆写 task.checklist
    assert task.checklist == new_checklist
    # done 被强制关闭 → react_agent 跑了一轮,追问指令透传
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "请检查依赖漏洞"
    # 协作轮评估用新清单
    assert ua_calls[1]["task_checklist"] == new_checklist
    # 任务最终完成
    assert task.status == TaskStatus.COMPLETED


def test_resume_checklist_unchanged_no_confirmation(monkeypatch):
    """agent2 输出与原清单相同 → 不触发确认,done=true 直接结束。"""
    task = _mk_resume_task()

    ua_result = {"covered": ["a"], "missing": [], "reasoning": "无需新检查",
                 "followup_query": "", "done": True, "ask_user": False,
                 "checklist": [_dim("a")], "results": [], "grouping": None}

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock()

    set_pending = []
    _patch_resume_env(
        monkeypatch, executor, lambda *a, **k: ua_result,
        set_pending=set_pending,
    )

    orchestrator.resume_audit_with_message(task, MagicMock(), "好的谢谢")

    assert set_pending == []          # 未推送确认
    executor.run.assert_not_called()  # 直接结束,未空跑
    assert task.status == TaskStatus.COMPLETED


def test_resume_retry_never_updates_checklist(monkeypatch):
    """retry 续跑 → 不启用更新模式,即使输出 checklist 也不触发确认。"""
    task = _mk_resume_task()

    ua_results = [
        {"covered": ["a"], "missing": [], "reasoning": "续跑",
         "followup_query": "继续", "done": False, "ask_user": False,
         "checklist": [_dim("a"), _dim("x")]},
        {"covered": ["a"], "missing": [], "reasoning": "完成",
         "followup_query": "", "done": True, "ask_user": False,
         "results": [], "grouping": None},
    ]
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return ua_results[len(ua_calls) - 1]

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    set_pending = []
    _patch_resume_env(
        monkeypatch, executor, _ua,
        set_pending=set_pending,
    )

    orchestrator.resume_audit_with_message(
        task, MagicMock(), "重试续跑消息", retry=True,
    )

    assert ua_calls[0]["checklist_update_mode"] is False
    assert set_pending == []           # retry 不触发确认
    assert task.checklist == [_dim("a")]  # 原清单不变
    executor.run.assert_called_once()


def test_resume_confirmation_cancelled_keeps_old_checklist(monkeypatch):
    """等待期间任务被取消(返回空清单)→ 保留旧清单,不强制跑新一轮。"""
    task = _mk_resume_task()
    new_checklist = [_dim("a"), _dim("dep_vuln")]

    ua_result = {"covered": ["a"], "missing": [], "reasoning": "追问引入新维度",
                 "followup_query": "请检查依赖漏洞", "done": False,
                 "ask_user": False, "checklist": new_checklist}

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    set_pending = []
    _patch_resume_env(
        monkeypatch, executor, lambda *a, **k: dict(ua_result),
        confirmed=[],  # 模拟取消:wait 返回空
        set_pending=set_pending,
    )

    orchestrator.resume_audit_with_message(task, MagicMock(), "再查依赖漏洞")

    assert set_pending  # 推送过确认
    assert task.checklist == [_dim("a")]  # 旧清单保留
    # done=False(原始输出)→ 协作循环仍会用旧清单跑(不因取消改变语义)
    executor.run.assert_called()


def test_resume_first_react_round_shares_round_idx(monkeypatch):
    """分析评估与首轮 react 执行共享起始轮号,不单独占轮。"""
    task = _mk_resume_task()

    ua_results = [
        {"covered": [], "missing": [], "reasoning": "分析",
         "followup_query": "查依赖", "done": False, "ask_user": False},
        {"covered": ["a"], "missing": [], "reasoning": "完成",
         "followup_query": "", "done": True, "ask_user": False,
         "results": [], "grouping": None},
    ]
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return ua_results[len(ua_calls) - 1]

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_resume_env(monkeypatch, executor, _ua)

    db = MagicMock()
    orchestrator.resume_audit_with_message(task, db, "再查依赖")

    # 分析评估(round_idx=2)后,首轮 react 执行仍在 round_idx=2(共享轮号)
    assert executor.run.call_args.kwargs["round_idx"] == 2
    assert executor.run.call_args.kwargs["followup_query"] == "查依赖"
    # 分析评估落库 round_idx=2(与 react 执行同轮,不单独占轮)
    added = [
        c.args[0] for c in db.add.call_args_list
        if c.args and getattr(c.args[0], "role", None) == "agent2"
    ]
    assert added and added[0].round_idx == 2
