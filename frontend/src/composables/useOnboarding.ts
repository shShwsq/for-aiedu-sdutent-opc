/**
 * 新手引导状态管理(单例 composable)
 *
 * 职责:
 * - 维护引导运行时状态:是否激活、当前路由步骤队列、当前步骤索引
 * - 读写 localStorage 完成标记(按 user.email 区分,版本号驱动失效)
 * - 暴露 setUser / maybeStartForRoute / next / prev / skip / replay 等 API
 *
 * 设计要点:
 * - 单例:用 module-level ref,所有调用方共享同一状态(整个应用同时只能有一个引导实例)
 * - 路由驱动:外部 watch 路由变化时调用 maybeStartForRoute,按需启动该路由未读步骤
 * - 完成粒度:按"路由 + 版本"标记,而非全局一个布尔。跳过 home 引导后,
 *   进入 task-create 仍会触发该路由的引导(更符合"按需介绍"的节奏)
 * - email 由 App.vue 在 user 加载完成后调用 setUser 设置一次,内部所有方法直接读,
 *   避免 API 处处透传 email
 */
import { computed, ref } from 'vue'

import {
  ONBOARDING_VERSION,
  getStepsForRoute,
  type OnboardingStep,
} from '@/data/onboardingSteps'

/** localStorage key 前缀;按用户 email + 版本号区分 */
const LS_KEY_PREFIX = 'agentpair:onboarding:completed'

// ---- 模块级单例状态 ----

const isActive = ref(false)
const currentSteps = ref<OnboardingStep[]>([])
const currentIndex = ref(0)
/** 当前用户 email(由 App.vue 设置);null 表示未登录,引导不启动 */
const userEmail = ref<string | null>(null)

/** 当前步骤(派生) */
const currentStep = computed<OnboardingStep | null>(() => {
  if (!isActive.value) return null
  if (currentIndex.value < 0 || currentIndex.value >= currentSteps.value.length) {
    return null
  }
  return currentSteps.value[currentIndex.value] ?? null
})

/** 是否还有下一步 */
const hasNext = computed(() => currentIndex.value < currentSteps.value.length - 1)

/** 当前是否处于第一步(用于决定"上一步"按钮是否禁用) */
const hasPrev = computed(() => currentIndex.value > 0)

// ---- localStorage 读写 ----

/** 构造 localStorage key:agentpair:onboarding:completed:{email}:{routeName}:v{version} */
function buildLsKey(email: string, routeName: string): string {
  // email 中可能含特殊字符,encodeURIComponent 防止 key 解析异常
  const emailPart = encodeURIComponent(email)
  return `${LS_KEY_PREFIX}:${emailPart}:${routeName}:v${ONBOARDING_VERSION}`
}

/** 检查某路由是否已标记完成 */
function isRouteCompleted(routeName: string): boolean {
  if (!userEmail.value) return false
  const key = buildLsKey(userEmail.value, routeName)
  try {
    return localStorage.getItem(key) === '1'
  } catch {
    // localStorage 不可用(隐私模式 / 禁用)时,视为未完成,每次都触发
    return false
  }
}

/** 标记某路由的引导为已完成 */
function markRouteCompleted(routeName: string): void {
  if (!userEmail.value) return
  const key = buildLsKey(userEmail.value, routeName)
  try {
    localStorage.setItem(key, '1')
  } catch {
    // 静默失败:隐私模式下无法持久化,本次会话内仍可用
  }
}

/** 清除某路由完成标记(用于重看) */
function clearRouteCompleted(routeName: string): void {
  if (!userEmail.value) return
  const key = buildLsKey(userEmail.value, routeName)
  try {
    localStorage.removeItem(key)
  } catch {
    // 静默
  }
}

// ---- 运行时控制 ----

/**
 * 设置当前用户 email(由 App.vue 在 user 加载完成后调用)。
 * email 变化(登录/登出/切换账号)会重置内部运行时状态,避免上一个用户的引导残留。
 */
function setUser(email: string | null): void {
  if (userEmail.value === email) return
  // email 变了:清空运行时状态,避免跨账号串扰
  isActive.value = false
  currentSteps.value = []
  currentIndex.value = 0
  userEmail.value = email
}

/**
 * 路由变化时调用:若该路由仍有未读步骤,启动引导。
 * @returns 是否启动了引导
 */
function maybeStartForRoute(routeName: string): boolean {
  // 已激活时不重复启动(避免路由内子组件切换打断)
  if (isActive.value) return false
  if (!userEmail.value) return false
  if (isRouteCompleted(routeName)) return false

  const steps = getStepsForRoute(routeName)
  if (steps.length === 0) return false

  currentSteps.value = steps
  currentIndex.value = 0
  isActive.value = true
  return true
}

/** 显式启动某路由的引导(用于"重看引导"入口,跳过完成检查) */
function startForRoute(routeName: string): void {
  const steps = getStepsForRoute(routeName)
  if (steps.length === 0) return
  currentSteps.value = steps
  currentIndex.value = 0
  isActive.value = true
}

/** 下一步:若已是最后一步,则完成本路由引导 */
function next(): void {
  if (!isActive.value) return
  if (currentIndex.value < currentSteps.value.length - 1) {
    currentIndex.value += 1
  } else {
    completeCurrentRoute()
  }
}

/** 上一步 */
function prev(): void {
  if (!isActive.value) return
  if (currentIndex.value > 0) {
    currentIndex.value -= 1
  }
}

/** 跳过当前路由剩余步骤(标记本路由完成,关闭引导) */
function skip(): void {
  if (!isActive.value) return
  const routeName = currentStep.value?.route
  if (routeName) markRouteCompleted(routeName)
  isActive.value = false
  currentSteps.value = []
  currentIndex.value = 0
}

/** 完成当前路由引导(走到最后一步后调用;持久化完成标记) */
function completeCurrentRoute(): void {
  if (!isActive.value) return
  const routeName = currentStep.value?.route
  if (routeName) markRouteCompleted(routeName)
  isActive.value = false
  currentSteps.value = []
  currentIndex.value = 0
}

/**
 * 重看指定路由的引导:清除完成标记后立即启动。
 * 通常由"重看新手引导"入口调用,路由名由调用方决定(一般是当前路由或 home)。
 */
function replay(routeName: string): void {
  clearRouteCompleted(routeName)
  startForRoute(routeName)
}

/** 暴露给组件的 composable API */
export function useOnboarding() {
  return {
    // 状态
    isActive,
    currentStep,
    currentIndex,
    currentSteps,
    hasNext,
    hasPrev,
    // 用户管理
    setUser,
    // 路由驱动
    maybeStartForRoute,
    startForRoute,
    // 步骤控制
    next,
    prev,
    skip,
    completeCurrentRoute,
    replay,
    // 查询
    isRouteCompleted,
  }
}
