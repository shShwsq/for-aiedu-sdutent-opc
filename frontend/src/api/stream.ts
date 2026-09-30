/**
 * SSE 客户端封装
 *
 * 浏览器原生 EventSource 不能自定义 header,所以通过 ?token=XXX 传递鉴权。
 *
 * 用法:
 *   const es = subscribeTaskStream(taskId, {
 *     onConversation: (data) => { ... },
 *     onStatus: (data) => { ... },
 *     onThinkingDelta: (data) => { ... },  // 流式 token 增量
 *     onDone: () => { ... },
 *     onError: (msg) => { ... },
 *   })
 *   // 组件卸载时:es.close()
 */
import { getAccessToken } from './client'
import { clientLog } from '@/utils/clientLog'
import type {
  AgentCheckpointEventData,
  ChecklistReviewEventData,
  CloneProgressEventData,
  CommandConfirmEventData,
  ConnectedData,
  ConversationEventData,
  ConversationUpdateEventData,
  DoneEventData,
  InterruptCancelledEventData,
  PlanEventData,
  QuestionEventData,
  SSEEvent,
  SSEEventType,
  StatusEventData,
  ThinkingDeltaEventData,
  VerifyActionEventData,
} from '@/types/task'

/** 事件回调接口 */
export interface StreamCallbacks {
  onConnected?: (data: ConnectedData) => void
  onConversation?: (data: ConversationEventData) => void
  /** 更新已有对话项的 content(如 Kimi 增量参数补全后刷新 tool_call 显示) */
  onConversationUpdate?: (data: ConversationUpdateEventData) => void
  onStatus?: (data: StatusEventData) => void
  /** 流式 token 增量(打字机效果)。每个 LLM 调用按 conv_id 累积 */
  onThinkingDelta?: (data: ThinkingDeltaEventData) => void
  /** 仓库克隆进度(local 模式 Popen 流式解析 git stderr 推送) */
  onCloneProgress?: (data: CloneProgressEventData) => void
  /** 计划清单更新(复杂任务时 agent1 输出 <plan>,后端提取推送) */
  onPlan?: (data: PlanEventData) => void
  /** 用户澄清提问(阶段 8:agent2 输出 ask_user=true 时触发) */
  onQuestion?: (data: QuestionEventData) => void
  /** 覆盖度清单确认(agent2 第 0 轮动态生成 checklist 后触发) */
  onChecklistReview?: (data: ChecklistReviewEventData) => void
  /** 动态验证动作授权(verifier_agent per_action 模式,每个 HTTP/PoC 动作需用户确认) */
  onVerifyAction?: (data: VerifyActionEventData) => void
  /** 危险命令确认(local 模式安全策略,LLM 执行危险命令时需用户确认) */
  onCommandConfirm?: (data: CommandConfirmEventData) => void
  /** agent2 检查点评估结果(迭代边界轻量评估,interrupt=true 时已注入追问) */
  onAgentCheckpoint?: (data: AgentCheckpointEventData) => void
  /** 用户取消了待生效的检查点打断(CLI 执行器 pending 窗口内) */
  onInterruptCancelled?: (data: InterruptCancelledEventData) => void
  onDone?: (data: DoneEventData) => void
  onError?: (data: DoneEventData) => void
}

/**
 * 订阅任务事件流
 *
 * 返回 EventSource 实例,调用 .close() 取消订阅。
 */
export function subscribeTaskStream(
  taskId: string,
  callbacks: StreamCallbacks,
): EventSource {
  const token = getAccessToken()
  const params = new URLSearchParams()
  if (token) params.set('token', token)

  const url = `/api/tasks/${taskId}/stream?${params.toString()}`
  const es = new EventSource(url)

  // 为每种事件类型注册监听器
  const eventTypes: SSEEventType[] = [
    'connected',
    'conversation',
    'conversation_update',
    'status',
    'thinking_delta',
    'clone_progress',
    'plan',
    'question',
    'checklist_review',
    'verify_action',
    'command_confirm',
    'agent_checkpoint',
    'interrupt_cancelled',
    'done',
    'error',
  ]

  for (const type of eventTypes) {
    es.addEventListener(type, (e: MessageEvent) => {
      try {
        const event = JSON.parse(e.data) as SSEEvent
        const data = event.data as Record<string, unknown>

        switch (type) {
          case 'connected':
            // [诊断] SSE 连接快照:与后端 stream_task_events 日志对拍
            clientLog(taskId, 'sse_connected', {
              status: data.status,
              current_stage: data.current_stage,
            })
            callbacks.onConnected?.(data as unknown as ConnectedData)
            break
          case 'conversation':
            callbacks.onConversation?.(data as unknown as ConversationEventData)
            break
          case 'conversation_update':
            callbacks.onConversationUpdate?.(data as unknown as ConversationUpdateEventData)
            break
          case 'status':
            // [诊断] 状态事件:记录后端推送的状态,与前端本地状态对拍
            clientLog(taskId, 'sse_status', {
              status: data.status,
              current_stage: data.current_stage,
            })
            callbacks.onStatus?.(data as unknown as StatusEventData)
            break
          case 'thinking_delta':
            callbacks.onThinkingDelta?.(data as unknown as ThinkingDeltaEventData)
            break
          case 'clone_progress':
            callbacks.onCloneProgress?.(data as unknown as CloneProgressEventData)
            break
          case 'plan':
            callbacks.onPlan?.(data as unknown as PlanEventData)
            break
          case 'question':
            callbacks.onQuestion?.(data as unknown as QuestionEventData)
            break
          case 'checklist_review':
            callbacks.onChecklistReview?.(data as unknown as ChecklistReviewEventData)
            break
          case 'verify_action':
            callbacks.onVerifyAction?.(data as unknown as VerifyActionEventData)
            break
          case 'command_confirm':
            callbacks.onCommandConfirm?.(data as unknown as CommandConfirmEventData)
            break
          case 'agent_checkpoint':
            callbacks.onAgentCheckpoint?.(data as unknown as AgentCheckpointEventData)
            break
          case 'interrupt_cancelled':
            callbacks.onInterruptCancelled?.(data as unknown as InterruptCancelledEventData)
            break
          case 'done':
            // [诊断] done 事件:任务结束,记录触发时前端是否在 resume 窗口
            clientLog(taskId, 'sse_done', { status: data.status })
            callbacks.onDone?.(data as unknown as DoneEventData)
            es.close()
            break
          case 'error':
            // [诊断] error 事件:前端显示"失败"的唯一 SSE 来源,全量记录
            clientLog(taskId, 'sse_error_event', {
              status: data.status,
              error_message: data.error_message,
            })
            callbacks.onError?.(data as unknown as DoneEventData)
            es.close()
            break
        }
      } catch (err) {
        console.error('SSE 事件解析失败:', err, e.data)
      }
    })
  }

  // EventSource 原生 error 事件(网络断开等)
  es.onerror = () => {
    // [诊断] 原生连接错误:浏览器自动重连;记录供与后端 SSE 断开日志对拍
    // (排查"前端显示失败但后端 running"时,确认是否有网络断连参与)
    clientLog(taskId, 'sse_native_error', {
      readyState: es.readyState,
      // 1=CONNECTING(自动重连中) 2=OPEN 3=CLOSED
    })
  }

  // [诊断] 连接打开(原生 onopen)
  es.onopen = () => {
    clientLog(taskId, 'sse_open')
  }

  return es
}
