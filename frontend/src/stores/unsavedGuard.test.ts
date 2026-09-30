/**
 * unsavedGuard store 单元测试
 *
 * 重点覆盖路由守卫与弹窗的 Promise 流转,以及一个微任务时序竞争回归:
 * "保存并离开"成功后,页面的 watch 回调会先于 saveAndLeave 后续代码
 * 把 dirty 同步为 false,此时不得把导航取消掉(应按保存成功放行)。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useUnsavedGuardStore } from './unsavedGuard'

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('unsavedGuard store', () => {
  it('留在本页:导航被取消,脏状态保留', async () => {
    const store = useUnsavedGuardStore()
    store.syncDirty(true)

    const proceedP = store.confirmLeave()
    expect(store.dialogOpen).toBe(true)

    store.stay()
    await expect(proceedP).resolves.toBe(false)
    expect(store.dialogOpen).toBe(false)
    expect(store.dirty).toBe(true)
  })

  it('放弃并离开:清除脏状态并放行导航', async () => {
    const store = useUnsavedGuardStore()
    store.syncDirty(true, async () => true)

    const proceedP = store.confirmLeave()
    store.leave()

    await expect(proceedP).resolves.toBe(true)
    expect(store.dirty).toBe(false)
  })

  it('保存并离开:保存成功后放行导航', async () => {
    const store = useUnsavedGuardStore()
    let saved = false
    store.syncDirty(true, async () => {
      saved = true
      return true
    })

    const proceedP = store.confirmLeave()
    await store.saveAndLeave()

    expect(saved).toBe(true)
    await expect(proceedP).resolves.toBe(true)
    expect(store.dirty).toBe(false)
    expect(store.dialogOpen).toBe(false)
  })

  it('回归:保存期间页面 watch 先把 dirty 同步为 false,仍应放行导航', async () => {
    const store = useUnsavedGuardStore()
    store.syncDirty(true, async () => {
      // 模拟真实时序:保存成功后页面 dirty 变 false,composable 的 watch
      // 回调(微任务)先于 saveAndLeave 的 await 后续代码执行
      queueMicrotask(() => store.syncDirty(false))
      return true
    })

    const proceedP = store.confirmLeave()
    await store.saveAndLeave()

    await expect(proceedP).resolves.toBe(true)
    expect(store.dialogOpen).toBe(false)
    expect(store.dirty).toBe(false)
  })

  it('保存并离开:保存失败时留在弹窗内并显示错误', async () => {
    const store = useUnsavedGuardStore()
    store.syncDirty(true, async () => false)

    const proceedP = store.confirmLeave()
    await store.saveAndLeave()

    expect(store.dialogOpen).toBe(true)
    expect(store.saving).toBe(false)
    expect(store.error).not.toBe('')
    expect(store.dirty).toBe(true)
    // 导航仍被挂起,未放行
    let resolved = false
    void proceedP.then(() => {
      resolved = true
    })
    await Promise.resolve()
    expect(resolved).toBe(false)
  })

  it('弹窗打开且非保存流程中脏状态消失:按留在本页自动关闭', async () => {
    const store = useUnsavedGuardStore()
    store.syncDirty(true)

    const proceedP = store.confirmLeave()
    store.syncDirty(false)

    await expect(proceedP).resolves.toBe(false)
    expect(store.dialogOpen).toBe(false)
  })

  it('无保存回调时 canSave 为 false', () => {
    const store = useUnsavedGuardStore()
    store.syncDirty(true)
    expect(store.canSave).toBe(false)

    store.syncDirty(true, async () => true)
    expect(store.canSave).toBe(true)

    store.syncDirty(false)
    expect(store.canSave).toBe(false)
  })
})
