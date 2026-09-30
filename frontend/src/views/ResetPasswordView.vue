<script setup lang="ts">
/**
 * 重置密码页
 *
 * 用户点击重置邮件中的链接跳转到此页,URL 带 ?token=XXX。
 * 展示新密码输入表单,提交后调后端重置。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { resetPassword } from '@/api/auth'
import { extractErrorMessage } from '@/utils/error'

const route = useRoute()
const router = useRouter()

const newPassword = ref('')
const confirmPassword = ref('')
const showPassword = ref(false)
const loading = ref(false)
const error = ref('')
const success = ref('')

// token 从 URL 获取(邮件链接带 ?token=XXX)
const token = computed(() => route.query.token as string | undefined)

const passwordError = computed(() => {
  if (!newPassword.value) return '请输入新密码'
  if (newPassword.value.length < 8) return '密码至少 8 位'
  if (confirmPassword.value && newPassword.value !== confirmPassword.value) {
    return '两次输入的密码不一致'
  }
  return ''
})

const confirmError = computed(() => {
  if (!confirmPassword.value) return '请确认密码'
  if (newPassword.value !== confirmPassword.value) return '两次输入的密码不一致'
  return ''
})

const canSubmit = computed(
  () => !passwordError.value && !confirmError.value && token.value !== undefined,
)

async function handleSubmit(): Promise<void> {
  error.value = ''
  success.value = ''
  if (!token.value || !canSubmit.value) return

  loading.value = true
  try {
    const res = await resetPassword(token.value, newPassword.value)
    success.value = res.message
    // 3 秒后跳转登录
    setTimeout(() => router.push('/login'), 3000)
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="auth-header">
        <div class="logo">
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
        </div>
        <h1>设置新密码</h1>
      </div>

      <div v-if="!token" class="alert alert-error">
        重置链接缺少 token 参数,请重新申请重置密码
      </div>

      <Transition name="fade">
        <div v-if="error" class="alert alert-error" role="alert">{{ error }}</div>
      </Transition>
      <Transition name="fade">
        <div v-if="success" class="alert alert-success" role="status">
          {{ success }}(3 秒后跳转登录页...)
        </div>
      </Transition>

      <form v-if="token && !success" @submit.prevent="handleSubmit" novalidate>
        <div class="field">
          <label for="new-password">新密码</label>
          <div class="input-wrapper">
            <input
              id="new-password"
              v-model="newPassword"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="new-password"
              placeholder="至少 8 位"
              :class="{ invalid: passwordError }"
            />
            <button
              type="button"
              class="toggle-pwd"
              :aria-label="showPassword ? '隐藏密码' : '显示密码'"
              @click="showPassword = !showPassword"
            >
              <svg v-if="!showPassword" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
              <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
            </button>
          </div>
          <span v-if="passwordError" class="field-error">{{ passwordError }}</span>
        </div>

        <div class="field">
          <label for="confirm-password">确认密码</label>
          <input
            id="confirm-password"
            v-model="confirmPassword"
            :type="showPassword ? 'text' : 'password'"
            autocomplete="new-password"
            placeholder="再输入一次"
            :class="{ invalid: confirmError }"
          />
          <span v-if="confirmError" class="field-error">{{ confirmError }}</span>
        </div>

        <button type="submit" class="btn-primary" :disabled="loading || !canSubmit">
          <span v-if="loading" class="spinner" />
          {{ loading ? '处理中...' : '重置密码' }}
        </button>
      </form>

      <RouterLink to="/login" class="back-link">← 返回登录</RouterLink>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  /* 手机地址栏伸缩兜底 */
  min-height: 100dvh;
  padding: var(--space-6) var(--space-4);
  background: linear-gradient(135deg, #f0f4ff 0%, #f8fafc 50%, #faf5ff 100%);
}

.auth-card {
  width: 100%;
  max-width: var(--auth-card-width);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
  padding: var(--space-8);
}

.auth-header {
  text-align: center;
  margin-bottom: var(--space-6);
}

.logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: var(--radius-lg);
  background: var(--color-primary);
  color: var(--color-text-inverse);
  margin-bottom: var(--space-3);
}

.auth-header h1 {
  font-size: var(--fs-xl);
  font-weight: var(--fw-bold);
}

.alert {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

.alert-success {
  background: var(--color-success-light);
  color: var(--color-success);
  border: 1px solid #bbf7d0;
}

.field {
  margin-bottom: var(--space-4);
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
}

.field input {
  width: 100%;
  height: 42px;
  padding: 0 var(--space-3);
  font-size: var(--fs-base);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input::placeholder { color: var(--color-text-muted); }

.field input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input.invalid { border-color: var(--color-danger); }

.input-wrapper { position: relative; }
.input-wrapper input { padding-right: 44px; }

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
  border-radius: var(--radius-sm);
}

.toggle-pwd:hover { color: var(--color-text); }

.field-error {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

.btn-primary {
  width: 100%;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.btn-primary:hover:not(:disabled) { background: var(--color-primary-hover); }
.btn-primary:disabled { opacity: 0.6; }

.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

.back-link {
  display: block;
  text-align: center;
  font-size: var(--fs-sm);
  margin-top: var(--space-4);
}

.fade-enter-active, .fade-leave-active { transition: opacity var(--transition-base); }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
