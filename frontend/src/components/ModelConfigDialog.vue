<script setup lang="ts">
/**
 * 模型配置 新增/编辑 弹窗
 *
 * 复用 QuestionDialog 的视觉语言(mask + card + header/body/footer + Teleport)。
 * 通过 kind 区分 LLM / Embedding,条件渲染对应字段:
 * - LLM:        enable_thinking(仅当厂商 supportsThinking)
 * - Embedding:  维度提示(只读,由模型元信息推导)
 *
 * 草稿在 open=true 时按 initial 初始化;确定时 emit confirm,由父组件写回列表并持久化。
 * 编辑态下 api_key 始终从空串开始,留空表示"保留已存的 key"。
 */
import { computed, reactive, ref, watch } from 'vue'
import ModelCombobox, { type ComboboxOption } from '@/components/ModelCombobox.vue'
import BaseSelect from '@/components/BaseSelect.vue'
import type {
  EmbeddingConfigItem,
  EmbeddingProvider,
  LLMConfigItem,
  LLMProvider,
  ModelsCatalog,
} from '@/types/model_configs'

type Kind = 'llm' | 'embedding'

const props = defineProps<{
  open: boolean
  kind: Kind
  mode: 'add' | 'edit'
  /** 编辑时传入原配置(含 has_api_key);新增时传 null */
  initial:
    | (LLMConfigItem & { has_api_key: boolean })
    | (EmbeddingConfigItem & { has_api_key: boolean })
    | null
  catalog: ModelsCatalog | null
  /** 持久化进行中(禁用按钮 + 显示 spinner) */
  saving: boolean
}>()

const emit = defineEmits<{
  (e: 'confirm', payload: { kind: Kind; config: LLMConfigItem | EmbeddingConfigItem }): void
  (e: 'cancel'): void
}>()

// 草稿:含两类配置所有字段,按 kind 使用。Object.assign 复用同一个 reactive。
const draft = reactive<{
  id: string
  name: string
  provider: string
  api_key: string
  model: string
  base_url: string | null
  enable_thinking: boolean
  dimension: number
  /** 单次输出上限(null = 未设置,按 catalog/系统默认) */
  max_output_tokens: number | null
  has_api_key: boolean
}>({
  id: '',
  name: '',
  provider: '',
  api_key: '',
  model: '',
  base_url: null,
  enable_thinking: true,
  dimension: 1024,
  max_output_tokens: null,
  has_api_key: false,
})

// 输出上限输入框用字符串承载(空串 = 未设置),提交时转 number | null。
// 注意:输入框为 type="number",Vue 的 v-model 会自动把值转成 number
//(等价于隐式 .number 修饰符),因此运行时实际类型是 string | number
const maxOutputStr = ref<string | number>('')

/** open 变 true 时(重新)初始化草稿 */
watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) return
    const ini = props.initial
    if (ini) {
      draft.id = ini.id
      draft.name = ini.name
      draft.provider = ini.provider
      draft.api_key = '' // 编辑态始终从空开始,留空 = 保留
      draft.model = ini.model
      draft.base_url = ini.base_url
      draft.enable_thinking = (ini as LLMConfigItem).enable_thinking ?? true
      draft.dimension = (ini as EmbeddingConfigItem).dimension ?? 1024
      draft.max_output_tokens = (ini as LLMConfigItem).max_output_tokens ?? null
      draft.has_api_key = ini.has_api_key
    } else {
      // crypto.randomUUID() 仅在 Secure Context(HTTPS 或 localhost)可用。
      // Docker 部署默认走 http://服务器IP,是非 Secure Context,需降级生成 UUID,
      // 否则 draft.id 保持空串,落库后 JSONB 元素 id 为空,导致编辑/删除错乱。
      draft.id = crypto?.randomUUID?.()
        ?? 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
          const r = (Math.random() * 16) | 0
          return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
        })
      draft.name = ''
      draft.provider = ''
      draft.api_key = ''
      draft.model = ''
      draft.base_url = null
      draft.enable_thinking = true
      draft.dimension = 1024
      draft.max_output_tokens = null
      draft.has_api_key = false
    }
    maxOutputStr.value = draft.max_output_tokens != null ? String(draft.max_output_tokens) : ''
  },
  { immediate: true },
)

// ---- 厂商查询 ----
function getLlmProvider(id: string): LLMProvider | null {
  return props.catalog?.llmProviders.find((p) => p.id === id) ?? null
}
function getEmbProvider(id: string): EmbeddingProvider | null {
  return props.catalog?.embeddingProviders.find((p) => p.id === id) ?? null
}

