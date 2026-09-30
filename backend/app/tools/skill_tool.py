"""SKILL 工具入口(阶段 5)

暴露给 react_agent 的两个工具:
- list_skills:列出可用 skill(name + description)
- skill:获取某个 skill 的 SKILL.md body 内容,让 LLM 按指令自行执行后续工具

设计要点:
- skill 工具不直接执行任何审计操作,只返回 SKILL.md 文本
- LLM 看完 SKILL.md 后,自己决定调用 search_code / read_file / run_semgrep 等底层工具
- 这符合 Trae / Claude Code 的 skill 规范:Markdown 指令驱动,而非程序化 steps

场景降级后的变更:
- 不再按 scenario 过滤 skill。改为按 task.allowed_skills 过滤(用户创建任务时选择)。
- allowed_skills 为 None/空 表示全部 skill 可用(默认)。
- skill 目录改为全局可见(skills/ 下所有子目录),不再按场景前缀组织。

用户隔离:按当前任务所属用户过滤可见 skill。
- 内置 skill(场景目录非 user_ 前缀):所有用户可见
- 用户上传的 skill(user_<uuid> 前缀):仅 owner 可见

并发安全:用 contextvars 替代全局变量,每个后台线程有独立上下文。
"""
import contextvars
import logging
import uuid

from app.skills import loader as skill_loader

logger = logging.getLogger(__name__)


# 当前任务允许调用的 skill 名称列表(由 react_agent 在每轮开始时注入,每个线程独立)
# None/空 表示全部 skill 可用(默认)
_CURRENT_ALLOWED_SKILLS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "current_allowed_skills", default=None
)

# 当前任务所属用户 id(由 react_agent 每轮开始时注入,用于过滤用户上传的私有 skill)
_CURRENT_USER_ID: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "current_user_id", default=None
)


def set_current_allowed_skills(allowed_skills: list[str] | None) -> None:
    """react_agent 调用工具前,注入当前任务允许的 skill 列表

    allowed_skills: skill 名称列表。None 或空列表表示全部可用。
    使用 ContextVar,每个后台线程的 set 只影响该线程自身。
    """
    _CURRENT_ALLOWED_SKILLS.set(allowed_skills if allowed_skills else None)


def set_current_user_id(user_id: uuid.UUID | None) -> None:
    """react_agent 调用工具前,注入当前任务所属用户 id

    用于过滤用户上传的私有 skill:仅 owner 可见。None 表示匿名任务,
    此时只看内置 skill。使用 ContextVar,每个后台线程独立。
    """
    _CURRENT_USER_ID.set(user_id)


def _get_all_skills():
    """获取当前用户可见的 skill(内置全局共享 + 自己上传的)

    同名 skill 跨场景去重(保留首个)。
    """
    all_skills = skill_loader.list_visible_skills(_CURRENT_USER_ID.get())
    seen_names: set[str] = set()
    result = []
    for skill in all_skills:
        if skill.name not in seen_names:
            result.append(skill)
            seen_names.add(skill.name)
    return result


# ============================================================
# 工具 1:list_skills
# ============================================================


def list_available_skills(task_id: str = "") -> dict:
    """列出当前任务可用的 skill

    返回:{
        "skills": [
            {"name": "check_sql_injection", "description": "..."},
            ...
        ],
        "total": int,
        "filtered": bool  # 是否按 allowed_skills 过滤
    }
    """
    allowed = _CURRENT_ALLOWED_SKILLS.get()
    all_skills = _get_all_skills()

    if allowed:
        # 按 allowed_skills 过滤(用户创建任务时选择的)
        allowed_set = set(allowed)
        skills = [s for s in all_skills if s.name in allowed_set]
        filtered = True
    else:
        # 全部可用(默认)
        skills = all_skills
        filtered = False

    return {
        "skills": [
            {"name": s.name, "description": s.description}
            for s in skills
        ],
        "total": len(skills),
        "filtered": filtered,
    }


# ============================================================
# 工具 2:skill(获取 SKILL.md 内容)
# ============================================================


def run_skill(skill_name: str, task_id: str = "") -> dict:
    """获取指定 skill 的 SKILL.md 指令内容

    参数:
        skill_name: skill 名称(先调 list_skills 查可用名称)

    返回:{
        "skill_name": "check_sql_injection",
        "description": "...",
        "instructions": "<SKILL.md body 全文>"
    }

    LLM 拿到 instructions 后,按其指引自行调用底层工具执行
    """
    allowed = _CURRENT_ALLOWED_SKILLS.get()
    all_skills = _get_all_skills()

    # 查找目标 skill
    target = None
    for s in all_skills:
        if s.name == skill_name:
            target = s
            break

    if target is None:
        available = [s.name for s in all_skills]
        return {
            "error": f"未知 skill: {skill_name}",
            "available_skills": available,
            "hint": "先调用 list_skills 查可用 skill 名称",
        }

    # 若设置了 allowed_skills 过滤,检查是否在允许列表内
    if allowed and skill_name not in set(allowed):
        return {
            "error": f"skill {skill_name} 不在当前任务的允许列表内",
            "allowed_skills": allowed,
            "hint": "此 skill 未被用户授权调用,请用 allowed 内的 skill",
        }

    return {
        "skill_name": target.name,
        "description": target.description,
        "instructions": target.body,
    }
