/**
 * 错误处理工具
 *
 * 从 axios 错误中提取后端返回的 detail 字段(FastAPI HTTPException 格式)
 */
import axios from 'axios'

/**
 * 从未知错误中提取人类可读的消息
 *
 * 优先级:
 * 1. axios 错误 → 后端 detail 字段
 * 2. Error 实例 → message
 * 3. 兜底 → 网络错误
 */
export function extractErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string') return detail
    // FastAPI 校验错误 detail 是数组
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0]
      if (first?.msg) return first.msg
    }
    if (err.response?.status === 0 || !err.response) {
      return '网络错误,请检查网络连接后重试'
    }
    return `请求失败(${err.response.status})`
  }
  if (err instanceof Error) return err.message
  return '未知错误,请稍后重试'
}
