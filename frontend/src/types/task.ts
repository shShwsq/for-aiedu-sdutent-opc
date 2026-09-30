/**
 * 任务相关类型定义
 *
 * 与后端 app/schemas/task.py 一一对应
 */

/** 任务状态(后端 TaskStatus 枚举的字符串值) */
export type TaskStatus = 'pending' | 'running' | 'paused' | 'completed' | 'failed'

// ============================================================
// 阶段 8:agent2 向用户提问澄清意图
// ============================================================

/** 选择题选项 */
export interface ClarificationQuestionOption {
  value: string
  label: string
}

/**
 * agent2 向用户提出的问题
 *
 * 两种类型:
 * - choice: 选择题(用户从 options 中选,可单选或多选)
 * - text: 填空题(用户自由文本回答)
 */
export interface ClarificationQuestion {
  id: string
  type: 'choice' | 'text'
  question: string
  placeholder?: string
  required?: boolean
  options?: ClarificationQuestionOption[]
  multi?: boolean
}

/** 任务当前待回答的问题(GET /tasks/{id}/pending_question) */
export interface PendingQuestion {
  ask_round: number
  questions: ClarificationQuestion[]
  reasoning?: string
  conversation_id?: string | null
}

/** 单个问题的答案 */
export interface AnswerItem {
  question_id: string
  value: string | string[]
}

/** 提交答案请求 */
export interface AnswerRequest {
  answers: AnswerItem[]
}

/** 提交答案响应 */
export interface AnswerResponse {
  accepted: boolean
  message?: string
}

/**
 * 场景信息(后端 ScenarioInfo,精简模板)
 *
 * 场景降级为模板后,仅保留 id/名称/描述/预设 prompt/推荐 skill,
 * 不再声明 checklist/prompt/工具白名单/结果 schema。
 */
export interface Scenario {
  id: string
  name: string
  description?: string
  /** 预设 prompt:选择场景时预填到用户输入框,用户可自由编辑 */
  preset_prompt?: string
  /** 推荐使用的 skill 列表(展示给用户参考,不强制) */
  recommended_skills?: string[]
}

/**
 * 覆盖度清单维度(agent2 第 0 轮动态生成,用户可编辑确认)
 *
 * 维度对应一类检查目标,checklist 是该维度下的具体检查项。
 */
export interface ChecklistDimension {
  id: string
  name: string
  description: string
  /** 该维度下的检查项列表 */
  checklist: string[]
}

/** 任务覆盖度看板数据(GET /tasks/{id}/coverage) */
export interface TaskCoverage {
  /** 各维度覆盖状态 */
  dimensions: TaskCoverageDimension[]
  /** 已覆盖维度数 */
  covered_count: number
  /** 维度总数 */
  total_count: number
  /** 最新评估所在轮次(null 表示尚无评估) */
  last_round: number | null
}

/** 任务覆盖度维度(含运行时覆盖状态) */
export interface TaskCoverageDimension {
  /** 是否已覆盖 */
  covered: boolean
  id: string
  name: string
  description: string
}

