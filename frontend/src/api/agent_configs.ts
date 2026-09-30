/**
 * 外部 CLI agent 配置 API 模块
 *
 * 对应后端 app/routers/agents.py 的端点(动态多 agent 架构)。
 * - GET    /agents/types              所有已注册 agent 类型(无需登录)
 * - GET    /agents/configs             当前用户已配置的 agent 列表(鉴权)
 * - GET    /agents/configs/{type}     单个配置详情(鉴权)
 * - PUT    /agents/configs/{type}      保存配置(鉴权)
 * - POST   /agents/configs/{type}/test 测试凭证连通性(鉴权,SSE 流式推送进度+思考+回答)
 * - DELETE /agents/configs/{type}      删除配置(鉴权,返回剩余列表)
 *
 * 返回值已解包(取 response.data),调用方直接拿业务数据。
 */
import client, { getAccessToken } from './client'
import type {
  AgentConfigDetailOut,
  AgentConfigListResponse,
  AgentTypeMeta,
  SaveAgentConfigRequest,
} from '@/types/agent_configs'

/** 获取所有已注册 agent 类型及其凭据字段定义(无需登录) */
export function getAgentTypes(): Promise<AgentTypeMeta[]> {
  return client.get('/agents/types').then((r) => r.data)
}

/** 获取当前用户已配置的 agent 列表(不含凭据明文) */
export function getAgentConfigs(): Promise<AgentConfigListResponse> {
  return client.get('/agents/configs').then((r) => r.data)
}

/** 获取指定 agent 类型的配置详情(含各凭据字段是否已设置) */
export function getAgentConfig(agent_type: string): Promise<AgentConfigDetailOut> {
  return client.get(`/agents/configs/${agent_type}`).then((r) => r.data)
}

/** 保存指定 agent 类型的配置(凭据 secret 字段空串=保留,非空=更新) */
export function saveAgentConfig(
  agent_type: string,
  body: SaveAgentConfigRequest,
): Promise<AgentConfigDetailOut> {
  return client.put(`/agents/configs/${agent_type}`, body).then((r) => r.data)
}

/** 删除指定 agent 类型的配置(返回删除后剩余的列表) */
export function deleteAgentConfig(agent_type: string): Promise<AgentConfigListResponse> {
  return client.delete(`/agents/configs/${agent_type}`).then((r) => r.data)
}

/**
 * 测试事件回调接口(SSE 流式)
 *
 * 后端通过 SSE 推送以下事件类型:
 * - stage:    测试阶段进度(创建沙箱/启动 bridge/握手/发 prompt 等)
 * - thinking: 模型思考增量(reasoning chunk)
 * - content:  模型回答增量(agent_message_chunk)
 * - done:     测试完成(终止事件,ok=true/false)
 * - error:    测试异常(终止事件)
 */
export interface AgentTestStreamCallbacks {
  /** 阶段进度(stage_id + 人类可读消息) */
  onStage?: (stage: string, message: string) => void
  /** 模型思考增量 */
  onThinking?: (delta: string) => void
  /** 模型回答增量 */
  onContent?: (delta: string) => void
  /** 测试完成(终止事件) */
  onDone?: (ok: boolean, message: string) => void
  /** 网络错误/连接失败(非 SSE 业务 error 事件) */
  onError?: (message: string) => void
}

/**
 * 测试指定 agent 类型的凭证连通性(SSE 流式)
 *
 * 用原生 fetch + ReadableStream 消费 SSE(EventSource 不支持 POST + 自定义 header)。
 * 后端鉴权用 Authorization: Bearer <token>。
 *
 * 鉴权/配置校验失败时后端返回 4xx,此处解析 detail 后调 onError。
 * 必须先保存配置才能测试(测试用的是已加密存储的凭证)。
 *
 * 调用方应在调用前设置 loading,onDone/onError 后清除 loading。
 *
 * 可选 signal:离开页面或切换 agent 时调用方传入 AbortSignal 中止流,
 * 避免后端沙箱继续空跑;中止后 fetch 抛 AbortError,此处静默忽略(不触发 onError)。
 */
export async function testAgentConfig(
  agent_type: string,
  callbacks: AgentTestStreamCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  const token = getAccessToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  }
  if (token) headers.Authorization = `Bearer ${token}`

  let resp: Response
  try {
    resp = await fetch(`/api/agents/configs/${agent_type}/test`, {
      method: 'POST',
      headers,
      body: null,
      signal,
    })
  } catch (err) {
    // 主动中止不视为错误,静默忽略(不触发 onError 回调,避免覆盖已被调用方清理的状态)
    if (signal?.aborted) return
    callbacks.onError?.(`网络请求失败: ${err instanceof Error ? err.message : String(err)}`)
    return
  }

  // 非 200:解析后端错误 detail(JSON)后回调
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      if (body.detail) detail = String(body.detail)
    } catch {
      // 非 JSON 响应,用 status text
      detail = `HTTP ${resp.status} ${resp.statusText}`
    }
    // 401 单独提示(可能 token 过期)
    if (resp.status === 401) {
      detail = `鉴权失败(登录可能已过期): ${detail}`
    }
    callbacks.onError?.(detail)
    return
  }

  // 流式读取并解析 SSE
  const reader = resp.body?.getReader()
  if (!reader) {
    callbacks.onError?.('浏览器不支持流式读取(ReadableStream 不可用)')
    return
  }

  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE 事件以空行(\n\n)分隔,可能一次收到多个事件
      let sepIdx: number
      while ((sepIdx = buffer.indexOf('\n\n')) !== -1) {
        const rawEvent = buffer.slice(0, sepIdx)
        buffer = buffer.slice(sepIdx + 2)
        parseAndDispatchSse(rawEvent, callbacks)
      }
    }
    // flush 残留(理论上正常 SSE 都以 \n\n 结尾,防御性处理)
    if (buffer.trim()) {
      parseAndDispatchSse(buffer, callbacks)
    }
  } catch (err) {
    // 主动中止静默忽略(同上,不触发 onError)
    if (signal?.aborted) return
    callbacks.onError?.(`流式读取失败: ${err instanceof Error ? err.message : String(err)}`)
  }
}

/**
 * 解析单个 SSE 事件块并分发到回调
 *
 * SSE 块格式:
 *   event: <type>
 *   data: <json>
 *
 * data JSON 结构: {"type":"<type>","data":{...}}
 */
function parseAndDispatchSse(rawEvent: string, cb: AgentTestStreamCallbacks): void {
  const lines = rawEvent.split('\n')
  let eventType = 'message'
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith('event:')) {
      eventType = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trim())
    }
  }
  if (dataLines.length === 0) return

  let payload: { type?: string; data?: Record<string, unknown> }
  try {
    payload = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }

  const data = payload.data ?? {}
  switch (eventType) {
    case 'stage':
      cb.onStage?.(String(data.stage ?? ''), String(data.message ?? ''))
      break
    case 'thinking':
      cb.onThinking?.(String(data.delta ?? ''))
      break
    case 'content':
      cb.onContent?.(String(data.delta ?? ''))
      break
    case 'done':
      cb.onDone?.(Boolean(data.ok), String(data.message ?? ''))
      break
    case 'error':
      // 业务 error 事件也走 onError(终止)
      cb.onError?.(String(data.message ?? '测试失败'))
      break
    default:
      // 未知事件类型忽略
      break
  }
}
