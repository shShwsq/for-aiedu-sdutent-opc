/**
 * 模型设置相关类型定义(列表式)
 *
 * 用户可配置多个 LLM / Embedding 模型,每个配置有唯一 id 和自定义名称。
 * 任务提交时选择一个 llm_config_id,后端 orchestrator 按此 id 加载对应配置。
 *
 * 安全约定:
 * - 后端响应(GET)不返回 api_key 原文,只返回 has_api_key
 * - 前端表单 api_key 字段:已配置时 placeholder 提示"已配置,输入新值以替换"
 * - 保存时 api_key 为空串表示"保留已存的 key"(首次保存必须填)
 */

// ============================================================
// 厂商与模型清单(models_catalog.json)
// ============================================================

/** LLM 模型元信息 */
export interface LLMModelMeta {
  id: string
  /** 思考模式: hybrid(可开关) | only(强制) | none(不支持) */
  thinking?: 'hybrid' | 'only' | 'none'
  thinkingDefault?: boolean
  /** 单次输出上限(token),按官方能力保守取值;缺失时用厂商 fallback/系统默认 */
  outputLimit?: number
}

/** LLM 厂商 */
export interface LLMProvider {
  id: string
  name: string
  baseUrl: string
  apiKeyUrl?: string
  supportsThinking?: boolean
  thinkingParam?: string
  thinkingEnabledType?: string
  thinkingTemperature?: number
  nonThinkingTemperature?: number
  fallbackThinking?: string
  reasoningSplit?: boolean
  /** 模型未列 outputLimit 时的厂商级输出上限兜底 */
  fallbackOutputLimit?: number
  models: LLMModelMeta[]
}

/** Embedding 模型元信息 */
export interface EmbeddingModelMeta {
  id: string
  name: string
  dimension: number
  multimodal?: boolean
  dimensionsParam?: boolean
  multimodalEndpoint?: boolean
}

/** Embedding 厂商 */
export interface EmbeddingProvider {
  id: string
  name: string
  baseUrl: string
  apiKeyUrl?: string
  fallbackDimension?: number
  fallbackMultimodal?: boolean
  fallbackMultimodalEndpoint?: boolean
  fallbackDimensionsParam?: boolean
  models: EmbeddingModelMeta[]
}

/** 完整 catalog */
export interface ModelsCatalog {
  llmProviders: LLMProvider[]
  embeddingProviders: EmbeddingProvider[]
}

// ============================================================
// 用户已保存的配置(响应,不含 api_key 原文)——列表式
// ============================================================

export interface LLMConfigItemOut {
  id: string
  name: string
  provider: string
  model: string
  enable_thinking: boolean
  base_url: string | null
  /** 单次输出上限(null = 未显式设置,按 catalog/系统默认解析) */
  max_output_tokens: number | null
  has_api_key: boolean
}

export interface EmbeddingConfigItemOut {
  id: string
  name: string
  provider: string
  model: string
  base_url: string | null
  dimension: number
  has_api_key: boolean
}

export interface UserModelsResponse {
  llm_configs: LLMConfigItemOut[]
  embedding_configs: EmbeddingConfigItemOut[]
}

// ============================================================
// 保存配置(请求)——列表式
// ============================================================

export interface LLMConfigItem {
  id: string
  name: string
  provider: string
  /** 空串 = 保留已存的 key(更新时);首次保存必填 */
  api_key: string
  model: string
  enable_thinking: boolean
  /** 自定义 baseUrl,留空用 catalog 预设 */
  base_url: string | null
  /** 单次输出上限(max_tokens 钳制值),null = 用 catalog/系统默认 */
  max_output_tokens: number | null
}

export interface EmbeddingConfigItem {
  id: string
  name: string
  provider: string
  api_key: string
  model: string
  base_url: string | null
  dimension: number
}

export interface SaveModelsRequest {
  llm_configs?: LLMConfigItem[]
  embedding_configs?: EmbeddingConfigItem[]
}

// ============================================================
// 测试请求与响应
// ============================================================

export interface TestRequest {
  config_id: string
}

export interface TestResponse {
  success: boolean
  message: string
  latency_ms?: number | null
  /** 仅 embedding 测试返回 */
  dimension?: number | null
  /** 仅 LLM 测试返回:模型的实际回复文本 */
  reply?: string | null
}
