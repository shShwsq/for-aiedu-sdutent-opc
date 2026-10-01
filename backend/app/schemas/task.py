"""任务相关的 Pydantic 模型(请求与响应)

通用化设计:
- TaskCreateRequest: scenario + user_input + params(可选)
- 兼容旧 API:提供 repo_url 时自动转成 user_input + params
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, HttpUrl

from app.agents.registry import get_registered_types

# 合法执行器:builtin + registry 中已注册的 agent_type
# 新增 agent 类型只需在 registry 注册,此处自动生效
_VALID_EXECUTORS = ("builtin", *get_registered_types())
_EXECUTOR_PATTERN = "^(" + "|".join(_VALID_EXECUTORS) + ")$"


class VerifierAuthToken(BaseModel):
    """登录凭证(verifier_agent 的 http_request 按身份注入请求头)

    label 为身份标识(如"管理员"/"普通用户"),LLM 调 http_request 时通过
    auth_profile=label 选择身份,工具自动把 header_name: header_value 加到请求头。
    """

    label: str = Field(..., min_length=1, max_length=64, description="身份标识(LLM 据此选择)")
    header_name: str = Field(
        ..., min_length=1, max_length=128, description="请求头名(如 Authorization / Cookie)"
    )
    header_value: str = Field(
        ..., min_length=1, max_length=4096, description="请求头值(如 Bearer xxx / session=yyy)"
    )


class TaskCreateRequest(BaseModel):
    """提交任务的请求

    方式 1(通用):传 scenario + user_input + params
    方式 2(兼容):只传 repo_url,自动生成通用 user_input(场景无关)
    """

    # 场景标识,对应已注册的场景(见 app/scenarios/);默认 "general"(通用,未选特定模板)
    scenario: str = "general"
    # 任务标题:可选,用户自定义便于识别;为空时前端用 user_input 截断展示
    title: str | None = Field(default=None, max_length=255)
    # 用户意图文本(必填,若提供 repo_url 则自动生成)
    user_input: str | None = None
    # 可选参数(如 repo_url、branch 等),场景专用
    params: dict[str, Any] | None = None

    # 用户选择的 LLM 配置 id(对应 user_llm_configs.llm_configs[].id)
    # 语义:agent2 评估模型;为空表示用 env 默认配置或匿名任务
    llm_config_id: str | None = None

    # 内置 react_agent 使用的 LLM 配置 id(仅 executor=builtin 时生效)。
    # 为空时回退到 llm_config_id(react_agent 与 agent2 共用同一模型)。
    # 外部 CLI 执行器忽略此字段。
    react_llm_config_id: str | None = None

    # 执行器选择:"builtin"(默认,内置 react_agent)或 registry 中已注册的 agent_type(如 "qoder_cli")
    # 外部 CLI 模式下,react 角色模型由该 CLI 账号配额管理;
    # llm_config_id 仍用于 agent2 评估(为空时回退 env 默认)
    executor: str = Field(default="builtin", pattern=_EXECUTOR_PATTERN)

    # 兼容字段:旧 API 直接传 repo_url
    repo_url: HttpUrl | None = None
    branch: str | None = None
    scope: str | None = None

    # 交付物来源二选一:上传文件 id(对应 POST /uploads 的返回)。
    # 与 repo_url 互斥(同时提供报 422);只提供上传时 user_input 仍必填
    # (描述要处理什么任务),文件作为附件进入沙箱工作区
    upload_id: str | None = Field(default=None, max_length=64, pattern="^[A-Za-z0-9-]+$")

    # 验证器配置(可选):agent2 可自主调用 verifier_agent 在已部署的测试环境验证
    # react_agent 的发现。对用户透明(前端不出现 verifier_agent 字样,只显示"正在验证")。
    test_env_url: str | None = Field(default=None, max_length=2048)
    verifier_enabled: bool = False
    # "direct":验证动作直接执行不弹窗;"per_action":每个 HTTP 请求/PoC 运行前弹窗授权
    verifier_auth_mode: str = Field(default="per_action", pattern="^(direct|per_action)$")
    # 登录凭证列表(可选):LLM 调 http_request 时按 auth_profile=label 注入对应请求头
    verifier_auth_tokens: list[VerifierAuthToken] = Field(default_factory=list)


class TaskTitleUpdateRequest(BaseModel):
    """修改任务标题的请求

    title 为空字符串等价于清除自定义标题(回退到用 user_input 展示)。
    """

    title: str = Field(max_length=255)


class ResultResponse(BaseModel):
    """任务结果项响应(通用)"""

    id: uuid.UUID
    round_idx: int
    title: str
    content: str
    metadata_: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class ConversationResponse(BaseModel):
    """对话记录响应"""

    id: uuid.UUID
    round_idx: int
    role: str
    type: str
    content: str
    # 思考链(仅 type=thinking 有,模型 reasoning_content 输出)
    reasoning: str | None = None
    # 仅 type=tool_result 有:对应 tool_call 会话记录的 id,前端据此配对展示
    tool_call_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskResponse(BaseModel):
    """任务详情响应"""

    id: uuid.UUID
    scenario: str
    title: str | None = None
    user_input: str
    params: dict[str, Any] | None = None
    llm_config_id: str | None = None
    react_llm_config_id: str | None = None
    executor: str = "builtin"
    # 验证器配置(从 task.params._verifier 读取,见 Task 模型 property)
    test_env_url: str | None = None
    verifier_enabled: bool = False
    verifier_auth_mode: str = "per_action"
    verifier_auth_tokens: list[VerifierAuthToken] = []
    status: str
    current_stage: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    results: list[ResultResponse] = []
    conversations: list[ConversationResponse] = []

    model_config = {"from_attributes": True}


class TaskListItem(BaseModel):
    """任务列表项(精简版,不含对话/结果,用于侧栏列表)"""

    id: uuid.UUID
    scenario: str
    title: str | None = None
    user_input: str
    status: str
    current_stage: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class TaskCreateResponse(BaseModel):
    """提交任务后的响应"""

    id: uuid.UUID
    status: str

    model_config = {"from_attributes": True}


class ScenarioInfo(BaseModel):
    """场景模板信息(给前端展示用)

    场景降级后:仅提供预设提示词 + 推荐 skill,不再驱动表单/分组/覆盖度。
    前端用 preset_prompt 预填输入框,用 recommended_skills 默认勾选 skill。
    """

    id: str
    name: str
    description: str = ""
    preset_prompt: str = ""
    recommended_skills: list[str] = []


# ============================================================
# 阶段 8:用户澄清(agent2 向用户提问)
# ============================================================


class ClarificationQuestionOption(BaseModel):
    """选择题选项"""

    value: str
    label: str


class ClarificationQuestion(BaseModel):
    """agent2 向用户提出的问题

    两种类型:
    - choice: 选择题(用户从 options 中选,可单选或多选)
    - text: 填空题(用户自由文本回答)
    """

    id: str
    type: str  # "choice" | "text"
    question: str
    placeholder: str | None = None
    required: bool = False
    options: list[ClarificationQuestionOption] | None = None
    multi: bool = False


class PendingQuestion(BaseModel):
    """任务当前待回答的问题(GET /tasks/{id}/pending_question 返回)

    前端在收到 question 事件后弹出 QuestionDialog;刷新页面后通过
    GET /tasks/{id}/pending_question 恢复弹窗。无待回答问题时返回 None。
    """

    ask_round: int
    questions: list[ClarificationQuestion]
    reasoning: str = ""
    conversation_id: str | None = None


class AnswerItem(BaseModel):
    """单个问题的答案"""

    question_id: str
    value: str | list[str]


class AnswerRequest(BaseModel):
    """用户提交答案(POST /tasks/{id}/answer)"""

    answers: list[AnswerItem]


class AnswerResponse(BaseModel):
    """提交答案的响应"""

    accepted: bool
    message: str = ""


# ============================================================
# 覆盖度清单动态生成 + 用户编辑(场景降级后)
# ============================================================


class ChecklistDimension(BaseModel):
    """覆盖度清单的一个维度(由 agent2 动态生成,用户可编辑)"""

    id: str
    name: str
    description: str = ""
    checklist: list[str] = []


class ChecklistReviewRequest(BaseModel):
    """用户编辑覆盖度清单后提交(POST /tasks/{id}/checklist)

    checklist 为 None 表示"直接采用 LLM 生成结果"(不编辑)。
    """

    checklist: list[ChecklistDimension] | None = None


# ============================================================
# 用户补充消息(对话界面下方输入框)
# ============================================================


class SendMessageRequest(BaseModel):
    """用户在对话界面下方输入框发送的补充消息(POST /tasks/{id}/messages)

    用途:用户在任务运行中/暂停中/完成后追加指令或补充要求。
    后端按 task.status 分发:
    - running/paused:消息入队,react_agent 下一迭代注入 LLM 上下文
    - completed:启动新的协作 round(resume_audit_with_message)
    """

    content: str = Field(min_length=1, max_length=8000)


class SendMessageResponse(BaseModel):
    """发送用户补充消息的响应"""

    accepted: bool
    message: str = ""


# ============================================================
# 验证器动作授权(verifier_agent per_action 模式)
# ============================================================


class VerifyActionRequest(BaseModel):
    """用户对验证动作的授权决议(POST /tasks/{id}/verify_action)"""

    action_id: str
    approved: bool


class VerifyActionResponse(BaseModel):
    """提交授权决议的响应"""

    accepted: bool
    message: str = ""


class CommandConfirmRequest(BaseModel):
    """用户对危险命令的确认决议(POST /tasks/{id}/command_confirm)"""

    command_id: str
    approved: bool


class CommandConfirmResponse(BaseModel):
    """提交命令确认决议的响应"""

    accepted: bool
    message: str = ""


class VerifyConfigUpdateRequest(BaseModel):
    """更新验证器配置请求(PATCH /tasks/{id}/verifier_config)

    运行时允许调整验证授权模式与开关(任务运行界面也可修改)。
    所有字段可选,只更新传入的字段。
    """

    verifier_enabled: bool | None = None
    verifier_auth_mode: str | None = Field(default=None, pattern="^(direct|per_action)$")
    test_env_url: str | None = Field(default=None, max_length=2048)
    # 登录凭证列表(可选):传入则整体覆盖;空列表清空;None/省略=不修改
    verifier_auth_tokens: list[VerifierAuthToken] | None = None


class RuntimePolicyUpdate(BaseModel):
    """运行时可调整的协作策略字段(任务级覆盖,合并写入 task.params._agent_policy)

    仅暴露高频字段;None=不修改该字段。
    """

    max_rounds: int | None = Field(default=None, ge=1)


class RuntimeConfigUpdateRequest(BaseModel):
    """更新任务运行时配置请求(PATCH /tasks/{id}/runtime_config)

    任务进行中修改 react_agent / agent2 模型与协作策略。
    生效时机:running/paused 的当前执行线程仍用启动时加载的配置,
    修改在下一轮执行(completed 后追加消息 / failed 重试)时生效。

    所有字段可选,只更新传入的字段;模型字段传空字符串表示清除
    (llm_config_id 清除后回退 env 默认,react_llm_config_id 清除后回退 llm_config_id)。
    """

    llm_config_id: str | None = Field(default=None, max_length=128)
    react_llm_config_id: str | None = Field(default=None, max_length=128)
    agent_policy: RuntimePolicyUpdate | None = None