/** 提交任务请求(后端 TaskCreateRequest) */
export interface TaskCreateRequest {
  scenario: string
  /** 任务标题:可选,用户自定义便于在历史列表识别;为空时前端用 user_input 截断展示 */
  title?: string
  user_input?: string
  params?: Record<string, unknown>
  /** agent2 评估使用的 LLM 配置 id(对应 user_llm_configs.llm_configs[].id) */
  llm_config_id?: string
  /**
   * 内置 agent1 使用的 LLM 配置 id(仅 executor=builtin 时生效)。
   * 为空时回退到 llm_config_id(agent1 与 agent2 共用同一模型)。
   * 外部 CLI 执行器忽略此字段(模型由 CLI 账号配额管理)。
   */
  react_llm_config_id?: string
  /**
   * 执行器选择:"builtin"(默认,内置 agent1)或某个 agent_type(如 "qoder_cli")
   *
   * builtin 模式下,agent1 用 react_llm_config_id(回退到 llm_config_id),
   * agent2 用 llm_config_id。
   * CLI 模式下,react 角色模型由该 CLI 自管,llm_config_id 仅用于 agent2 评估。
   * 候选 agent 列表由后端 GET /agents/configs 动态返回(is_active=true 的)。
   */
  executor?: string
  /** 兼容字段:传 repo_url 时自动生成 user_input */
  repo_url?: string
  branch?: string
  scope?: string
  /**
   * 交付物来源二选一:上传文件 id(对应 POST /uploads 返回)。
   * 与 repo_url 互斥(同时传后端报 422);上传模式下 user_input 仍必填。
   */
  upload_id?: string
  /**
   * 用户选择的 skill 列表(可选)
   *
   * 不传(undefined)= 使用全部可用 skill;传空数组 = 禁用所有 skill;
   * 传非空数组 = 仅允许使用列表中的 skill。
   */
  allowed_skills?: string[]
  /**
   * 测试环境 URL(可选):agent2 可在已部署的测试环境动态验证 agent1
   * 发现的安全问题。对用户透明(前端不出现 verifier_agent 字样,只显示"正在验证")。
   *
   * 仅当 verifier_enabled=true 且 test_env_url 非空时启用验证。
   */
  test_env_url?: string
  /** 是否启用动态验证(agent2 可自主调用验证) */
  verifier_enabled?: boolean
  /**
   * 验证授权模式:
   * - "direct":验证动作直接执行不弹窗
   * - "per_action":每个 HTTP 请求/PoC 运行前弹窗授权
   */
  verifier_auth_mode?: 'direct' | 'per_action'
  /** 登录凭证列表(可选,LLM 调 http_request 时按 auth_profile=label 注入对应请求头) */
  verifier_auth_tokens?: VerifierAuthToken[]
}

/** 提交任务响应(后端 TaskCreateResponse) */
export interface TaskCreateResponse {
  id: string
  status: TaskStatus
}

/** 任务结果项(后端 ResultResponse) */
export interface TaskResult {
  id: string
  round_idx: number
  title: string
  content: string
  /** 场景专用信息(安全场景:cwe/severity/file_path/line_range) */
  metadata_?: Record<string, unknown> | null
}

/** 对话记录(后端 ConversationResponse) */
export interface Conversation {
  id: string
  round_idx: number
  /** 角色:user / agent2 / agent1 */
  role: string
  /**
   * 消息类型:
   * - evaluation: agent2 评估
   * - question: agent2 向用户提问 / agent1 接收的 user 指令(原始意图)
   * - answer: 用户对澄清提问的回答
   * - message: 用户在对话界面下方输入框主动发送的补充消息
   * - thinking: agent1 思考过程
   * - tool_call / tool_result: 工具调用 / 结果
   * - submit: agent1 提交结果
   * - summary: agent2 最终总结
   * - error: 错误信息
   */
  type: string
  content: string
  /**
   * 思考链(仅 type=thinking 有,模型 reasoning_content 输出)
   *
   * 流式期间通过 thinking_delta 实时推送,完成后落库到此字段。
   * 刷新页面后从 GET /tasks/{id} 拿到,用于还原流式卡片(只读模式,不再实时打字)。
   */
  reasoning?: string | null
  /**
   * 仅 type=tool_result 有:对应 tool_call 会话记录的 id。
   * 并行工具调用时 result 不再紧跟 call 落库,前端靠它精确配对;
   * 历史数据为 null,回退相邻配对
   */
  tool_call_id?: string | null
  created_at: string
}

