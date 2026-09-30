<script setup lang="ts">
/**
 * 邮箱验证页
 *
 * 用户点击验证邮件中的链接跳转到此页,URL 带 ?token=XXX。
 * 自动提取 token 调后端验证,展示结果。
 */
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { verifyEmail } from '@/api/auth'
import { extractErrorMessage } from '@/utils/error'

const route = useRoute()

const status = ref<'loading' | 'success' | 'error'>('loading')
const message = ref('')

onMounted(async () => {
  const token = route.query.token as string | undefined
  if (!token) {
    status.value = 'error'
    message.value = '验证链接缺少 token 参数'
    return
  }

  try {
    const res = await verifyEmail(token)
    status.value = 'success'
    message.value = res.message
  } catch (err) {
    status.value = 'error'
    message.value = extractErrorMessage(err)
  }
})
</script>

<template>
  <div class="result-page">
    <div class="result-card">
      <!-- 加载中 -->
      <template v-if="status === 'loading'">
        <div class="spinner-lg" />
        <h2>正在验证邮箱...</h2>
      </template>

      <!-- 成功 -->
      <template v-else-if="status === 'success'">
        <div class="status-icon success">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
            <polyline points="22 4 12 14.01 9 11.01" />
          </svg>
        </div>
        <h2>验证成功</h2>
        <p>{{ message }}</p>
      </template>

      <!-- 失败 -->
      <template v-else>
        <div class="status-icon error">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10" />
            <line x1="15" y1="9" x2="9" y2="15" />
            <line x1="9" y1="9" x2="15" y2="15" />
          </svg>
        </div>
        <h2>验证失败</h2>
        <p>{{ message }}</p>
      </template>

      <RouterLink to="/login" class="back-link">前往登录</RouterLink>
    </div>
  </div>
</template>

<style scoped>
.result-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  /* 手机地址栏伸缩兜底 */
  min-height: 100dvh;
  background: var(--color-bg);
}

.result-card {
  text-align: center;
  padding: var(--space-10) var(--space-8);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-md);
  max-width: 400px;
  width: calc(100% - var(--space-8));
}

.result-card h2 {
  margin-top: var(--space-4);
  font-size: var(--fs-lg);
}

.result-card p {
  margin-top: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

.status-icon {
  display: inline-flex;
}

.status-icon.success { color: var(--color-success); }
.status-icon.error { color: var(--color-danger); }

.spinner-lg {
  width: 40px;
  height: 40px;
  margin: 0 auto;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.back-link {
  display: inline-block;
  margin-top: var(--space-6);
  font-size: var(--fs-sm);
}
</style>
