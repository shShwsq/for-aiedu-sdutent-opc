"""长期记忆管理相关的 Pydantic schema

对应 /memory 系列 API:
- UserPreferenceOut/SaveUserPreferenceRequest:User Profile (1:1)
- UserMemoryOut/SaveUserMemoryRequest:全局长期记忆(1:1)
- ProjectOut/SaveProjectRequest/ProjectListResponse:分项目记忆(1:N)

大小校验在 schema 层做第一道防线(写入时):
- user_profile ≤ 2000
- content(全局记忆) ≤ 20000
- memory_content(项目记忆) ≤ 20000

后续还有合并时截断(项目 8000 / 全局 10000)与注入时截断(2000)两道防线。
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.practice import (
    LEARNING_TOPIC_ARCHITECTURE,
    LEARNING_TOPIC_CODING,
    LEARNING_TOPIC_SECURITY,
    THINKING_MODE_FOLLOW,
    THINKING_MODE_OFF,
    THINKING_MODE_ON,
)


class UserPreferenceOut(BaseModel):
    """User Profile 响应(GET /memory/preferences)"""

    # 自由文本 Markdown(用户在记忆管理页编辑,注入 agent2)
    user_profile: str = ""
    # agent 策略配置(检查点评估频率、打断权限、验证权限)
    # None 表示未配置(用系统默认),dict 表示用户自定义的覆盖值
    agent_policy: dict[str, Any] | None = None
    # 任务完成后是否自动生成练习题 draft(默认开)
    auto_generate_practice: bool = True
    # 当前学习主题(出题提示词按此切换,默认 security)
    learning_topic: str = "security"
    # 出题前沙箱已清理时是否重新 clone 恢复工作区(默认关)
    restore_workspace_for_practice: bool = False
    # 用户级默认出题模型(UserLLMConfig 配置 id;None=未设置,回退任务级/env 默认)
    default_llm_config_id: str | None = None
    # 始终用默认出题模型(忽略任务自带模型配置;默认关)
    force_default_llm: bool = False
    # 出题思考模式覆盖(follow=跟随模型配置/on=强制开/off=强制关,默认 follow)
    thinking_mode_for_practice: str = THINKING_MODE_FOLLOW
    # 最后更新时间(可空 — 未配置时为 None;FastAPI 序列化为 ISO 字符串)
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SaveUserPreferenceRequest(BaseModel):
    """保存 User Profile 请求(PUT /memory/preferences)"""

    user_profile: str = Field(default="", max_length=2000)


class SavePracticeSettingsRequest(BaseModel):
    """保存练习设置请求(PUT /memory/preferences/practice)

    - auto_generate_practice:任务完成后是否自动生成练习题 draft
      (产出仍需用户在预览对话框确认才转 active)
    - learning_topic:当前学习主题(security/architecture/coding),
      出题提示词按此切换;None 表示本次不修改
    - restore_workspace_for_practice:出题前沙箱已清理时是否重新 clone
      恢复工作区;None 表示本次不修改
    - default_llm_config_id:用户级默认出题模型(UserLLMConfig 配置 id);
      None 表示本次不修改,空串表示清空(回退任务级/env 默认)
    - force_default_llm:始终用默认出题模型(忽略任务自带模型配置);
      None 表示本次不修改
    - thinking_mode_for_practice:出题思考模式覆盖(follow/on/off);
      None 表示本次不修改
    """

    auto_generate_practice: bool = True
    learning_topic: Literal[
        LEARNING_TOPIC_SECURITY,
        LEARNING_TOPIC_ARCHITECTURE,
        LEARNING_TOPIC_CODING,
    ] | None = None
    restore_workspace_for_practice: bool | None = None
    default_llm_config_id: str | None = Field(default=None, max_length=36)
    force_default_llm: bool | None = None
    thinking_mode_for_practice: Literal[
        THINKING_MODE_FOLLOW,
        THINKING_MODE_ON,
        THINKING_MODE_OFF,
    ] | None = None


class SaveAgentPolicyRequest(BaseModel):
    """保存 agent 策略配置请求(PUT /memory/preferences/agent_policy)

    结构与 agent_checkpoint.DEFAULT_AGENT_POLICY 对齐:
    - agent2_enabled: 是否启用 agent2(关闭=单 agent 模式)
    - max_rounds: agent2 协作总轮次(上限由 MAX_MAX_ROUNDS 控制)
    - checkpoint_interval: 统一 K 值(每 K 个迭代评估一次)
    - checkpoint_interval_builtin: 内置 react_agent 专用 K 值(null=用统一值)
    - checkpoint_interval_cli: CLI agent 专用 K 值(null=用统一值)
    - allow_interrupt: agent2 是否能打断 react_agent
    - max_interrupts_per_round: 每轮最多打断次数
    - allow_verify: agent2 是否能调用 verifier_agent 验证(需任务配了 test_env_url)
    - verifier_auth_mode_default: 验证授权默认模式("direct"直接执行 / "per_action"逐动作授权)
    - executor_command_confirm_default: 执行智能体命令确认默认模式
        "always_approve" 自动批准所有命令 / "per_command" 每个危险命令弹窗确认
    """

    agent2_enabled: bool = True
    # 上界在路由层用 MAX_MAX_ROUNDS 动态校验(schema 层只校验下界)
    max_rounds: int = Field(default=4, ge=1)
    checkpoint_interval: int = Field(default=10, ge=1, le=20)
    checkpoint_interval_builtin: int | None = Field(default=None, ge=1, le=20)
    checkpoint_interval_cli: int | None = Field(default=None, ge=1, le=20)
    allow_interrupt: bool = True
    max_interrupts_per_round: int = Field(default=2, ge=0, le=10)
    allow_verify: bool = False
    verifier_auth_mode_default: str = Field(default="per_action", pattern="^(direct|per_action)$")
    executor_command_confirm_default: str = Field(
        default="always_approve", pattern="^(always_approve|per_command)$"
    )


class PolicyLimitsOut(BaseModel):
    """系统级策略限制(GET /memory/policy-limits)

    前端据此动态渲染输入上限,不硬编码。后端 MAX_MAX_ROUNDS 可通过
    环境变量 SECONDLOOK_MAX_ROUNDS_LIMIT 调整。
    """

    max_rounds: int


class UserMemoryOut(BaseModel):
    """全局长期记忆响应(GET /memory/global)"""

    content: str = ""
    # 最后更新时间(可空 — 未配置时为 None;FastAPI 序列化为 ISO 字符串)
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SaveUserMemoryRequest(BaseModel):
    """保存全局长期记忆请求(PUT /memory/global)"""

    content: str = Field(default="", max_length=20000)


class ProjectOut(BaseModel):
    """分项目记忆响应(GET /memory/projects、GET/PUT /memory/projects/{id})"""

    id: str
    repo_url_normalized: str
    repo_url_raw: str
    alias: str | None = None
    note: str | None = None
    memory_content: str = ""
    # 精简版记忆(系统生成,注入 system prompt 用;前端可查看)
    memory_summary: str = ""
    # 上次自动归纳时间(ISO 字符串,可空)
    last_summary_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}


class SaveProjectRequest(BaseModel):
    """保存分项目记忆请求(PUT /memory/projects/{id})

    仅允许编辑 alias/note/memory_content(不修改 repo_url 与 last_summary_at)。
    """

    alias: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None)
    memory_content: str = Field(default="", max_length=20000)


class ProjectListResponse(BaseModel):
    """分项目记忆列表响应(GET /memory/projects、DELETE 后响应)"""

    projects: list[ProjectOut] = Field(default_factory=list)