/** 任务详情(后端 TaskResponse) */
export interface TaskDetail {
  id: string
  scenario: string
  /** 任务标题:可空,前端按 null 回退到 user_input 截断展示 */
  title: string | null
  user_input: string
  params?: Record<string, unknown> | null
  llm_config_id?: string | null
  /** 内置 agent1 模型 id(空时回退到 llm_config_id) */
  react_llm_config_id?: string | null
  /** 执行器:"builtin"(内置 agent1)或某个 agent_type(如 "qoder_cli") */
  executor?: string
  status: TaskStatus
  current_stage: string | null
  error_message: string | null
  created_at: string
  completed_at: string | null
  results: TaskResult[]
  conversations: Conversation[]
  /**
   * 覆盖度清单(agent2 第 0 轮动态生成,用户确认后落库)
   *
   * null/未定义 = 未生成清单(任务尚未进入清单生成阶段,或场景不使用清单)。
   * 覆盖度看板据此判断是否展示。
   */
  checklist?: ChecklistDimension[] | null
  /** 用户选择的 skill 列表(null/未定义 = 全部可用) */
  allowed_skills?: string[] | null
  /** 测试环境 URL(启用验证时,agent2 在此环境动态验证安全发现) */
  test_env_url?: string | null
  /** 是否启用动态验证 */
  verifier_enabled?: boolean
  /** 验证授权模式:"direct" 直接执行 / "per_action" 逐动作授权 */
  verifier_auth_mode?: 'direct' | 'per_action'
  /** 登录凭证列表(从 params._verifier.auth_tokens 读取) */
  verifier_auth_tokens?: VerifierAuthToken[]
}

/** 任务列表项(后端 TaskListItem,精简版用于侧栏) */
export interface TaskListItem {
  id: string
  scenario: string
  /** 任务标题:可空,前端按 null 回退到 user_input 截断展示 */
  title: string | null
  user_input: string
  status: TaskStatus
  current_stage: string | null
  error_message: string | null
  created_at: string
  completed_at: string | null
}

// ============================================================
// SSE 事件类型(后端 event_bus.py publish 的事件格式)
// ============================================================

/** SSE 事件类型 */
export type SSEEventType =
  | 'connected'
  | 'conversation'
  | 'conversation_update'
  | 'status'
  | 'thinking_delta'
  | 'clone_progress'
  | 'plan'
  | 'question'
  | 'checklist_review'
  | 'verify_action'
  | 'command_confirm'
  | 'done'
  | 'error'
  | 'agent_checkpoint'
  | 'interrupt_cancelled'

/** SSE 事件通用结构 */
export interface SSEEvent {
  type: SSEEventType
  task_id: string
  data: Record<string, unknown>
  timestamp: string
}

/** connected 事件 data */
export interface ConnectedData {
  status: TaskStatus
  current_stage: string | null
}

/** conversation 事件 data(与 Conversation 字段一致) */
export interface ConversationEventData {
  id: string
  round_idx: number
  role: string
  type: string
  content: string
  /** 完整评估/思考链(如 agent2 evaluation 的覆盖情况+判断),可折叠回看 */
  reasoning?: string | null
  /** 仅 type=tool_result 有:对应 tool_call 会话记录的 id */
  tool_call_id?: string | null
  created_at: string | null
}

/** conversation_update 事件 data(更新已有对话项的 content,如 Kimi 增量参数补全) */
export interface ConversationUpdateEventData {
  id: string
  content: string
}

/** status 事件 data */
export interface StatusEventData {
  status: TaskStatus
  current_stage: string | null
}

/**
 * clone_progress 事件 data
 *
 * local 模式下 git clone 的进度推送。后端用 Popen 流式读 git stderr,
 * 解析 "Receiving objects: X%" 等行后推送。高频瞬时事件,前端按 percent
 * 更新进度条。克隆完成(后端推 status 切换 current_stage)或任务结束时清除。
 */
export interface CloneProgressEventData {
  /** 进度百分比 0-100 */
  percent: number
  /** 原始进度行文本(如 "Receiving objects: 45% (1234/5678)") */
  message: string
}

