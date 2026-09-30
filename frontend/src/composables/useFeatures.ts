/**
 * 后端功能开关(对应 GET /health 返回的 features)
 *
 * 后端 PRACTICE_ENABLED=false 时 /practice/* 路由不注册(全部 404),
 * 前端据此隐藏练习相关入口(顶栏导航 / 任务详情「生成练习题」按钮)并拦截直连 /practice。
 * 开关只拉取一次并缓存;拉取失败保持默认开启,由后端 API 行为兜底。
 */
import { ref } from 'vue'

import client from '@/api/client'

/** 练习功能是否开启(默认 true,拉到 features.practice_enabled === false 才置 false) */
export const practiceEnabled = ref(true)

let loading: Promise<void> | null = null

/** 拉取并缓存功能开关(并发去重;失败时静默保持默认开,允许后续重试) */
export function ensureFeaturesLoaded(): Promise<void> {
  if (!loading) {
    loading = client
      .get('/health')
      .then((r) => {
        practiceEnabled.value = r.data?.features?.practice_enabled !== false
      })
      .catch(() => {
        // 静默失败:保持默认开,重置后下次调用可重试
        loading = null
      })
  }
  return loading
}
