/**
 * 主题管理(浅色 / 深色 / 跟随系统)
 *
 * 职责:
 * - 持久化用户选择到 localStorage(键:agentpair-theme-mode)
 * - 通过 <html data-theme="..."> 驱动 tokens.css 中的深浅色变量(全局换肤)
 * - system 模式下监听系统 prefers-color-scheme 变化,自动跟随
 *
 * 模块级单例:initTheme 在应用挂载前调用(避免首屏闪烁),
 * 之后组件通过 useTheme() 获取同一份响应式状态。
 */
import { computed, ref } from 'vue'

export type ThemeMode = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'agentpair_theme_mode'

/** 系统深色偏好媒体查询(jsdom 等测试环境可能缺失) */
const darkMedia: MediaQueryList | null =
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)')
    : null

/** 系统当前是否为深色(响应式,供 computed 自动重算) */
const systemDark = ref(darkMedia?.matches ?? false)

/** 用户选择的模式;initTheme 前为 null(视为跟随系统) */
const mode = ref<ThemeMode | null>(null)

/** 实际生效的主题:system 模式下跟随系统偏好 */
const resolved = computed<'light' | 'dark'>(() => {
  if (mode.value === 'light') return 'light'
  if (mode.value === 'dark') return 'dark'
  return systemDark.value ? 'dark' : 'light'
})

/** 应用主题到 <html data-theme>(tokens.css 据此切换深浅色变量) */
function apply(): void {
  document.documentElement.dataset.theme = resolved.value
}

let initialized = false

/** 应用启动时初始化:读取持久化偏好并应用 + 监听系统主题变化(需在挂载前调用,避免首屏闪烁) */
export function initTheme(): void {
  if (initialized) return
  initialized = true

  const saved = localStorage.getItem(STORAGE_KEY)
  mode.value = saved === 'light' || saved === 'dark' || saved === 'system' ? saved : 'system'
  apply()

  // 系统偏好变化:systemDark 更新后 apply 内经 computed 取最新值,自动跟随
  darkMedia?.addEventListener('change', (e) => {
    systemDark.value = e.matches
    apply()
  })
}

export function useTheme() {
  /** 切换主题模式并持久化到 localStorage */
  function setMode(next: ThemeMode): void {
    mode.value = next
    localStorage.setItem(STORAGE_KEY, next)
    apply()
  }

  return { mode, resolved, setMode }
}
