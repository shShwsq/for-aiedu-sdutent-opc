<script setup lang="ts">
/**
 * 删除账号 弹窗
 *
 * 高危不可逆操作,需输入完整邮箱二次确认。
 * - 展示当前账号邮箱作为对照
 * - 输入框实时校验:必须与当前邮箱完全一致(忽略大小写)才允许提交
 * - 确认后 emit confirm,由父组件调 API 删除 + 登出跳转
 *
 * 视觉上强调危险性:危险色边框 + 警告图标 + 明确告知删除范围。
 */
import { computed, reactive, watch } from 'vue'

const props = defineProps<{
  /** 是否显示 */
  open: boolean
  /** 当前账号邮箱(用于校验输入) */
  currentEmail: string
  /** 提交中状态 */
  loading: boolean
  /** 错误信息 */
  error?: string
}>()

const emit = defineEmits<{
  (e: 'confirm', email: string): void
  (e: 'cancel'): void
}>()

const draft = reactive({
  email: '',
  acknowledged: false, // 是否勾选"我已了解后果"
})

watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) return
    draft.email = ''
    draft.acknowledged = false
  },
  { immediate: true },
)

const emailMismatch = computed(() => {
  if (!draft.email) return false
  return draft.email.trim().toLowerCase() !== props.currentEmail.toLowerCase()
})

const canSubmit = computed(
  () =>
    !!draft.email &&
    !emailMismatch.value &&
    draft.acknowledged &&
    !props.loading,
)

function handleSubmit(): void {
  if (!canSubmit.value) return
  emit('confirm', draft.email.trim())
}

function handleCancel(): void {
  if (props.loading) return
  emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleCancel">
        <div class="dialog-card danger-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <div class="header-title">
              <span class="danger-icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                  <line x1="12" y1="9" x2="12" y2="13" />
                  <line x1="12" y1="17" x2="12.01" y2="17" />
                </svg>
              </span>
              <h3>删除账号</h3>
            </div>
            <button
              class="dialog-close"
              :disabled="loading"
              aria-label="关闭"
              @click="handleCancel"
            >×</button>
          </header>

          <div class="dialog-body">
            <!-- 危险提示 -->
            <div class="danger-banner">
              <p class="banner-title">此操作不可恢复</p>
              <p class="banner-desc">
                将永久删除你的账号及以下数据:
              </p>
              <ul class="banner-list">
                <li>所有历史任务、对话与审计结果</li>
                <li>练习题库、答题记录与学习进度</li>
                <li>模型配置(API Key 等)与智能体设置</li>
                <li>项目记忆、长期记忆与偏好设置</li>
                <li>Git 平台账号关联(GitHub / Gitee)</li>
                <li>邮箱验证记录</li>
                <li>上传的自定义 Skill</li>
              </ul>
            </div>

            <!-- 邮箱确认 -->
            <div class="field">
              <label for="delete-email">
                请输入完整邮箱 <code>{{ currentEmail }}</code> 以确认
              </label>
              <input
                id="delete-email"
                v-model="draft.email"
                type="email"
                autocomplete="off"
                placeholder="输入你的账号邮箱"
                :class="{ invalid: emailMismatch }"
                :disabled="loading"
              />
              <span v-if="emailMismatch" class="field-error">邮箱不匹配</span>
            </div>

            <!-- 后果确认勾选 -->
            <label class="ack-row">
              <input
                v-model="draft.acknowledged"
                type="checkbox"
                :disabled="loading"
              />
              <span>我已了解删除后数据无法恢复</span>
            </label>
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
                class="btn btn-danger-solid"
                :disabled="!canSubmit"
                @click="handleSubmit"
              >
                <span v-if="loading" class="btn-spinner" />
                {{ loading ? '删除中...' : '永久删除账号' }}
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

/* 危险弹窗:加红色边框强调 */
.danger-card {
  border: 1px solid var(--color-danger);
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.header-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.danger-icon {
  display: inline-flex;
  color: var(--color-danger);
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

/* ---- 危险提示横幅 ---- */
.danger-banner {
  background: var(--color-danger-light);
  border: 1px solid #fecaca;
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4);
}

.banner-title {
  font-weight: var(--fw-semibold);
  color: var(--color-danger);
  margin: 0 0 var(--space-1);
  font-size: var(--fs-sm);
}

.banner-desc {
  font-size: var(--fs-sm);
  color: var(--color-text);
  margin: 0 0 var(--space-2);
}

.banner-list {
  margin: 0;
  padding-left: var(--space-5);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.banner-list li {
  list-style: disc;
}

/* ---- 邮箱输入 ---- */
.field {
  margin-bottom: var(--space-4);
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
  color: var(--color-text);
}

.field label code {
  font-family: var(--font-mono);
  background: var(--color-surface-alt);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  color: var(--color-text);
  word-break: break-all;
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
  border-color: var(--color-danger);
  box-shadow: 0 0 0 3px var(--color-danger-light);
}

.field input:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field input.invalid {
  border-color: var(--color-danger);
}

.field-error {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ---- 后果确认勾选 ---- */
.ack-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.ack-row input[type="checkbox"] {
  width: 16px;
  height: 16px;
  cursor: pointer;
}

.ack-row input:disabled {
  cursor: not-allowed;
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

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-secondary:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

/* 实心危险按钮(删除账号) */
.btn-danger-solid {
  background: var(--color-danger);
  color: var(--color-text-inverse);
}

.btn-danger-solid:hover:not(:disabled) {
  background: var(--color-danger-hover);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
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
