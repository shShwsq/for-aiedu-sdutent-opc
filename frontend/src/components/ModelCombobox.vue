<script setup lang="ts">
/**
 * 模型选择组合框
 *
 * 兼具 <select> 的下拉外观与 <input> 的自由输入能力:
 * - 点击输入框或下拉箭头展开列表
 * - 直接输入任意模型 ID(含目录未收录的自定义模型)
 * - 输入时实时筛选下拉列表(按 value 或 label 模糊匹配)
 * - 键盘导航:↑/↓ 移动高亮,Enter 选中,Esc 关闭
 * - 点击外部自动关闭
 *
 * 下拉列表通过 Teleport 挂载到 body,用 @floating-ui/dom 计算定位,
 * 避免被弹窗(dialog-card overflow:hidden / dialog-body overflow:auto)裁剪,
 * 列表可正常覆盖在弹窗 footer 之上显示。
 *
 * 视觉与 .field select 保持一致,便于在表单中无缝替换。
 */
import { autoUpdate, computePosition, flip, offset, size } from '@floating-ui/dom'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

export interface ComboboxOption {
  /** 实际填入模型的值(模型 ID) */
  value: string
  /** 下拉项展示文本;不提供时展示 value */
  label?: string
}

const props = withDefaults(
  defineProps<{
    modelValue: string
    options: ComboboxOption[]
    placeholder?: string
    disabled?: boolean
  }>(),
  {
    placeholder: '',
    disabled: false,
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

const rootRef = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)
const listRef = ref<HTMLElement | null>(null)
const open = ref(false)
const highlightIndex = ref(-1)

// 输入框文本。与 modelValue 解耦:选中下拉项时同步,自由输入时仅作展示缓冲。
// 用 watch 同步外部 -> 输入框(如厂商切换清空、编辑态初始化)。
const query = ref(props.modelValue)
watch(
  () => props.modelValue,
  (v) => {
    query.value = v
  },
)

const filtered = computed<ComboboxOption[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return props.options
  return props.options.filter((o) => {
    const v = o.value.toLowerCase()
    const l = (o.label ?? '').toLowerCase()
    return v.includes(q) || l.includes(q)
  })
})

function openList(): void {
  if (props.disabled) return
  open.value = true
  // 默认高亮当前已选中项(若存在于列表),否则第一项
  const idx = filtered.value.findIndex((o) => o.value === query.value)
  highlightIndex.value = idx >= 0 ? idx : filtered.value.length > 0 ? 0 : -1
}

function closeList(): void {
  open.value = false
  highlightIndex.value = -1
}

function onInput(e: Event): void {
  const v = (e.target as HTMLInputElement).value
  query.value = v
  emit('update:modelValue', v)
  if (!open.value) open.value = true
  // 输入变化后重置高亮到首项(若当前值仍在结果中则高亮它)
  const idx = filtered.value.findIndex((o) => o.value === v)
  highlightIndex.value = idx >= 0 ? idx : filtered.value.length > 0 ? 0 : -1
}

function selectOption(opt: ComboboxOption): void {
  query.value = opt.value
  emit('update:modelValue', opt.value)
  closeList()
  nextTick(() => inputRef.value?.focus())
}

function onKeydown(e: KeyboardEvent): void {
  if (props.disabled) return
  switch (e.key) {
    case 'ArrowDown': {
      e.preventDefault()
      if (!open.value) {
        openList()
        return
      }
      if (filtered.value.length === 0) return
      highlightIndex.value = (highlightIndex.value + 1) % filtered.value.length
      scrollIntoView()
      break
    }
    case 'ArrowUp': {
      e.preventDefault()
      if (!open.value) {
        openList()
        return
      }
      if (filtered.value.length === 0) return
      highlightIndex.value =
        (highlightIndex.value - 1 + filtered.value.length) % filtered.value.length
      scrollIntoView()
      break
    }
    case 'Enter': {
      if (!open.value) return
      e.preventDefault()
      const opt = filtered.value[highlightIndex.value]
      if (opt) selectOption(opt)
      else closeList()
      break
    }
    case 'Escape': {
      if (open.value) {
        e.preventDefault()
        closeList()
      }
      break
    }
    case 'Tab': {
      if (open.value) closeList()
      break
    }
  }
}

