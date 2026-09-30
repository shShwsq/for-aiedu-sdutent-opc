/**
 * 未保存改动离开守卫(composable)
 *
 * 用法:在含表单/编辑器的页面 setup 中调用
 *   useUnsavedGuard(isDirty, saveAll)
 *
 * 行为:
 * - 将页面脏状态实时同步到全局 unsavedGuard store
 * - dirty 时切换路由会被 router.beforeEach 拦截,弹窗询问
 *   (留在本页 / 保存并离开 / 放弃改动并离开)
 * - saveAll 可选:提供后弹窗出现"保存并离开"按钮;返回 true 表示保存成功
 * - 组件卸载时自动清除脏状态,避免影响其他页面
 */
import { onBeforeUnmount, watch, type Ref } from 'vue'

import { useUnsavedGuardStore, type SaveHandler } from '@/stores/unsavedGuard'

export function useUnsavedGuard(isDirty: Ref<boolean>, saveAll?: SaveHandler): void {
  const store = useUnsavedGuardStore()

  watch(
    isDirty,
    (d) => {
      store.syncDirty(d, d ? saveAll ?? null : null)
    },
    { immediate: true },
  )

  onBeforeUnmount(() => {
    store.clear()
  })
}
