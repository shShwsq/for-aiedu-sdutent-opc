/**
 * 客户端诊断日志(前后端日志对拍)
 *
 * 把前端关键事件(SSE 事件、状态变更、失败提示、重试点击)上报到后端
 * POST /api/debug/client-log,追加写入 backend/logs/client.log。
 *
 * 目的:定位"前端显示失败但后端 running"等状态不一致问题——
 * 后端日志(事件总线 error/done 推送、SSE 连接快照)与前端日志按
 * 时间 + task_id 对拍,可还原"未知失败"出现时的完整事件序列。
 *
 * fire-and-forget:失败静默,不阻塞业务、不弹错误、不影响性能。
 * 高频事件(thinking_delta / clone_progress)不上报,防止日志膨胀。
 */
import { getAccessToken } from '@/api/client'

/**
 * 上报一条前端诊断日志
 * @param taskId 任务 id(可为空,全局事件如 SSE 连接错误)
 * @param event  事件名,如 "sse_error_event" / "task_status_changed"
 * @param detail 附加字段(将被后端截断到 200 字符/字段)
 */
export function clientLog(
  taskId: string,
  event: string,
  detail?: Record<string, unknown>,
): void {
  try {
    const token = getAccessToken()
    void fetch('/api/debug/client-log', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        task_id: taskId,
        event,
        detail,
        ts: new Date().toISOString(),
      }),
      keepalive: true, // 页面卸载时也尽量送达
    }).catch(() => {
      // 静默失败,不影响业务
    })
  } catch {
    // 静默失败(序列化异常等)
  }
}