/**
 * thinking_delta 事件 data
 *
 * LLM 流式输出的 token 增量。前端按 conv_id 累积,实现打字机效果。
 * 一个 conv_id 对应一次 LLM 调用:
 *   1. phase='start' → 创建占位项,显示"正在生成..."
 *   2. phase='reasoning' → 累积到思考链区域
 *   3. phase='content' → 累积到正式回答区域
 *   4. phase='end' → 标记完成
 *
 * 流结束后,完整内容会通过 conversation 事件再推一次(用于落库 + 迟到订阅者补播)
 */
export interface ThinkingDeltaEventData {
  /** 该次 LLM 调用的临时 ID(前端按此 key 累积) */
  conv_id: string
  /** 协作轮次 */
  round_idx: number
  /** 角色:agent1 / agent2 */
  role: 'agent1' | 'agent2'
  /** 阶段:start / reasoning / content / end / error */
  phase: 'start' | 'reasoning' | 'content' | 'end' | 'error'
  /** 增量文本(start/end 时为空字符串) */
  delta: string
  /** 迭代序号(仅 agent1 有,标识 ReAct 循环的第几次 LLM 调用) */
  iteration?: number
  /**
   * 是否为动态验证的思考流(verifier_agent 产生,对用户透明归到 agent2 名下)。
   * true 时前端显示"正在验证"而非"正在评估"。
   */
  verify?: boolean
  /**
   * 思考流来源:'checkpoint' 表示检查点评估的思考链,
   * 前端路由到任务详情右侧栏(检查点评估聚合区),不进主对话流。
   */
  source?: 'checkpoint'
}

/** done/error 事件 data */
export interface DoneEventData {
  status: TaskStatus
  error_message?: string
}

/**
 * plan 事件 data
 *
 * agent1 在复杂任务的思考 content 里输出 <plan>...</plan> 计划清单,
 * 后端提取后推送此事件(覆盖式更新,取最新一次)。
 * 历史回放时前端从 thinking conversation.content 重新提取。
 */
export interface PlanEventData {
  /** 协作轮次 */
  round_idx: number
  /** 计划步骤列表 */
  steps: PlanStep[]
}

/** 计划步骤 */
export interface PlanStep {
  /** 步骤序号(从 1 开始) */
  id: number
  /** 步骤描述 */
  text: string
  /** 状态:pending / in_progress / done */
  status: 'pending' | 'in_progress' | 'done'
}

/**
 * question 事件 data(阶段 8:用户澄清)
 *
 * agent2 在第 0 轮初始评估时,若认为用户意图不清晰,输出 ask_user=true
 * + questions 列表。后端推送此事件,前端弹出 QuestionDialog 让用户填答。
 * 用户提交后通过 POST /tasks/{id}/answer 唤醒后台线程继续评估。
 */
export interface QuestionEventData {
  /** 提问轮次(0=首次,1=用户回答后再问) */
  ask_round: number
  /** 问题列表(最后一题固定为"是否有其他补充") */
  questions: ClarificationQuestion[]
  /** agent2 的判断依据(展示给用户参考) */
  reasoning?: string
  /** 对应的 Conversation 记录 id(落库的提问记录) */
  conversation_id?: string | null
}

/**
 * checklist_review 事件 data(覆盖度清单动态生成)
 *
 * agent2 在第 0 轮动态生成覆盖度清单后推送此事件,前端弹出 ChecklistReviewDialog
 * 让用户编辑确认。用户提交后通过 POST /tasks/{id}/checklist 唤醒后台线程继续评估。
 */
export interface ChecklistReviewEventData {
  /** agent2 生成的覆盖度维度列表 */
  checklist: ChecklistDimension[]
  /** agent2 生成清单的依据(展示给用户参考,可选) */
  reasoning?: string
}

/**
 * agent_checkpoint 事件 data(agent2 检查点评估结果)
 *
 * agent1(含内置 agent1 和外部 CLI agent)执行过程中,每 K 个迭代边界
 * agent2 做轻量评估,判断方向是否跑偏。评估结果通过此事件推送前端展示。
 * interrupt=true 时表示 agent2 决定打断并注入追问指令(软中断)。
 */
