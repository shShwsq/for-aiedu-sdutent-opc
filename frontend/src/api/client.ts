/**
 * Axios 客户端
 *
 * 职责:
 * - 统一 baseURL: /api(Vite proxy 转发到后端)
 * - 请求拦截:自动注入 Authorization: Bearer <access_token>
 * - 响应拦截:401 时自动尝试 refresh,刷新失败则清空 token 跳登录页
 *
 * token 存储:localStorage,key 见 TOKEN_KEYS
 */
import axios, {
  type AxiosInstance,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'

const TOKEN_KEYS = {
  access: 'agentpair_access_token',
  refresh: 'agentpair_refresh_token',
} as const

/** 从 localStorage 读 access token */
export function getAccessToken(): string | null {
  return localStorage.getItem(TOKEN_KEYS.access)
}

/** 从 localStorage 读 refresh token */
export function getRefreshToken(): string | null {
  return localStorage.getItem(TOKEN_KEYS.refresh)
}

/** 存储 token 对 */
export function setTokens(access: string, refresh: string): void {
  localStorage.setItem(TOKEN_KEYS.access, access)
  localStorage.setItem(TOKEN_KEYS.refresh, refresh)
}

/** 清空 token(登出 / 失效) */
export function clearTokens(): void {
  localStorage.removeItem(TOKEN_KEYS.access)
  localStorage.removeItem(TOKEN_KEYS.refresh)
}

// ---- 创建实例 ----

const client: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

// ---- 请求拦截:注入 Authorization ----

client.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = getAccessToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

// ---- 响应拦截:401 自动刷新 ----

// 防止并发刷新
let isRefreshing = false
let pendingQueue: Array<{
  resolve: (token: string) => void
  reject: (error: unknown) => void
}> = []

/** 通知队列中等待的请求:token 已刷新,继续 */
function notifyQueue(token: string): void {
  pendingQueue.forEach((item) => item.resolve(token))
  pendingQueue = []
}

/** 通知队列中等待的请求:刷新失败,放弃 */
function rejectQueue(error: unknown): void {
  pendingQueue.forEach((item) => item.reject(error))
  pendingQueue = []
}

/** 尝试用 refresh token 换新的 access token */
async function tryRefresh(): Promise<string> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) {
    throw new Error('no refresh token')
  }
  // 直接用 axios 裸调,不走 client 实例(避免拦截器递归)
  const res = await axios.post('/api/auth/refresh', { refresh_token: refreshToken })
  const newToken = res.data.access_token as string
  setTokens(newToken, refreshToken)
  return newToken
}

client.interceptors.response.use(
  (response: AxiosResponse) => response,
  async (error) => {
    const originalRequest = error.config as AxiosRequestConfig & { _retried?: boolean }

    // 非 401 或已重试过,直接抛
    if (error.response?.status !== 401 || originalRequest._retried) {
      return Promise.reject(error)
    }

    // refresh 接口本身 401:清空跳登录
    if (originalRequest.url?.includes('/auth/refresh')) {
      clearTokens()
      rejectQueue(error)
      redirectToLogin()
      return Promise.reject(error)
    }

    // 已有刷新在进行:排队等
    if (isRefreshing) {
      return new Promise((resolve, reject) => {
        pendingQueue.push({
          resolve: (token: string) => {
            originalRequest._retried = true
            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${token}`
            }
            resolve(client(originalRequest))
          },
          reject,
        })
      })
    }

    // 发起刷新
    isRefreshing = true
    try {
      const newToken = await tryRefresh()
      notifyQueue(newToken)
      originalRequest._retried = true
      if (originalRequest.headers) {
        originalRequest.headers.Authorization = `Bearer ${newToken}`
      }
      return client(originalRequest)
    } catch (refreshError) {
      clearTokens()
      rejectQueue(refreshError)
      redirectToLogin()
      return Promise.reject(refreshError)
    } finally {
      isRefreshing = false
    }
  },
)

/** 跳转登录页(避免硬编码路径,用 window.location) */
function redirectToLogin(): void {
  const current = window.location.pathname + window.location.search
  // 避免在登录页重复跳转
  if (!window.location.pathname.startsWith('/login')) {
    window.location.href = `/login?redirect=${encodeURIComponent(current)}`
  }
}

export default client
