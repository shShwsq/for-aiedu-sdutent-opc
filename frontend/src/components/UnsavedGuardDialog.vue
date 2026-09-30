<script setup lang="ts">
/**
 * 未保存改动确认弹窗(全局单例,挂载在 App.vue)
 *
 * 切换路由时若当前页有未保存改动,由 router.beforeEach 触发:
 * - 留在本页:取消导航
 * - 保存并离开:调用页面提供的保存回调,成功后放行
 * - 放弃改动并离开:清除脏状态后放行
 *
 * 视觉与其他 dialog 一致(遮罩 + 卡片 + header/body/footer)。
 */
import { useUnsavedGuardStore } from '@/stores/unsavedGuard'

const store = useUnsavedGuardStore()
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="store.dialogOpen" class="dialog-mask" @click.self="store.stay()">
        <div class="dialog-card" role="dialog" aria-modal="true" aria-label="有未保存改动">
          <header class="dialog-header">
            <h3>有未保存改动</h3>
            <button
              class="dialog-close"
              :disabled="store.saving"
              aria-label="留在本页"
              @click="store.stay()"
            >×</button>
          </header>

          <div class="dialog-body">
            <p class="leave-tip">
              当前页面有未保存的改动。离开前你可以先保存,放弃改动将丢失所做修改。
            </p>
          </div>

          <footer class="dialog-footer">
            <span v-if="store.error" class="validation-error">{{ store.error }}</span>
            <span v-else></span>
            <div class="footer-actions">
              <button
                class="btn btn-secondary"
                :disabled="store.saving"
                @click="store.stay()"
              >留在本页</button>
              <button
                class="btn btn-danger-outline"
                :disabled="store.saving"
                @click="store.leave()"
              >放弃并离开</button>
              <button
                v-if="store.canSave"
                class="btn btn-primary"
                :disabled="store.saving"
                @click="store.saveAndLeave()"
              >
                <span v-if="store.saving" class="btn-spinner" />
                {{ store.saving ? '保存中…' : '保存并离开' }}
              </button>
            </div>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100;
  padding: var(--space-4);
}

.dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 100%;
  max-width: 460px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.dialog-header h3 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.dialog-close {
  background: none;
  border: none;
  font-size: 24px;
  line-height: 1;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.dialog-close:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.dialog-close:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dialog-body {
  padding: var(--space-5);
}

.leave-tip {
  font-size: var(--fs-sm);
  color: var(--color-text);
  margin: 0;
  line-height: var(--lh-relaxed);
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.validation-error {
  font-size: var(--fs-sm);
  color: var(--color-danger);
  flex: 1;
}

.footer-actions {
  display: flex;
  gap: var(--space-2);
}

.btn {
  height: 38px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-secondary:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

/* 放弃并离开:危险色描边按钮 */
.btn-danger-outline {
  background: var(--color-surface);
  color: var(--color-danger);
  border-color: var(--color-border);
}

.btn-danger-outline:hover:not(:disabled) {
  background: var(--color-danger-light);
  border-color: var(--color-danger);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, currentColor 30%, transparent);
  border-top-color: currentColor;
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
