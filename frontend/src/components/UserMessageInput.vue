<script setup lang="ts">
/**
 * 用户补充消息输入框(对话界面下方)
 *
 * 用户在任务运行中/暂停中/完成后,可通过此输入框主动发送补充消息:
 * - running / paused:消息入队,agent1 下一迭代注入 LLM 上下文
 * - completed:启动新的协作 round(resume_audit_with_message)
 *
 * 交互:
 * - Enter 发送,Shift+Enter 换行
 * - 发送后清空输入框,自动聚焦以便连续输入
 * - 发送中(disabled)禁用输入,显示加载态
 * - 任务不可发送状态时显示禁用提示
 */
import { computed, nextTick, ref } from 'vue'

import { sendTaskMessage } from '@/api/task'
import { extractErrorMessage } from '@/utils/error'
import { clientLog } from '@/utils/clientLog'
import type { SendMessageResponse, TaskStatus } from '@/types/task'

const props = defineProps<{
  taskId: string
  taskStatus: TaskStatus
}>()

const emit = defineEmits<{
  /** 消息发送成功(后端已落库 + 推送 SSE,父组件无需额外处理) */
  sent: [response: SendMessageResponse]
  /** 发送失败(展示错误提示用) */
  error: [message: string]
}>()

const text = ref('')
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const sending = ref(false)
const localError = ref('')

/** 是否允许发送(运行中/暂停中/完成后可用,pending/failed 不可用) */
const canSend = computed(
  () =>
    !sending.value &&
    text.value.trim().length > 0 &&
    (props.taskStatus === 'running' ||
      props.taskStatus === 'paused' ||
      props.taskStatus === 'completed'),
)

/** 占位提示文案(随任务状态变化) */
const placeholder = computed(() => {
  switch (props.taskStatus) {
    case 'running':
      return '追加指令或补充要求(Enter 发送,Shift+Enter 换行)...'
    case 'paused':
      return '已暂停,可在此输入消息,恢复后智能体会处理...'
    case 'completed':
      return '继续追问或追加要求,将启动新一轮执行...'
    case 'pending':
      return '任务尚未开始,暂不可发送消息'
    case 'failed':
      return '任务已失败,暂不可发送消息'
    default:
      return '输入消息...'
  }
})

/** textarea 自动调整高度(1~6 行) */
function autoResize(): void {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  // 行高约 22px,最小 1 行,最大 6 行
  const maxHeight = 22 * 6 + 16
  el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`
}

function handleInput(): void {
  localError.value = ''
  autoResize()
}

function handleKeydown(e: KeyboardEvent): void {
  // Enter 发送,Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    void handleSend()
  }
}

async function handleSend(): Promise<void> {
  if (!canSend.value) return
  const content = text.value.trim()
  if (!content) return

  sending.value = true
  localError.value = ''
  try {
    const resp = await sendTaskMessage(props.taskId, { content })
    // [诊断] 消息发送结果:与后端 user_message 落库 / resume_start 对拍
    // (定位"消息已落库但前端提示失败"的响应丢失问题)
    clientLog(String(props.taskId), 'message_sent', {
      accepted: resp.accepted,
      message: resp.message,
    })
    if (resp.accepted) {
      text.value = ''
      emit('sent', resp)
      // 清空后重置高度 + 重新聚焦
      await nextTick()
      autoResize()
      textareaRef.value?.focus()
    } else {
      localError.value = resp.message || '消息发送失败'
      emit('error', localError.value)
    }
  } catch (err) {
    // [诊断] 消息发送异常:记录错误详情("未知错误,请稍后重试"的来源之一)
    clientLog(String(props.taskId), 'message_send_error', {
      error: extractErrorMessage(err),
    })
    localError.value = extractErrorMessage(err)
    emit('error', localError.value)
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="msg-input-wrapper">
    <div class="msg-input-row">
      <textarea
        ref="textareaRef"
        v-model="text"
        class="msg-input"
        :placeholder="placeholder"
        :disabled="sending || taskStatus === 'pending' || taskStatus === 'failed'"
        rows="1"
        @input="handleInput"
        @keydown="handleKeydown"
      />
      <button
        class="msg-send-btn"
        :disabled="!canSend"
        :title="canSend ? '发送(Enter)' : '输入消息后发送'"
        @click="handleSend"
      >
        <span v-if="sending" class="msg-send-spinner" />
        <svg
          v-else
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <line x1="22" y1="2" x2="11" y2="13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" />
        </svg>
      </button>
    </div>
    <p v-if="localError" class="msg-input-error">{{ localError }}</p>
  </div>
</template>

<style scoped>
.msg-input-wrapper {
  width: 94%;
  margin: 0 auto var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.msg-input-row {
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-2);
  box-shadow: var(--shadow-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.msg-input-row:focus-within {
  border-color: var(--color-primary);
  box-shadow: var(--shadow-lg);
}

.msg-input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  resize: none;
  font-family: inherit;
  font-size: var(--fs-sm);
  line-height: 1.5;
  color: var(--color-text);
  padding: var(--space-1) var(--space-2);
  max-height: 148px; /* 6 行 + padding */
  min-height: 24px;
  overflow-y: auto;
}

.msg-input::placeholder {
  color: var(--color-text-muted);
}

.msg-input:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.msg-send-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  padding: 0;
  color: var(--color-text-muted);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.msg-send-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.msg-send-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.msg-send-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: msg-send-spin 0.8s linear infinite;
}

@keyframes msg-send-spin {
  to {
    transform: rotate(360deg);
  }
}

.msg-input-error {
  font-size: var(--fs-xs);
  color: var(--color-danger);
  margin: 0;
  padding: 0 var(--space-2);
}

/* 手机档:输入区满宽 + 发送按钮触控目标增大 */
@media (max-width: 640px) {
  .msg-input-wrapper {
    width: 100%;
    margin-bottom: var(--space-3);
  }

  .msg-send-btn {
    width: 40px;
    height: 40px;
  }
}
</style>
