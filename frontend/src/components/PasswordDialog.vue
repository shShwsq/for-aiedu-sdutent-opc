<script setup lang="ts">
/**
 * 密码修改/设置 弹窗
 *
 * 复用 QuestionDialog / ModelConfigDialog 的视觉语言(mask + card + header/body/footer)。
 * - hasPassword=true:修改密码,需先验证当前密码
 * - hasPassword=false:设置密码(OAuth 用户首次设密码),跳过当前密码字段
 *
 * 草稿在 open=true 时重置;确定时 emit confirm,由父组件调 API 持久化。
 * 修改成功后的登出跳转由父组件处理。
 */
import { computed, reactive, watch } from 'vue'

const props = defineProps<{
  /** 是否显示 */
  open: boolean
  /** 是否已设密码(决定是否显示当前密码字段) */
  hasPassword: boolean
  /** 提交中状态(禁用按钮 + spinner) */
  loading: boolean
  /** 错误信息(父组件 API 失败时传入) */
  error?: string
}>()

const emit = defineEmits<{
  (e: 'confirm', payload: { current_password?: string; new_password: string }): void
  (e: 'cancel'): void
}>()

const draft = reactive({
  currentPassword: '',
  newPassword: '',
  confirmPassword: '',
  showPassword: false,
})

/** open 变 true 时重置草稿 */
watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) return
    draft.currentPassword = ''
    draft.newPassword = ''
    draft.confirmPassword = ''
    draft.showPassword = false
  },
  { immediate: true },
)

const currentPasswordError = computed(() => {
  if (!props.hasPassword) return ''
  if (!draft.currentPassword) return '请输入当前密码'
  return ''
})

const newPasswordError = computed(() => {
  if (!draft.newPassword) return '请输入新密码'
  if (draft.newPassword.length < 8) return '密码至少 8 位'
  if (props.hasPassword && draft.currentPassword && draft.newPassword === draft.currentPassword) {
    return '新密码不能与当前密码相同'
  }
  if (draft.confirmPassword && draft.newPassword !== draft.confirmPassword) {
    return '两次输入的密码不一致'
  }
  return ''
})

const confirmError = computed(() => {
  if (!draft.confirmPassword) return '请确认密码'
  if (draft.newPassword !== draft.confirmPassword) return '两次输入的密码不一致'
  return ''
})

const canSubmit = computed(
  () => !currentPasswordError.value && !newPasswordError.value && !confirmError.value && !props.loading,
)

function handleSubmit(): void {
  if (!canSubmit.value) return
  emit('confirm', {
    current_password: props.hasPassword ? draft.currentPassword : undefined,
    new_password: draft.newPassword,
  })
}

function handleCancel(): void {
  if (props.loading) return // 提交中不允许取消
  emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleCancel">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>{{ hasPassword ? '修改密码' : '设置密码' }}</h3>
            <button
              class="dialog-close"
              :disabled="loading"
              aria-label="关闭"
              @click="handleCancel"
            >×</button>
          </header>

          <div class="dialog-body">
            <p class="dialog-tip">
              {{ hasPassword
                ? '需先验证当前密码,新密码生效后需重新登录'
                : '当前账号通过 GitHub OAuth 登录,设置密码后可用邮箱密码登录' }}
            </p>

            <div v-if="hasPassword" class="field">
              <label for="pwd-current">当前密码</label>
              <div class="input-wrapper">
                <input
                  id="pwd-current"
                  v-model="draft.currentPassword"
                  :type="draft.showPassword ? 'text' : 'password'"
                  autocomplete="current-password"
                  placeholder="输入当前密码"
                  :class="{ invalid: currentPasswordError }"
                  :disabled="loading"
                />
                <button
                  type="button"
                  class="toggle-pwd"
                  :aria-label="draft.showPassword ? '隐藏密码' : '显示密码'"
                  @click="draft.showPassword = !draft.showPassword"
                >
                  <svg v-if="!draft.showPassword" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                  <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                    <line x1="1" y1="1" x2="23" y2="23" />
                  </svg>
                </button>
              </div>
              <span v-if="currentPasswordError" class="field-error">{{ currentPasswordError }}</span>
            </div>

            <div class="field">
              <label for="pwd-new">新密码</label>
              <div class="input-wrapper">
                <input
                  id="pwd-new"
                  v-model="draft.newPassword"
                  :type="draft.showPassword ? 'text' : 'password'"
                  autocomplete="new-password"
                  placeholder="至少 8 位"
                  :class="{ invalid: newPasswordError }"
                  :disabled="loading"
                />
                <button
                  v-if="!hasPassword"
                  type="button"
                  class="toggle-pwd"
                  :aria-label="draft.showPassword ? '隐藏密码' : '显示密码'"
                  @click="draft.showPassword = !draft.showPassword"
                >
                  <svg v-if="!draft.showPassword" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                  <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                    <line x1="1" y1="1" x2="23" y2="23" />
                  </svg>
                </button>
              </div>
              <span v-if="newPasswordError" class="field-error">{{ newPasswordError }}</span>
            </div>

            <div class="field">
              <label for="pwd-confirm">确认密码</label>
              <input
                id="pwd-confirm"
                v-model="draft.confirmPassword"
                :type="draft.showPassword ? 'text' : 'password'"
                autocomplete="new-password"
                placeholder="再输入一次"
                :class="{ invalid: confirmError }"
                :disabled="loading"
              />
              <span v-if="confirmError" class="field-error">{{ confirmError }}</span>
            </div>
          </div>

          <footer class="dialog-footer">
            <span v-if="error" class="validation-error">{{ error }}</span>
            <span v-else></span>
            <div class="footer-actions">
              <button
                class="btn btn-secondary"
                :disabled="loading"
                @click="handleCancel"
              >取消</button>
              <button
                class="btn btn-primary"
                :disabled="!canSubmit"
                @click="handleSubmit"
              >
                <span v-if="loading" class="btn-spinner" />
                {{ loading ? '处理中...' : (hasPassword ? '修改密码' : '设置密码') }}
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
  z-index: 1000;
  padding: var(--space-4);
}

.dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 100%;
  max-width: 460px;
  max-height: 90vh;
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
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5);
}

.dialog-tip {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-primary);
  margin: 0 0 var(--space-4);
}

/* ---- 表单字段 ---- */
.field {
  margin-bottom: var(--space-4);
}

.field:last-child {
  margin-bottom: 0;
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
  color: var(--color-text);
}

.field input {
  width: 100%;
  height: 42px;
  padding: 0 var(--space-3);
  font-size: var(--fs-base);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input::placeholder {
  color: var(--color-text-muted);
}

.field input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field input.invalid {
  border-color: var(--color-danger);
}

.input-wrapper {
  position: relative;
}

.input-wrapper input {
  padding-right: 44px;
}

.toggle-pwd {
  position: absolute;
  right: var(--space-2);
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  color: var(--color-text-muted);
  border: none;
  background: transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.toggle-pwd:hover {
  color: var(--color-text);
}

.field-error {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ---- footer ---- */
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

/* 弹窗淡入淡出 */
.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