const llmProvider = computed(() => (props.kind === 'llm' ? getLlmProvider(draft.provider) : null))

const thinkingSupported = computed(() => !!llmProvider.value?.supportsThinking)
const thinkingOnly = computed(() => {
  const p = llmProvider.value
  if (!p?.supportsThinking) return false
  const m = p.models.find((x) => x.id === draft.model)
  return (m?.thinking ?? p.fallbackThinking ?? 'none') === 'only'
})

const embDimensionHint = computed(() => {
  if (props.kind !== 'embedding') return ''
  const p = getEmbProvider(draft.provider)
  if (!p || !draft.model) return ''
  const m = p.models.find((x) => x.id === draft.model)
  const dim = m?.dimension ?? p.fallbackDimension ?? 1024
  const mm = m?.multimodal ?? p.fallbackMultimodal ?? false
  const dp = m?.dimensionsParam ?? p.fallbackDimensionsParam ?? false
  let text = `维度: ${dim}`
  if (mm) text += ' · 多模态'
  if (dp) text += ' · 通过 dimensions 参数指定'
  return text
})

/** 未显式设置时的输出上限默认值:模型级 > 厂商级 > 系统默认 16384 */
const maxOutputDefault = computed<number>(() => {
  const p = llmProvider.value
  if (!p) return 16384
  const m = p.models.find((x) => x.id === draft.model)
  return m?.outputLimit ?? p.fallbackOutputLimit ?? 16384
})

/** 统一取值:无论 v-model 回写的是 string 还是 number,都转成 trim 后的字符串 */
function maxOutputText(): string {
  return String(maxOutputStr.value ?? '').trim()
}

/** 输入框 → number | null(空串 = 未设置) */
function parseMaxOutput(): number | null {
  const s = maxOutputText()
  if (!s) return null
  const n = Number(s)
  return Number.isInteger(n) && n > 0 ? n : null
}

// 当前厂商的可选模型,供 ModelCombobox 渲染。
// LLM 仅展示 id;Embedding 展示 name(主) + id(副),与原 select 视觉一致。
const modelOptions = computed<ComboboxOption[]>(() => {
  if (props.kind === 'llm') {
    const p = getLlmProvider(draft.provider)
    return (p?.models ?? []).map((m) => ({ value: m.id }))
  }
  const p = getEmbProvider(draft.provider)
  return (p?.models ?? []).map((m) => ({ value: m.id, label: m.name }))
})

// ---- 厂商切换:自动填 baseUrl + 选默认模型 ----
/** 厂商选项:自定义 + 当前 kind 对应的厂商列表 */
const providerOptions = computed(() => {
  const providers =
    props.kind === 'llm' ? props.catalog?.llmProviders : props.catalog?.embeddingProviders
  return [
    { value: '', label: '自定义' },
    ...(providers ?? []).map((p) => ({ value: p.id, label: p.name })),
  ]
})

function onProviderChange(): void {
  draft.model = ''
  if (props.kind === 'llm') {
    const p = getLlmProvider(draft.provider)
    draft.base_url = p?.baseUrl ?? null
    if (p && p.models.length > 0) draft.model = p.models[0].id
  } else {
    const p = getEmbProvider(draft.provider)
    draft.base_url = p?.baseUrl ?? null
    if (p && p.models.length > 0) draft.model = p.models[0].id
  }
}

const apiKeyPlaceholder = computed(() =>
  draft.has_api_key ? '已配置,输入新值以替换(留空则保留)' : 'sk-...',
)

const title = computed(() => {
  const k = props.kind === 'llm' ? 'LLM' : 'Embedding'
  return props.mode === 'add' ? `添加 ${k} 配置` : `编辑 ${k} 配置`
})

const validationError = computed<string | null>(() => {
  if (!draft.provider) return '请选择厂商'
  if (!draft.model) return '请输入或选择模型'
  if (!draft.has_api_key && !draft.api_key) return '请填写 API Key'
  if (maxOutputText() !== '' && parseMaxOutput() === null) {
    return '输出上限需为正整数'
  }
  return null
})

const canSubmit = computed(() => !validationError.value && !props.saving)

