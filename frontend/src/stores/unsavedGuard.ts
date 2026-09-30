/**
 * 未保存改动导航守卫(Pinia)
 *
 * 职责:
 * - 持有"当前页面是否有未保存改动"状态(由各页面通过 useUnsavedGuard 同步)
 * - 提供 router.beforeEach 调用的 confirmLeave():弹出确认弹窗并以 Promise 返回用户选择
 * - 支持三种选择:留在本页 / 保存并离开(调用页面提供的保存回调)/ 放弃改动并离开
 * - dirty 期间监听 beforeunload,覆盖刷新/关闭标签页场景
 *
 * 弹窗 UI 由 UnsavedGuardDialog.vue 渲染(挂载在 App.vue),本 store 只管状态与 Promise 流转。
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import { extractErrorMessage } from '@/utils/error'

/** 页面提供的保存回调:返回 true 表示保存成功 */
export type SaveHandler = () => Promise<boolean>

export const useUnsavedGuardStore = defineStore('unsavedGuard', () => {
  // ---- state ----

  /** 当前页面是否有未保存改动 */
  const dirty = ref(false)
  /** 当前页面提供的保存回调(dirty 为 false 时清空) */
  const saveHandler = ref<SaveHandler | null>(null)

  /** 确认弹窗是否打开 */
  const dialogOpen = ref(false)
  /** "保存并离开"进行中 */
  const saving = ref(false)
  /** 保存失败时的弹窗内错误提示 */
  const error = ref('')

  /** 等待用户选择的 Promise resolver(弹窗关闭时兑现) */
  let resolver: ((proceed: boolean) => void) | null = null

  // ---- getters ----

  /** 是否提供保存能力(决定弹窗是否显示"保存并离开"按钮) */
  const canSave = computed(() => saveHandler.value !== null)

  // ---- actions ----

  /** 页面侧同步脏状态(由 useUnsavedGuard 调用) */
  function syncDirty(d: boolean, handler: SaveHandler | null = null): void {
    dirty.value = d
    saveHandler.value = d ? handler : null
    // 弹窗打开期间脏状态消失(如用户撤销了编辑):按"留在本页"关闭。
    // 保存流程中(saving)除外:保存成功后页面 watch 会先于 saveAndLeave
    // 后续代码把 dirty 同步为 false,若在此处关闭会把导航取消掉,
    // 应由 saveAndLeave 以 closeDialog(true) 放行。
    if (!d && dialogOpen.value && !saving.value) {
      closeDialog(false)
    }
  }

  /** 清除脏状态(页面卸载 / 用户选择放弃) */
  function clear(): void {
    dirty.value = false
    saveHandler.value = null
  }

  /**
   * 路由守卫调用:弹窗询问用户,返回 true=放行导航
   * (resolve true 后原始导航继续,无需重新 push)
   */
  function confirmLeave(): Promise<boolean> {
    error.value = ''
    dialogOpen.value = true
    return new Promise<boolean>((resolve) => {
      resolver = resolve
    })
  }

  /** 留在本页:取消导航 */
  function stay(): void {
    closeDialog(false)
  }

  /** 放弃改动并离开:清脏状态后放行 */
  function leave(): void {
    clear()
    closeDialog(true)
  }

  /** 保存并离开:调用页面保存回调,成功放行、失败留在弹窗内 */
  async function saveAndLeave(): Promise<void> {
    const fn = saveHandler.value
    if (!fn) {
      leave()
      return
    }
    saving.value = true
    error.value = ''
    try {
      const ok = await fn()
      if (ok) {
        clear()
        closeDialog(true)
      } else {
        error.value = '保存失败,请留在本页重试或选择放弃改动'
      }
    } catch (err) {
      error.value = extractErrorMessage(err)
    } finally {
      saving.value = false
    }
  }

  /** 关闭弹窗并兑现守卫 Promise */
  function closeDialog(proceed: boolean): void {
    dialogOpen.value = false
    const r = resolver
    resolver = null
    r?.(proceed)
  }

  // ---- beforeunload:dirty 期间拦截刷新/关闭标签页 ----
  function onBeforeUnload(e: BeforeUnloadEvent): void {
    e.preventDefault()
    // Chrome 要求设置 returnValue 才会弹原生确认
    e.returnValue = ''
  }

  watch(dirty, (d) => {
    if (d) window.addEventListener('beforeunload', onBeforeUnload)
    else window.removeEventListener('beforeunload', onBeforeUnload)
  })

  return {
    // state
    dirty,
    dialogOpen,
    saving,
    error,
    // getters
    canSave,
    // actions
    syncDirty,
    clear,
    confirmLeave,
    stay,
    leave,
    saveAndLeave,
  }
})