function scrollIntoView(): void {
  nextTick(() => {
    const list = listRef.value
    const el = list?.querySelector<HTMLElement>(`[data-idx="${highlightIndex.value}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  })
}

// 点击外部关闭
function onDocumentClick(e: MouseEvent): void {
  const t = e.target as Node
  if (rootRef.value?.contains(t)) return
  if (listRef.value?.contains(t)) return
  closeList()
}

// ---- floating-ui 定位 ----
// 打开时建立 autoUpdate(自动监听滚动/resize/锚点位移),关闭时清理。
let cleanupFloating: (() => void) | null = null

function setupFloating(): void {
  const anchor = inputRef.value
  const floating = listRef.value
  if (!anchor || !floating) return

  // 先同步算一次,避免列表在(0,0)闪烁
  updatePosition(anchor, floating)

  cleanupFloating?.()
  cleanupFloating = autoUpdate(anchor, floating, () => updatePosition(anchor, floating))
}

function updatePosition(anchor: HTMLElement, floating: HTMLElement): void {
  computePosition(anchor, floating, {
    placement: 'bottom-start',
    // 4px 间距,避免列表紧贴输入框
    middleware: [
      offset(4),
      // 下方空间不足时翻转到上方
      flip({ padding: 8 }),
      // 限制列表宽度不超过锚点宽度(与 input 对齐),
      // 同时限制最大高度,超出则内部滚动
      size({
        apply({ rects, elements }) {
          Object.assign(elements.floating.style, {
            minWidth: `${rects.reference.width}px`,
            maxWidth: `${Math.max(rects.reference.width, 320)}px`,
          })
        },
        padding: 8,
      }),
    ],
  }).then(({ x, y }) => {
    Object.assign(floating.style, {
      left: `${x}px`,
      top: `${y}px`,
    })
  })
}

watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('mousedown', onDocumentClick)
    // 双重等待:nextTick(Vue 更新) + rAF(浏览器布局),确保 Teleport 目标已挂载
    nextTick(() => requestAnimationFrame(setupFloating))
  } else {
    document.removeEventListener('mousedown', onDocumentClick)
    cleanupFloating?.()
    cleanupFloating = null
  }
})

// filtered 变化会导致 ul ↔ p 切换,listRef 重新绑定,需重新建立定位
watch(filtered, () => {
  if (open.value) nextTick(() => requestAnimationFrame(setupFloating))
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocumentClick)
  cleanupFloating?.()
})
</script>

<template>
  <div ref="rootRef" class="combobox" :class="{ 'is-disabled': disabled }">
    <input
      ref="inputRef"
      v-model="query"
      type="text"
      class="combobox-input"
      :placeholder="placeholder"
      :disabled="disabled"
      autocomplete="off"
      @input="onInput"
      @focus="openList"
      @keydown="onKeydown"
    />
    <button
      type="button"
      class="combobox-toggle"
      tabindex="-1"
      :disabled="disabled"
      aria-label="展开模型列表"
      @click="open ? closeList() : openList()"
    >
      <svg
        class="combobox-arrow"
        :class="{ 'is-open': open }"
        viewBox="0 0 16 16"
        width="12"
        height="12"
        aria-hidden="true"
      >
        <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>

    <!-- 下拉列表 Teleport 到 body,由 floating-ui 定位,避免被弹窗裁剪 -->
    <Teleport to="body">
      <ul
        v-if="open && filtered.length > 0"
        ref="listRef"
        class="combobox-list"
        role="listbox"
      >
        <li
          v-for="(opt, idx) in filtered"
          :key="opt.value"
          :data-idx="idx"
          class="combobox-option"
          :class="{
            'is-highlighted': idx === highlightIndex,
            'is-selected': opt.value === modelValue,
          }"
          role="option"
          :aria-selected="opt.value === modelValue"
          @mousedown.prevent="selectOption(opt)"
          @mouseenter="highlightIndex = idx"
        >
          <span class="combobox-option-label">{{ opt.label ?? opt.value }}</span>
          <span v-if="opt.label && opt.label !== opt.value" class="combobox-option-value">{{ opt.value }}</span>
        </li>
      </ul>
      <p
        v-else-if="open && filtered.length === 0 && query.trim()"
        ref="listRef"
        class="combobox-empty"
      >
        无匹配模型,将使用输入的 ID
      </p>
    </Teleport>
  </div>
</template>

<style scoped>
.combobox {
  position: relative;
  width: 100%;
}

.combobox .combobox-input {
  width: 100%;
  height: 38px;
  padding: 0 32px 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.combobox .combobox-input::placeholder {
  color: var(--color-text-muted);
}

.combobox .combobox-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.combobox.is-disabled .combobox-input {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.combobox-toggle {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 0;
}

.combobox-toggle:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.combobox-arrow {
  transition: transform var(--transition-fast);
}

.combobox-arrow.is-open {
  transform: rotate(180deg);
}

.combobox-list {
  /* Teleport 到 body,由 floating-ui 设置 left/top;width 由 size middleware 控制 */
  position: fixed;
  top: 0;
  left: 0;
  /* 高于弹窗(1000)与 toast(2000) */
  z-index: 3000;
  margin: 0;
  /* 不留外边距:让高亮块紧贴列表边框 */
  padding: 0;
  list-style: none;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  max-height: 160px;
  overflow-y: auto;
}

.combobox-option {
  display: flex;
  flex-direction: column;
  gap: 2px;
  /* 文字保持左右内边距可读;高亮背景由 padding 外侧填满,紧贴列表边框 */
  padding: var(--space-2) var(--space-3);
  border-radius: 0;
  cursor: pointer;
  transition: background var(--transition-fast);
}

.combobox-option.is-highlighted {
  background: var(--color-surface-alt);
}

.combobox-option.is-selected {
  color: var(--color-primary);
  font-weight: var(--fw-medium);
}

.combobox-option-label {
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
}

.combobox-option-value {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-family: var(--font-mono);
}

.combobox-option.is-selected .combobox-option-value {
  color: var(--color-primary);
  opacity: 0.8;
}

.combobox-empty {
  /* 同 .combobox-list,Teleport 到 body 由 floating-ui 定位 */
  position: fixed;
  top: 0;
  left: 0;
  z-index: 3000;
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}
</style>
