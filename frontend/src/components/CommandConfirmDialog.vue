<script setup lang="ts">
/**
 * 危险命令确认弹窗
 *
 * local 模式下,LLM 调用 run_command 执行的危险命令(如 rm -rf /、curl | sh 等)
 * 会推送 command_confirm SSE 事件,前端用此组件弹出对话框让用户确认或拒绝。
 *
 * 用户选择后通过 @approve / @reject 事件通知父组件,由父组件调 API 提交决议。
 */
import type { CommandConfirmEventData } from '@/types/task'

const props = defineProps<{
  /** 是否显示弹窗 */
  open: boolean
  /** 待确认的危险命令详情 */
  command: CommandConfirmEventData | null
  /** 提交中状态(禁用按钮) */
  submitting?: boolean
}>()

const emit = defineEmits<{
  /** 用户同意执行该命令 */
  (e: 'approve', commandId: string): void
  /** 用户拒绝执行该命令 */
  (e: 'reject', commandId: string): void
}>()

function handleApprove(): void {
  if (!props.command?.command_id || props.submitting) return
  emit('approve', props.command.command_id)
}

function handleReject(): void {
  if (!props.command?.command_id || props.submitting) return
  emit('reject', props.command.command_id)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open && command" class="dialog-mask" @click.self="handleReject">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>⚠️ 危险命令需要确认</h3>
            <button
              class="dialog-close"
              :disabled="submitting"
              aria-label="关闭"
              @click="handleReject"
            >×</button>
          </header>

          <div class="dialog-body">
            <p class="dialog-intro">
              智能体想执行以下被安全策略标记为危险的命令,请确认是否允许。
            </p>

            <div class="command-detail">
              <div class="detail-row">
                <span class="detail-label">触发工具</span>
                <span class="detail-value">{{ command.tool }}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">拦截原因</span>
                <span class="detail-value danger">{{ command.reason }}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">命令内容</span>
                <pre class="command-text">{{ command.command }}</pre>
              </div>
            </div>

            <p class="dialog-warning">
              此命令可能在宿主机上直接执行(local 模式无沙箱隔离),请确认命令内容无害后再同意。
            </p>
          </div>

          <footer class="dialog-footer">
            <button
              class="btn btn-secondary"
              :disabled="submitting"
              @click="handleReject"
            >
              拒绝执行
            </button>
            <button
              class="btn btn-danger"
              :disabled="submitting"
              @click="handleApprove"
            >
              {{ submitting ? '提交中…' : '同意执行' }}
            </button>
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
}

.dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  max-width: 600px;
  width: 90%;
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
  margin: 0;
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-danger);
}

.dialog-close {
  background: none;
  border: none;
  font-size: var(--fs-xl);
  color: var(--color-text-secondary);
  cursor: pointer;
  padding: 0 var(--space-2);
  line-height: 1;
}

.dialog-close:hover:not(:disabled) {
  color: var(--color-text);
}

.dialog-close:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dialog-body {
  padding: var(--space-5);
  overflow-y: auto;
}

.dialog-intro {
  margin: 0 0 var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  line-height: 1.5;
}

.command-detail {
  background: var(--color-background);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4);
}

.detail-row {
  display: flex;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
}

.detail-row:last-child {
  margin-bottom: 0;
}

.detail-label {
  flex-shrink: 0;
  width: 80px;
  font-size: var(--fs-xs);
  color: var(--color-text-tertiary);
  font-weight: var(--fw-medium);
  padding-top: 2px;
}

.detail-value {
  font-size: var(--fs-sm);
  color: var(--color-text);
  word-break: break-all;
}

.detail-value.danger {
  color: var(--color-danger);
  font-weight: var(--fw-medium);
}

.command-text {
  flex: 1;
  margin: 0;
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}

.dialog-warning {
  margin: 0;
  padding: var(--space-3);
  background: var(--color-warning-bg, rgba(245, 158, 11, 0.1));
  border: 1px solid var(--color-warning-border, rgba(245, 158, 11, 0.3));
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  color: var(--color-warning, #b45309);
  line-height: 1.5;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.btn {
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  cursor: pointer;
  border: none;
  transition: background 0.15s, opacity 0.15s;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-secondary {
  background: var(--color-surface-alt);
  color: var(--color-text-secondary);
}

.btn-secondary:hover:not(:disabled) {
  background: var(--color-border);
}

.btn-danger {
  background: var(--color-danger);
  color: white;
}

.btn-danger:hover:not(:disabled) {
  opacity: 0.9;
}

.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
