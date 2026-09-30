<script setup lang="ts">
/**
 * 用户澄清提问弹窗(阶段 8)
 *
 * agent2 在第 0 轮初始评估时,若认为用户意图不清晰,会输出 ask_user=true
 * + questions 列表。后端推送 question 事件,前端用此组件弹出对话框让用户填答。
 *
 * 支持两种题型:
 * - choice: 选择题(单选 radio / 多选 checkbox)
 * - text: 填空题(textarea)
 *
 * 最后一题固定为"是否有其他补充"(由后端追加),用于收集用户额外上下文。
 *
 * 提交后通过 @submit 事件把答案列表传给父组件,由父组件调 API。
 */
import { computed, reactive, watch } from 'vue'
import type {
  AnswerItem,
  ClarificationQuestion,
} from '@/types/task'

const props = defineProps<{
  /** 是否显示弹窗 */
  open: boolean
  /** 问题列表(最后一题固定为"是否有其他补充") */
  questions: ClarificationQuestion[]
  /** agent2 的判断依据(展示给用户参考,可选) */
  reasoning?: string
  /** 提问轮次(0=首次,1=用户回答后再问),用于标题显示 */
  askRound?: number
  /** 提交中状态(禁用按钮) */
  submitting?: boolean
}>()

const emit = defineEmits<{
  /** 提交答案 */
  (e: 'submit', answers: AnswerItem[]): void
  /** 关闭弹窗(用户主动取消) */
  (e: 'cancel'): void
}>()

// 答案状态:key = question_id,value = string | string[]
// - choice 单选:string(选中的 option value)
// - choice 多选:string[](选中的 option value 列表)
// - text:string(用户输入文本)
const answers = reactive<Record<string, string | string[]>>({})

/** 初始化/重置答案:open=true 且 questions 变化时,清空旧答案并设默认值 */
watch(
  () => [props.open, props.questions] as const,
  ([isOpen, qs]) => {
    if (!isOpen) return
    // 清空旧答案
    for (const key of Object.keys(answers)) {
      delete answers[key]
    }
    // 设默认值
    for (const q of qs) {
      if (q.type === 'choice') {
        answers[q.id] = q.multi ? [] : ''
      } else {
        answers[q.id] = ''
      }
    }
  },
  { immediate: true },
)

/** 切换单选选项 */
function selectSingle(q: ClarificationQuestion, value: string): void {
  answers[q.id] = value
}

/** 切换多选选项 */
function toggleMulti(q: ClarificationQuestion, value: string): void {
  const cur = (answers[q.id] as string[]) || []
  const idx = cur.indexOf(value)
  if (idx >= 0) {
    answers[q.id] = cur.filter((v) => v !== value)
  } else {
    answers[q.id] = [...cur, value]
  }
}

/** 判断多选是否选中 */
function isChecked(q: ClarificationQuestion, value: string): boolean {
  const cur = answers[q.id]
  return Array.isArray(cur) && cur.includes(value)
}

/** 校验:必填项不能为空 */
const validationError = computed<string | null>(() => {
  for (const q of props.questions) {
    if (!q.required) continue
    const v = answers[q.id]
    if (q.type === 'choice') {
      if (q.multi) {
        if (!Array.isArray(v) || v.length === 0) {
          return `请回答:${q.question}`
        }
      } else {
        if (!v) {
          return `请回答:${q.question}`
        }
      }
    } else {
      if (!v || !String(v).trim()) {
        return `请填写:${q.question}`
      }
    }
  }
  return null
})

/** 提交按钮是否可点 */
const canSubmit = computed(() => !validationError.value && !props.submitting)

function handleSubmit(): void {
  if (!canSubmit.value) return
  const result: AnswerItem[] = props.questions.map((q) => ({
    question_id: q.id,
    value: answers[q.id] ?? '',
  }))
  emit('submit', result)
}

function handleCancel(): void {
  emit('cancel')
}

/** 弹窗标题 */
const title = computed(() => {
  const round = props.askRound ?? 0
  return round === 0 ? '智能体需要你澄清几个问题' : `智能体还需要进一步澄清(第 ${round + 1} 次)`
})
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleCancel">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>{{ title }}</h3>
            <button
              class="dialog-close"
              :disabled="submitting"
              aria-label="关闭"
              @click="handleCancel"
            >×</button>
          </header>

          <div class="dialog-body">
            <p v-if="reasoning" class="dialog-reasoning">
              <span class="reasoning-label">智能体的判断:</span>
              {{ reasoning }}
            </p>

            <div
              v-for="(q, idx) in questions"
              :key="q.id"
              class="question-item"
            >
              <label class="question-title">
                <span class="question-text">{{ idx + 1 }}. {{ q.question }}</span>
                <span v-if="q.required" class="question-required" title="必填">*</span>
                <span v-if="q.multi" class="question-hint">(可多选)</span>
              </label>

              <!-- 选择题 -->
              <div v-if="q.type === 'choice'" class="question-options">
                <label
                  v-for="opt in q.options"
                  :key="opt.value"
                  class="option-item"
                  :class="{
                    'option-checked': q.multi
                      ? isChecked(q, opt.value)
                      : answers[q.id] === opt.value,
                  }"
                >
                  <input
                    :type="q.multi ? 'checkbox' : 'radio'"
                    :name="`q-${q.id}`"
                    :value="opt.value"
                    :checked="q.multi
                      ? isChecked(q, opt.value)
                      : answers[q.id] === opt.value"
                    :disabled="submitting"
                    @change="q.multi ? toggleMulti(q, opt.value) : selectSingle(q, opt.value)"
                  />
                  <span class="option-label">{{ opt.label }}</span>
                </label>
              </div>

              <!-- 填空题 -->
              <textarea
                v-else
                v-model="answers[q.id]"
                class="question-textarea"
                :placeholder="q.placeholder || ''"
                :disabled="submitting"
                rows="3"
              />
            </div>
          </div>

          <footer class="dialog-footer">
            <span v-if="validationError" class="validation-error">
              {{ validationError }}
            </span>
            <div class="footer-actions">
              <button
                class="btn btn-secondary"
                :disabled="submitting"
                @click="handleCancel"
              >取消</button>
              <button
                class="btn btn-primary"
                :disabled="!canSubmit"
                @click="handleSubmit"
              >
                <span v-if="submitting" class="btn-spinner" />
                {{ submitting ? '提交中...' : '提交' }}
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
  box-shadow: var(--shadow-xl, 0 20px 40px rgba(0, 0, 0, 0.15));
  width: 100%;
  max-width: 560px;
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
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.dialog-reasoning {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-primary);
  margin: 0;
}

.reasoning-label {
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.question-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.question-title {
  display: flex;
  align-items: baseline;
  gap: var(--space-1);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.question-required {
  color: var(--color-danger);
  font-weight: var(--fw-semibold);
}

.question-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
}

.question-options {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-left: var(--space-2);
}

.option-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  font-size: var(--fs-sm);
}

.option-item:hover {
  border-color: var(--color-border-strong);
  background: var(--color-surface-alt);
}

.option-checked {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.option-item input {
  margin: 0;
  cursor: pointer;
  accent-color: var(--color-primary);
}

.option-label {
  flex: 1;
  color: var(--color-text);
}

.question-textarea {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-family: inherit;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text);
  resize: vertical;
  transition: border-color var(--transition-fast);
}

.question-textarea:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.question-textarea:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
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
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
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
  filter: brightness(1.05);
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
  width: 12px;
  height: 12px;
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