export interface AgentCheckpointEventData {
  /** 协作轮次 */
  round_idx: number
  /** 触发评估时的迭代序号 */
  iteration: number
  /** 是否打断(true=agent2 认为方向跑偏,注入追问指令) */
  interrupt: boolean
  /** 评估理由(展示给用户看) */
  reason: string
  /** 打断时的追问指令(interrupt=true 时非空,注入 agent1 作为 user 消息) */
  query: string | null
}

/**
 * interrupt_cancelled 事件 data(用户取消了待生效的检查点打断)
 *
 * CLI 执行器的打断入队后要等当前 prompt 结束才注入,期间用户可点
 * "取消打断"。取消成功后后端推此事件,前端把 pending 卡片切为已取消态。
 */
export interface InterruptCancelledEventData {
  /** 被取消打断所属的协作轮次 */
  round_idx: number
  /** 触发打断时的迭代序号 */
  iteration: number | null
}

/**
 * GET /tasks/{id}/pending_interrupt 响应(待生效的检查点打断)
 *
 * 刷新页面后恢复前端 pending 卡片用;无待生效打断时接口返回 null。
 */
export interface PendingInterruptInfo {
  round_idx: number
  iteration: number | null
  reason: string
  query: string | null
  created_at?: string
}

/**
 * verify_action 事件 data(动态验证动作授权)
 *
 * verifier_agent 在 per_action 模式下,每次执行 http_request / run_python_code 前
 * 推送此事件,前端弹窗展示动作详情让用户确认/拒绝。用户提交授权决议后通过
 * POST /tasks/{id}/verify_action 唤醒后台线程继续执行。
 *
 * 对用户透明:不出现 verifier_agent 字样,只显示"验证动作需要授权"。
 */
export interface VerifyActionEventData {
  /** 动作唯一 ID(提交授权决议时回传) */
  action_id: string
  /** 动作类型:http_request / run_python_code / 其他 */
  type: string
  /** http_request 专用:HTTP 方法(GET/POST/PUT/DELETE 等) */
  method?: string
  /** http_request 专用:完整 URL(已拼接 test_env_url + path) */
  url?: string
  /** http_request 专用:请求头 */
  headers?: Record<string, string>
  /** http_request 专用:请求体 */
  body?: string
  /** http_request 专用:使用的登录身份 label(若有) */
  auth_profile?: string
  /** run_python_code 专用:PoC 代码(可能已截断) */
  code?: string
  /** run_python_code 专用:代码是否被截断 */
  code_truncated?: boolean
  /** 其他类型的原始参数 */
  args?: Record<string, unknown>
}

/**
 * 登录凭证(verifier_agent 的 http_request 按身份注入请求头)
 *
 * label 为身份标识(如"管理员"/"普通用户"),LLM 调 http_request 时通过
 * auth_profile=label 选择身份,工具自动把 header_name: header_value 加到请求头。
 * LLM 看不到 header_value 明文(安全)。
 */
export interface VerifierAuthToken {
  /** 身份标识(LLM 据此选择,如 '管理员'/'普通用户') */
  label: string
  /** 请求头名(如 'Authorization' / 'Cookie' / 'X-API-Key') */
  header_name: string
  /** 请求头值(如 'Bearer xxx' / 'session=yyy') */
  header_value: string
}

/** 验证动作授权请求(POST /tasks/{id}/verify_action body) */
export interface VerifyActionRequest {
  /** 对应 verify_action 事件的 action_id */
  action_id: string
  /** true=同意执行,false=拒绝 */
  approved: boolean
}

/** 验证动作授权响应 */
export interface VerifyActionResponse {
  /** 是否成功唤醒后台线程(false=无待授权动作或任务已结束) */
  accepted: boolean
  message?: string
}

/** 危险命令确认事件数据(SSE command_confirm 事件 payload) */
export interface CommandConfirmEventData {
  /** 命令唯一 ID(提交确认决议时回传) */
  command_id: string
  /** 待确认的命令内容 */
  command: string
  /** 触发工具:run_command / run_python_code */
  tool: string
  /** 拦截原因(匹配的危险命令模式) */
  reason: string
}

