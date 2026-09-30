<script setup lang="ts">
/**
 * 覆盖度清单确认弹窗
 *
 * agent2 在第 0 轮动态生成覆盖度清单(checklist)后,后端推送 checklist_review
 * SSE 事件,前端用此组件弹出对话框让用户编辑确认。
 *
 * 支持的编辑操作:
 * - 编辑维度的 name / description
 * - 编辑维度下的 checklist 子项(增删改文本)
 * - 新增维度 / 删除维度
 *
 * 两个提交按钮:
 * - "直接采用":提交 null(直接采用 agent2 生成的清单,不编辑)
 * - "确认编辑":提交编辑后的 checklist
 *
 * 风格参考 QuestionDialog.vue(同项目内的弹窗组件)。
 */
import { computed, reactive, watch } from 'vue'
import type { ChecklistDimension } from '@/types/task'

const props = defineProps<{
  /** 是否显示弹窗 */
  open: boolean
  /** agent2 生成的覆盖度清单 */
  checklist: ChecklistDimension[]
  /** agent2 生成清单的依据(展示给用户参考,可选) */
  reasoning?: string
  /** 提交中状态(禁用按钮) */
  submitting?: boolean
}>()

const emit = defineEmits<{
  /** 提交编辑后的清单(null=直接采用,数组=用户编辑后的清单) */
  (e: 'submit', checklist: ChecklistDimension[] | null): void
  /** 关闭弹窗(用户主动取消) */
  (e: 'cancel'): void
}>()

/**
 * 本地可编辑的清单副本
 *
 * props.checklist 是只读的(来自后端),这里深拷贝一份供用户编辑。
 * 维度 id 保持只读(用于后端识别);name/description/checklist 可编辑。
 */
const editingChecklist = reactive<ChecklistDimension[]>([])

/** open=true 且 checklist 变化时,重置本地编辑副本 */
watch(
  () => [props.open, props.checklist] as const,
  ([isOpen, cl]) => {
    if (!isOpen) return
    // 深拷贝:避免修改 props 原数据
    editingChecklist.splice(
      0,
      editingChecklist.length,
      ...cl.map((d) => ({
        id: d.id,
        name: d.name,
        description: d.description,
        checklist: [...d.checklist],
      })),
    )
  },
  { immediate: true },
)

/** 新增维度:生成临时 id(前端构造,后端按需重映射) */
function addDimension(): void {
  const tempId = `dim-${Date.now()}-${editingChecklist.length}`
  editingChecklist.push({
    id: tempId,
    name: '新维度',
    description: '',
    checklist: [],
  })
}

/** 删除维度 */
function removeDimension(idx: number): void {
  editingChecklist.splice(idx, 1)
}

/** 给维度新增检查项 */
function addChecklistItem(dim: ChecklistDimension): void {
  dim.checklist.push('')
}

/** 删除维度的某个检查项 */
function removeChecklistItem(dim: ChecklistDimension, idx: number): void {
  dim.checklist.splice(idx, 1)
}

/** 校验:每个维度必须有名称,每个检查项不能为空 */
const validationError = computed<string | null>(() => {
  if (editingChecklist.length === 0) return null // 允许清空所有维度
  for (const d of editingChecklist) {
    if (!d.name.trim()) return '维度名称不能为空'
    for (const item of d.checklist) {
      if (!item.trim()) return '检查项不能为空(请删除空项或填写内容)'
    }
  }
  return null
})

/** "确认编辑"按钮是否可点(校验通过 + 非提交中) */
const canSubmitEdited = computed(() => !validationError.value && !props.submitting)

/** 直接采用(提交 null) */
function handleAcceptAsIs(): void {
  if (props.submitting) return
  emit('submit', null)
}

/** 确认编辑(提交编辑后的 checklist) */
function handleSubmitEdited(): void {
  if (!canSubmitEdited.value) return
  // 过滤掉空的检查项 + trim 文本
  const cleaned: ChecklistDimension[] = editingChecklist.map((d) => ({
    id: d.id,
    name: d.name.trim(),
    description: d.description.trim(),
    checklist: d.checklist.map((s) => s.trim()).filter((s) => s.length > 0),
  }))
  emit('submit', cleaned)
}

