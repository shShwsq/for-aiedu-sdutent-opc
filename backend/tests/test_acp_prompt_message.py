"""acp_base._build_prompt_message 单元测试(纯函数,不连 DB / 不连沙箱)。

覆盖 CLI 智能体 prompt 中项目记忆精简版 + 全局记忆的注入与拼接顺序,
以及"纯指令(落库展示)与记忆注入段(只进发送内容)"的拆分。
"""
from unittest.mock import MagicMock

from app.agents.acp_base import (
    _build_base_prompt,
    _build_cli_interrupt_message,
    _build_memory_section,
    _build_prompt_message,
    _build_repo_context_section,
)


def _mk_task(user_input="审计这个仓库的安全问题", params=None):
    """构造最小 Task mock:_build_prompt_message 只用到 user_input / params。"""
    t = MagicMock()
    t.user_input = user_input
    t.params = params if params is not None else {"repo_url": "https://github.com/a/b"}
    return t


def test_first_round_includes_project_memory_and_file_hint():
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="## Hard Constraints\n- rule A",
    )
    assert "[项目记忆摘要]" in msg
    assert "## Hard Constraints\n- rule A" in msg
    assert "/home/user/.agent_memory/project_memory.md" in msg


def test_first_round_includes_global_memory():
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        global_memory="The following is general experience accumulated across tasks, organized by category (follow during execution):\n## Tech Stack\n- PG",
    )
    assert "The following is general experience accumulated across tasks" in msg
    assert "## Tech Stack\n- PG" in msg


def test_project_memory_before_global_memory():
    """项目记忆(具体)在前,全局记忆(通用)在后。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM_MARKER",
        global_memory="GLOBAL_MEM_MARKER",
    )
    assert msg.index("PROJECT_MEM_MARKER") < msg.index("GLOBAL_MEM_MARKER")


def test_empty_memories_not_appended():
    """memory_summary / global_memory 为空 → 不追加对应段。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="", global_memory="",
    )
    assert "[项目记忆摘要]" not in msg
    assert "项目记忆" not in msg


