"""长期记忆管理路由

用户可编辑三类记忆:
- User Profile (1:1,自由文本):影响 agent2 评判标准与 checklist 生成
- 全局长期记忆(1:1,自由文本):跨项目通用经验,注入 agent2
- 分项目记忆(1:N,按 repo_url 聚合):注入 react_agent,影响审计方向

端点:
- GET/PUT /memory/preferences       User Profile
- GET/PUT /memory/global            全局长期记忆
- GET    /memory/projects           项目列表
- GET/PUT/DELETE /memory/projects/{project_id}  单个分项目记忆

鉴权:全部 Depends(get_current_user)。匿名用户无法访问(匿名任务不持久化记忆)。
跨用户隔离:所有查询/更新都带 user_id 过滤,确保用户 A 看不到/改不了用户 B 的数据。

注意:项目(repo_url)由 orchestrator 在任务完成时自动归纳创建(_get_or_create_project),
本路由不提供"新建项目"端点——用户只能编辑/删除已由 agent 自动归纳产生的项目记录。
若用户想手动为某仓库预置记忆,可在任务跑一次后编辑,或后续扩展 POST 端点。
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.agent_policy import AgentPolicy
from app.models.practice import (
    DEFAULT_LEARNING_TOPIC,
    DEFAULT_THINKING_MODE,
    PracticeSettings,
)
from app.models.project import Project
from app.models.user import User
from app.models.user_llm_config import UserLLMConfig
from app.models.user_memory import UserMemory
from app.models.user_preference import UserPreference
from app.schemas.memory import (
    PolicyLimitsOut,
    ProjectListResponse,
    ProjectOut,
    SaveAgentPolicyRequest,
    SavePracticeSettingsRequest,
    SaveProjectRequest,
    SaveUserMemoryRequest,
    SaveUserPreferenceRequest,
    UserMemoryOut,
    UserPreferenceOut,
)
from app.agent_checkpoint import MAX_MAX_ROUNDS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


# ============================================================
# User Profile (1:1)
# ============================================================


@router.get("/preferences", response_model=UserPreferenceOut)
def get_preferences(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """获取当前用户的偏好(未配置则返回空默认值)"""
    return _build_preference_out(db, current_user.id)


@router.put("/preferences", response_model=UserPreferenceOut)
def save_preferences(
    req: SaveUserPreferenceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """保存/更新 User Profile (get_or_create)"""
    row = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = UserPreference(
            user_id=current_user.id,
            user_profile=req.user_profile,
        )
        db.add(row)
    else:
        row.user_profile = req.user_profile
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 更新了偏好", current_user.id)
    return _build_preference_out(db, current_user.id, pref_row=row)


@router.put("/preferences/practice", response_model=UserPreferenceOut)
def save_practice_settings(
    req: SavePracticeSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """保存/更新练习设置(自动生成开关 / 学习主题 / 出题前恢复工作区 / 默认出题模型 / 思考模式)

    存于 practice_settings 独立表(1:1),get_or_create:无行时自动创建。
    learning_topic / restore_workspace_for_practice / default_llm_config_id /
    force_default_llm / thinking_mode_for_practice 可选:传 None 表示不修改;
    default_llm_config_id 传空串表示清空。
    """
    # 默认出题模型归属校验:必须是当前用户已保存的 LLM 配置
    if req.default_llm_config_id:
        cfg_row = (
            db.query(UserLLMConfig)
            .filter(UserLLMConfig.user_id == current_user.id)
            .first()
        )
        ids = {c.get("id") for c in (cfg_row.llm_configs or [])} if cfg_row else set()
        if req.default_llm_config_id not in ids:
            raise HTTPException(
                status_code=400,
                detail="出题模型配置不存在或不属于当前用户",
            )
    row = (
        db.query(PracticeSettings)
        .filter(PracticeSettings.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = PracticeSettings(
            user_id=current_user.id,
            auto_generate_practice=req.auto_generate_practice,
        )
        db.add(row)
    else:
        row.auto_generate_practice = req.auto_generate_practice
    if req.learning_topic is not None:
        row.learning_topic = req.learning_topic
    if req.restore_workspace_for_practice is not None:
        row.restore_workspace_for_practice = req.restore_workspace_for_practice
    if req.default_llm_config_id is not None:
        row.default_llm_config_id = req.default_llm_config_id or None
    if req.force_default_llm is not None:
        row.force_default_llm = req.force_default_llm
    if req.thinking_mode_for_practice is not None:
        row.thinking_mode_for_practice = req.thinking_mode_for_practice
    db.commit()
    db.refresh(row)
    logger.info(
        "用户 %s 更新练习设置: auto_generate_practice=%s learning_topic=%s "
        "restore_workspace=%s default_llm_config_id=%s force_default_llm=%s thinking_mode=%s",
        current_user.id, req.auto_generate_practice,
        req.learning_topic, req.restore_workspace_for_practice,
        row.default_llm_config_id, row.force_default_llm,
        row.thinking_mode_for_practice,
    )
    return _build_preference_out(db, current_user.id, settings_row=row)


@router.put("/preferences/agent_policy", response_model=UserPreferenceOut)
def save_agent_policy(
    req: SaveAgentPolicyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """保存/更新 agent 策略配置(检查点评估频率、打断权限等)

    作为用户级默认值(存 agent_policies 独立表),
    任务级可通过 task.params["_agent_policy"] 覆盖。
    get_or_create:若用户无策略记录,自动创建。
    """
    policy_dict = req.model_dump()
    # 钳制 max_rounds 到 [1, MAX_MAX_ROUNDS](防御前端送超界值)
    policy_dict["max_rounds"] = max(1, min(int(policy_dict.get("max_rounds", 4)), MAX_MAX_ROUNDS))
    row = (
        db.query(AgentPolicy)
        .filter(AgentPolicy.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = AgentPolicy(user_id=current_user.id, **policy_dict)
        db.add(row)
    else:
        for key, value in policy_dict.items():
            setattr(row, key, value)
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 更新了 agent_policy", current_user.id)
    return _build_preference_out(db, current_user.id)


# ============================================================
# 全局长期记忆(1:1)
# ============================================================


@router.get("/policy-limits", response_model=PolicyLimitsOut)
def get_policy_limits(
    current_user: User = Depends(get_current_user),
) -> PolicyLimitsOut:
    """系统级策略限制(前端据此动态渲染输入上限,不硬编码)

    返回当前后端 MAX_MAX_ROUNDS(可通过环境变量 SECONDLOOK_MAX_ROUNDS_LIMIT 调整)。
    """
    return PolicyLimitsOut(max_rounds=MAX_MAX_ROUNDS)


@router.get("/global", response_model=UserMemoryOut)
def get_global_memory(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserMemoryOut:
    """获取当前用户的全局长期记忆(未配置则返回空)"""
    row = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == current_user.id)
        .first()
    )
    if row is None:
        return UserMemoryOut()
    return UserMemoryOut.model_validate(row)


@router.put("/global", response_model=UserMemoryOut)
def save_global_memory(
    req: SaveUserMemoryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserMemoryOut:
    """保存/更新全局长期记忆(get_or_create)"""
    row = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = UserMemory(user_id=current_user.id, content=req.content)
        db.add(row)
    else:
        row.content = req.content
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 更新了全局长期记忆", current_user.id)
    return UserMemoryOut.model_validate(row)


# ============================================================
# 分项目记忆(1:N)
# ============================================================


@router.get("/projects", response_model=ProjectListResponse)
def list_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectListResponse:
    """获取当前用户的所有项目记忆列表(按更新时间倒序)"""
    rows = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.updated_at.desc())
        .all()
    )
    return ProjectListResponse(projects=[_project_to_out(r) for r in rows])


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """获取单个项目记忆详情"""
    row = _find_project(db, current_user.id, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到项目: {project_id}")
    return _project_to_out(row)


@router.put("/projects/{project_id}", response_model=ProjectOut)
def save_project(
    project_id: str,
    req: SaveProjectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """更新项目记忆的 alias/note/memory_content(用户手动编辑)

    不更新 repo_url(归一化值是项目身份,不可改);
    不更新 last_summary_at(那是自动归纳的时间戳,手动编辑不改它)。

    memory_content 改动后同步重新生成 memory_summary(精简版,注入 system prompt 用):
    ≤2000 即时无 LLM;>2000 走 LLM(env 默认配置),失败兜底硬截断,不阻塞请求。
    """
    row = _find_project(db, current_user.id, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到项目: {project_id}")
    row.alias = req.alias
    row.note = req.note
    row.memory_content = req.memory_content
    # 重新生成精简版(用户手改 memory_content 后,旧 summary 可能失效)
    row.memory_summary = _regen_memory_summary(req.memory_content)
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 手动更新了项目记忆 %s", current_user.id, project_id)
    return _project_to_out(row)


@router.delete("/projects/{project_id}", response_model=ProjectListResponse)
def delete_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectListResponse:
    """删除项目记忆(整行删除,含 alias/note/memory_content)"""
    row = _find_project(db, current_user.id, project_id)
    if row is not None:
        db.delete(row)
        db.commit()
        logger.info("用户 %s 删除了项目记忆 %s", current_user.id, project_id)
    # 返回剩余列表
    rows = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.updated_at.desc())
        .all()
    )
    return ProjectListResponse(projects=[_project_to_out(r) for r in rows])


# ============================================================
# 辅助函数
# ============================================================


def _build_preference_out(
    db: Session, user_id,
    pref_row: UserPreference | None = None,
    settings_row: PracticeSettings | None = None,
) -> UserPreferenceOut:
    """组装 UserPreferenceOut(数据跨三表:user_preferences + agent_policies + practice_settings)

    - user_profile 来自 user_preferences(可能无行)
    - agent_policy 来自 agent_policies 独立表(可能无行 → None,前端用系统默认)
    - auto_generate_practice / learning_topic / restore_workspace_for_practice /
      default_llm_config_id / force_default_llm / thinking_mode_for_practice
      来自 practice_settings 独立表(可能无行 → 用默认值)
    - updated_at 取各行中较新的(哪边最后保存,就算最后更新)
    """
    if pref_row is None:
        pref_row = (
            db.query(UserPreference)
            .filter(UserPreference.user_id == user_id)
            .first()
        )
    policy_row = (
        db.query(AgentPolicy)
        .filter(AgentPolicy.user_id == user_id)
        .first()
    )
    if settings_row is None:
        settings_row = (
            db.query(PracticeSettings)
            .filter(PracticeSettings.user_id == user_id)
            .first()
        )
    if pref_row is None and policy_row is None and settings_row is None:
        return UserPreferenceOut()
    updated_at = None
    for r in (pref_row, policy_row, settings_row):
        if r is not None and r.updated_at is not None:
            if updated_at is None or r.updated_at > updated_at:
                updated_at = r.updated_at
    return UserPreferenceOut(
        user_profile=pref_row.user_profile if pref_row else "",
        agent_policy=policy_row.to_dict() if policy_row else None,
        auto_generate_practice=settings_row.auto_generate_practice if settings_row else True,
        learning_topic=settings_row.learning_topic if settings_row else DEFAULT_LEARNING_TOPIC,
        restore_workspace_for_practice=(
            settings_row.restore_workspace_for_practice if settings_row else False
        ),
        default_llm_config_id=(
            settings_row.default_llm_config_id if settings_row else None
        ),
        force_default_llm=(
            settings_row.force_default_llm if settings_row else False
        ),
        thinking_mode_for_practice=(
            settings_row.thinking_mode_for_practice
            if settings_row else DEFAULT_THINKING_MODE
        ),
        updated_at=updated_at,
    )


def _find_project(db: Session, user_id, project_id: str) -> Project | None:
    """按 project_id + user_id 查(确保跨用户隔离)

    project_id 是 UUID 字符串,解析失败返回 None(404)。
    """
    try:
        pid = UUID(project_id)
    except (ValueError, TypeError):
        return None
    return (
        db.query(Project)
        .filter(
            Project.id == pid,
            Project.user_id == user_id,
        )
        .first()
    )


def _project_to_out(row: Project) -> ProjectOut:
    """Project ORM → ProjectOut(把 uuid/datetime 序列化为字符串)"""
    return ProjectOut(
        id=str(row.id),
        repo_url_normalized=row.repo_url_normalized,
        repo_url_raw=row.repo_url_raw,
        alias=row.alias,
        note=row.note,
        memory_content=row.memory_content,
        memory_summary=row.memory_summary,
        last_summary_at=row.last_summary_at.isoformat() if row.last_summary_at else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _regen_memory_summary(memory_content: str) -> str:
    """重新生成精简版项目记忆(PUT 编辑后调用)。

    尝试用 env 默认 LLM 生成(>2000 时);LLM 不可用或失败 → generate_memory_summary
    内部兜底硬截断。任何异常都不影响请求,最差返回硬截断串。
    """
    try:
        from app.llm.client import LLMClient
        from app.services.memory_summarize import generate_memory_summary

        try:
            llm = LLMClient()  # env 默认配置
        except Exception:
            llm = None  # 未配置 env LLM → 走硬截断兜底
        return generate_memory_summary(memory_content, llm)
    except Exception as e:
        logger.warning("重新生成精简记忆失败,回退硬截断: %s", e)
        # 兜底:直接硬截断
        content = (memory_content or "").strip()
        return content[:2000] if len(content) > 2000 else content