function handleCancel(): void {
  emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleCancel">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>请确认覆盖度清单</h3>
            <button
              class="dialog-close"
              :disabled="submitting"
              aria-label="关闭"
              @click="handleCancel"
            >×</button>
          </header>

          <div class="dialog-body">
            <p class="dialog-intro">
              智能体已根据你的任务说明生成以下覆盖度清单,你可以直接采用,或编辑后确认。
            </p>
            <p v-if="reasoning" class="dialog-reasoning">
              <span class="reasoning-label">智能体的判断:</span>
              {{ reasoning }}
            </p>

            <div
              v-for="(dim, dimIdx) in editingChecklist"
              :key="dim.id"
              class="dimension-item"
            >
              <div class="dimension-header">
                <span class="dimension-id" :title="`维度 id: ${dim.id}`">#{{ dimIdx + 1 }}</span>
                <input
                  v-model="dim.name"
                  class="dimension-name-input"
                  placeholder="维度名称"
                  :disabled="submitting"
                />
                <button
                  type="button"
                  class="btn-remove-dim"
                  :disabled="submitting"
                  title="删除该维度"
                  @click="removeDimension(dimIdx)"
                >删除维度</button>
              </div>

              <textarea
                v-model="dim.description"
                class="dimension-desc-input"
                placeholder="维度描述(可选)"
                :disabled="submitting"
                rows="2"
              />

              <div class="checklist-items">
                <div class="checklist-items-header">
                  <span class="checklist-items-title">检查项</span>
                  <button
                    type="button"
                    class="btn-add-item"
                    :disabled="submitting"
                    @click="addChecklistItem(dim)"
                  >+ 添加检查项</button>
                </div>
                <div
                  v-for="(_, itemIdx) in dim.checklist"
                  :key="itemIdx"
                  class="checklist-item-row"
                >
                  <span class="checklist-item-bullet">•</span>
                  <input
                    v-model="dim.checklist[itemIdx]"
                    class="checklist-item-input"
                    placeholder="检查项描述"
                    :disabled="submitting"
                  />
                  <button
                    type="button"
                    class="btn-remove-item"
                    :disabled="submitting"
                    title="删除该检查项"
                    @click="removeChecklistItem(dim, itemIdx)"
                  >×</button>
                </div>
                <p v-if="dim.checklist.length === 0" class="checklist-empty">
                  暂无检查项
                </p>
              </div>
            </div>

            <button
              type="button"
              class="btn-add-dim"
              :disabled="submitting"
              @click="addDimension"
            >+ 新增维度</button>
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
                class="btn btn-secondary"
                :disabled="submitting"
                title="直接采用智能体生成的清单,不做修改"
                @click="handleAcceptAsIs"
              >
                <span v-if="submitting" class="btn-spinner" />
                {{ submitting ? '提交中...' : '直接采用' }}
              </button>
              <button
                class="btn btn-primary"
                :disabled="!canSubmitEdited"
                @click="handleSubmitEdited"
              >确认编辑</button>
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
  max-width: 640px;
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
  gap: var(--space-4);
}

.dialog-intro {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  margin: 0;
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

/* ---- 维度卡片 ---- */
.dimension-item {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  background: var(--color-surface-alt);
}

.dimension-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.dimension-id {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  width: 28px;
}

.dimension-name-input {
  flex: 1;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast);
}

.dimension-name-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.dimension-name-input:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.dimension-desc-input {
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

.dimension-desc-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.dimension-desc-input:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.btn-remove-dim {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-danger);
  background: transparent;
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-remove-dim:hover:not(:disabled) {
  background: var(--color-danger-light);
}

.btn-remove-dim:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ---- 检查项列表 ---- */
.checklist-items {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-left: var(--space-2);
}

.checklist-items-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.checklist-items-title {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.btn-add-item {
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: transparent;
  border: 1px dashed var(--color-primary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-add-item:hover:not(:disabled) {
  background: var(--color-primary-light);
}

.btn-add-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.checklist-item-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.checklist-item-bullet {
  flex-shrink: 0;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
}

.checklist-item-input {
  flex: 1;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  transition: border-color var(--transition-fast);
}

.checklist-item-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.checklist-item-input:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.btn-remove-item {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  padding: 0;
  font-size: 16px;
  line-height: 1;
  color: var(--color-text-muted);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-remove-item:hover:not(:disabled) {
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.btn-remove-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.checklist-empty {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  margin: 0;
  padding-left: var(--space-2);
  font-style: italic;
}

/* ---- 新增维度按钮 ---- */
.btn-add-dim {
  align-self: flex-start;
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: transparent;
  border: 1px dashed var(--color-primary);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-add-dim:hover:not(:disabled) {
  background: var(--color-primary-light);
}

.btn-add-dim:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ---- 底部按钮区 ---- */
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
  /* 轨道/高亮跟随文字色:主色按钮(深色主题下深字)正确 */
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