def test_followup_round_also_includes_memories():
    """追问轮同样注入记忆(与 react_agent system prompt 每轮都在一致)。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 2, "请重点检查 SQL 注入", None, "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM",
        global_memory="GLOBAL_MEM",
    )
    # 追问段标签为中性措辞,不暴露内部角色,也不绑定审计/审查等场景词
    assert "[本轮补充要求]" in msg
    assert "agent2" not in msg.split("[本轮补充要求]")[0]
    assert "请重点检查 SQL 注入" in msg
    assert "PROJECT_MEM" in msg
    assert "GLOBAL_MEM" in msg
    # 无沙箱会话(工作区不可探测)时不得声称"已 clone",避免误导跳过 clone
    assert "已 clone" not in msg


def test_previous_plan_and_memories_coexist():
    """跨轮 plan 提醒 + 记忆段可共存,互不干扰。"""
    task = _mk_task()
    previous_plan = [{"text": "检查鉴权", "status": "done"}]
    msg = _build_prompt_message(
        task, 2, "继续检查", None, "/home/user/repos/r", previous_plan,
        memory_summary="PROJECT_MEM", global_memory="GLOBAL_MEM",
    )
    assert "PROJECT_MEM" in msg
    assert "GLOBAL_MEM" in msg


# ---------- 纯指令 / 记忆段拆分(落库不含记忆注入段) ----------

def test_base_prompt_excludes_memories():
    """_build_base_prompt 只含纯指令,不含项目/全局记忆段(落库展示用)。"""
    task = _mk_task()
    base = _build_base_prompt(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
    )
    assert task.user_input in base
    assert "[项目记忆摘要]" not in base
    assert "general experience accumulated" not in base


def test_base_prompt_excludes_repo_context():
    """_build_base_prompt 不含预 clone 上下文段(落库展示用,前端不展示)。"""
    task = _mk_task(
        params={"repo_url": "https://github.com/a/b", "branch": "main"},
    )
    base = _build_base_prompt(
        task, 1, None, "仓库 xxx 已克隆到 /home/user/repos/r", "/home/user/repos/r", None,
    )
    assert "仓库已预先 clone" not in base
    assert "已克隆到" not in base
    # 仓库地址/分支属用户选择的元信息,仍保留展示
    assert "仓库地址: https://github.com/a/b" in base
    assert "分支: main" in base


def test_repo_context_section_roundtrip():
    """_build_repo_context_section:有内容时包裹提示语,空时返回空串。"""
    assert _build_repo_context_section(None) == ""
    assert _build_repo_context_section("") == ""
    section = _build_repo_context_section("仓库 xxx 已克隆到 /home/user/repos/r")
    assert "仓库已预先 clone" in section
    assert "已克隆到 /home/user/repos/r" in section


def test_memory_section_contains_both_and_global_file_hint():
    """_build_memory_section 含项目记忆 + 全局记忆 + 全局记忆文件路径提示。"""
    section = _build_memory_section(
        memory_summary="PROJECT_MEM", global_memory="GLOBAL_MEM",
    )
    assert "PROJECT_MEM" in section
    assert "GLOBAL_MEM" in section
    assert "/home/user/.agent_memory/global_memory.md" in section
    assert "/home/user/.agent_memory/project_memory.md" in section


def test_memory_section_empty_when_no_memories():
    """两部分都为空 → 记忆段为空串。"""
    assert _build_memory_section("", "") == ""
    assert _build_memory_section("   ", "") == ""


def test_prompt_message_equals_base_plus_repo_and_memory():
    """_build_prompt_message == 纯指令 + 预 clone 上下文段 + 记忆段(发送完整,落库纯净)。"""
    task = _mk_task()
    base = _build_base_prompt(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
    )
    repo_section = _build_repo_context_section("[repo context]")
    section = _build_memory_section("PROJECT_MEM", "GLOBAL_MEM")
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM", global_memory="GLOBAL_MEM",
    )
    assert msg == base + repo_section + section


# ============================================================
# _build_cli_interrupt_message:CLI 软中断追问(匿名化 + 展示完整)
# ============================================================

def test_cli_interrupt_send_text_anonymized_query_only():
    """发送文本:匿名化(不含 agent2),只含 query 不含 reason。"""
    built = _build_cli_interrupt_message([
        {"query": "转向检查认证授权", "reason": "agent2 认为方向跑偏", "iteration": 4},
    ])
    assert built is not None
    send, display = built
    assert "转向检查认证授权" in send
    assert "agent2" not in send
    assert "方向跑偏" not in send
    assert "[方向调整]" in send


def test_cli_interrupt_display_full_and_prefixed():
    """展示文本:带 [检查点中断] 前缀,含完整理由与追问指令(不截断)。"""
    long_reason = "理由很" + "长" * 300
    built = _build_cli_interrupt_message([
        {"query": "转向检查认证授权", "reason": long_reason, "iteration": 4},
    ])
    assert built is not None
    _, display = built
    assert display.startswith("[检查点中断] ")
    assert long_reason in display  # 不再截断到 200 字符
    assert "追问指令:转向检查认证授权" in display


def test_cli_interrupt_multiple_numbered():
    """多条中断(防御性路径):发送文本编号合并,展示文本分节。"""
    built = _build_cli_interrupt_message([
        {"query": "指令一", "reason": "r1", "iteration": 4},
        {"query": "指令二", "reason": "r2", "iteration": 6},
    ])
    assert built is not None
    send, display = built
    assert "[1] 指令一" in send
    assert "[2] 指令二" in send
    assert "理由:r1" in display
    assert "理由:r2" in display


def test_cli_interrupt_empty_returns_none():
    """无可注入内容(空列表或 query 全空)返回 None。"""
    assert _build_cli_interrupt_message([]) is None
    assert _build_cli_interrupt_message([{"query": "  ", "reason": "r", "iteration": 1}]) is None