/** 危险命令确认请求(POST /tasks/{id}/command_confirm) */
export interface CommandConfirmRequest {
  command_id: string
  approved: boolean
}

/** 危险命令确认响应 */
export interface CommandConfirmResponse {
  accepted: boolean
  message?: string
}

/** 更新验证器配置请求(PATCH /tasks/{id}/verifier_config,运行时可调) */
export interface VerifyConfigUpdateRequest {
  /** 是否启用动态验证(可选,只更新传入字段) */
  verifier_enabled?: boolean
  /** 验证授权模式(可选) */
  verifier_auth_mode?: 'direct' | 'per_action'
  /** 测试环境 URL(可选) */
  test_env_url?: string
  /** 登录凭证列表(可选):传入则整体覆盖;空数组清空;undefined=不修改 */
  verifier_auth_tokens?: VerifierAuthToken[]
}

/** 运行时可调整的协作策略字段(任务级覆盖,增量合并到 task.params._agent_policy) */
export interface RuntimePolicyUpdate {
  /** 统一 K 值,每 K 个迭代评估一次(1-20) */
  checkpoint_interval?: number
  /** agent2 是否能打断 agent1 */
  allow_interrupt?: boolean
  /** agent2 协作总轮次(1-10) */
  max_rounds?: number
}

/**
 * 更新任务运行时配置请求(PATCH /tasks/{id}/runtime_config)
 *
 * 任务进行中修改 agent1 / agent2 模型与协作策略。
 * 生效时机:running/paused 的当前执行仍用启动时配置,
 * 修改在下一轮执行(completed 后追加消息 / failed 重试)时生效。
 */
export interface RuntimeConfigUpdateRequest {
  /** agent2 模型配置 id;空字符串=清除(回退 env 默认);undefined=不修改 */
  llm_config_id?: string
  /** agent1 模型配置 id(仅 executor=builtin);空字符串=清除(回退 llm_config_id) */
  react_llm_config_id?: string
  /** 协作策略(增量合并) */
  agent_policy?: RuntimePolicyUpdate
}

/**
 * agent 策略配置(检查点评估频率、打断权限、验证权限)
 *
 * 用户级默认存储在 UserPreference.agent_policy,任务级覆盖存储在
 * task.params["_agent_policy"]。resolve_agent_policy 合并两者后生效。
 */
export interface AgentPolicy {
  /** 是否启用 agent2(关闭=单 agent 模式,跳过评估/打断/验证) */
  agent2_enabled: boolean
  /** agent2 协作总轮次(1-10,仅 agent2 启用时生效) */
  max_rounds: number
  /** 统一 K 值,每 K 个迭代评估一次 */
  checkpoint_interval: number
  /** 高级:内置 agent1 专用 K 值(null=用统一值) */
  checkpoint_interval_builtin: number | null
  /** 高级:CLI agent 专用 K 值(null=用统一值) */
  checkpoint_interval_cli: number | null
  /** agent2 是否能打断 agent1 */
  allow_interrupt: boolean
  /** 每轮最多打断次数(防死锁) */
  max_interrupts_per_round: number
  /** agent2 是否能自己在测试环境验证(实验性,先留开关) */
  allow_verify: boolean
  /** 验证授权默认模式:"direct" 直接执行 / "per_action" 逐动作授权(任务级可覆盖) */
  verifier_auth_mode_default: 'direct' | 'per_action'
}

// ============================================================
// 用户补充消息(对话界面下方输入框)
// ============================================================

/** 发送用户补充消息请求(POST /tasks/{id}/messages) */
export interface SendMessageRequest {
  /** 消息内容(1-8000 字符) */
  content: string
}

/** 发送用户补充消息响应 */
export interface SendMessageResponse {
  /** 是否被接受(false 表示任务状态不允许或内容无效) */
  accepted: boolean
  /** 提示信息(展示给用户) */
  message?: string
}