function handleConfirm(): void {
  if (!canSubmit.value) return
  if (props.kind === 'llm') {
    const cfg: LLMConfigItem = {
      id: draft.id,
      name: draft.name,
      provider: draft.provider,
      api_key: draft.api_key,
      model: draft.model,
      enable_thinking: draft.enable_thinking,
      base_url: draft.base_url,
      max_output_tokens: parseMaxOutput(),
    }
    emit('confirm', { kind: 'llm', config: cfg })
  } else {
    const cfg: EmbeddingConfigItem = {
      id: draft.id,
      name: draft.name,
      provider: draft.provider,
      api_key: draft.api_key,
      model: draft.model,
      base_url: draft.base_url,
      dimension: draft.dimension,
    }
    emit('confirm', { kind: 'embedding', config: cfg })
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="emit('cancel')">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>{{ title }}</h3>
            <button
              class="dialog-close"
              :disabled="saving"
              aria-label="关闭"
              @click="emit('cancel')"
            >×</button>
          </header>

          <div class="dialog-body">
            <div class="field-row">
              <div class="field">
                <label>名称(可选)</label>
                <input
                  v-model.trim="draft.name"
                  type="text"
                  placeholder="如:DeepSeek 日常"
                  :disabled="saving"
                />
              </div>
              <div class="field">
                <label>厂商</label>
                <BaseSelect
                  v-model="draft.provider"
                  :options="providerOptions"
                  :disabled="saving"
                  class="field-select"
                  @change="onProviderChange"
                />
              </div>
            </div>

            <div class="field">
              <label>API Key</label>
              <input
                v-model="draft.api_key"
                type="password"
                :placeholder="apiKeyPlaceholder"
                :disabled="saving"
              />
            </div>

            <div class="field-row">
              <div class="field">
                <label>模型</label>
                <ModelCombobox
                  v-model="draft.model"
                  :options="modelOptions"
                  placeholder="选择或输入模型 ID"
                  :disabled="saving || !draft.provider"
                />
                <p v-if="kind === 'embedding' && embDimensionHint" class="field-hint">
                  {{ embDimensionHint }}
                </p>
              </div>
              <div class="field">
                <label>Base URL(可选)</label>
                <input
                  v-model.trim="draft.base_url"
                  type="text"
                  placeholder="选厂商后自动填充"
                  :disabled="saving"
                />
              </div>
            </div>

            <div v-if="kind === 'llm'" class="field">
              <label>单次输出上限(token,可选)</label>
              <input
                v-model.trim="maxOutputStr"
                type="number"
                min="1"
                step="1"
                :placeholder="`默认 ${maxOutputDefault}`"
                :disabled="saving"
              />
              <p class="field-hint">
                模型单次回复的 max_tokens 钳制值,过小会导致长输出被截断;留空用默认上限 {{ maxOutputDefault }}
              </p>
            </div>

            <div v-if="kind === 'llm' && thinkingSupported" class="field field-checkbox">
              <label>
                <input
                  v-model="draft.enable_thinking"
                  type="checkbox"
                  :disabled="saving || thinkingOnly"
                />
                <span>启用深度思考</span>
              </label>
              <p v-if="thinkingOnly" class="field-hint">该模型为仅思考模式,无法关闭</p>
            </div>
          </div>

          <footer class="dialog-footer">
            <span v-if="validationError" class="validation-error">{{ validationError }}</span>
            <div class="footer-actions">
              <button class="btn btn-secondary" :disabled="saving" @click="emit('cancel')">
                取消
              </button>
              <button class="btn btn-primary" :disabled="!canSubmit" @click="handleConfirm">
                <span v-if="saving" class="btn-spinner" />
                {{ saving ? '保存中...' : (mode === 'add' ? '添加' : '保存') }}
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
  max-width: 560px;
  max-height: 95vh;
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

/* ---- 表单字段 ---- */
.field-row {
  display: flex;
  gap: var(--space-4);
}

.field-row .field {
  flex: 1;
}

.field {
  margin-bottom: var(--space-3);
}

.field label {
  display: block;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-1);
  color: var(--color-text-secondary);
}

.field input {
  width: 100%;
  height: 38px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
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

/* BaseSelect 在表单字段内:撑满宽度 + 触发器最小高度对齐 input(38px) */
.field :deep(.base-select) {
  width: 100%;
}

.field :deep(.base-select-trigger) {
  min-height: 38px;
}

.field-checkbox label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-weight: var(--fw-normal);
  cursor: pointer;
}

.field-checkbox input {
  width: auto;
  height: auto;
  margin: 0;
}

.field-hint {
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
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
