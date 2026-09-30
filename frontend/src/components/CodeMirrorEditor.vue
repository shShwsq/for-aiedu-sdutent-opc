<script setup lang="ts">
/**
 * CodeMirror 6 Markdown 编辑器封装
 *
 * 替代旧的 textarea + 镜像元素测行高方案:
 * - 行号与视觉行严格对齐(折行续行不显示行号,CodeMirror 原生建模视觉行)
 * - 内置 Markdown 语法高亮(标题/列表/代码块/链接等)
 * - Tab 缩进 2 空格,Shift+Tab 反缩进(indentWithTab)
 * - 软换行(lineWrapping),长段落自动折行
 * - v-model 双向绑定(基于 EditorView.updateListener)
 *
 * 主题通过 EditorView.theme 注入设计令牌,与全局亮色风格对齐,
 * 不引入额外主题包,保持由 tokens 统一控制。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { EditorState, Compartment } from '@codemirror/state'
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  placeholder as cmPlaceholder,
} from '@codemirror/view'
import { defaultKeymap, historyKeymap, history, indentWithTab, undo, redo, undoDepth, redoDepth } from '@codemirror/commands'
import { markdown } from '@codemirror/lang-markdown'
import { HighlightStyle, syntaxHighlighting, defaultHighlightStyle, indentUnit } from '@codemirror/language'
import { tags as t } from '@lezer/highlight'

const props = withDefaults(
  defineProps<{
    modelValue: string
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
  /** 撤销栈状态变化(供父组件驱动撤回/恢复按钮的 disabled 态) */
  (e: 'historyChange', payload: { canUndo: boolean; canRedo: boolean }): void
}>()

const hostRef = ref<HTMLDivElement | null>(null)
let view: EditorView | null = null

/** 当前撤销栈状态(内部追踪 + 通过 historyChange 事件外抛) */
const canUndo = ref(false)
const canRedo = ref(false)

// 用 Compartment 包装可动态切换的配置(placeholder / editable)
const placeholderCompartment = new Compartment()
const editableCompartment = new Compartment()

/** Markdown 语法高亮样式(基于 tags,与设计令牌色对齐)
 * 不为标题设置 fontSize 变化:不同字号的行高不一致,会破坏行号与视觉行的对齐。
 * 标题仅通过 fontWeight + color 区分(与 VS Code 默认 Markdown 编辑行为一致),
 * 字号差异交给预览模式(marked 渲染)呈现。 */
const mdHighlightStyle = HighlightStyle.define([
  { tag: t.heading1, fontWeight: '700', color: 'var(--color-text)' },
  { tag: t.heading2, fontWeight: '700', color: 'var(--color-text)' },
  { tag: t.heading3, fontWeight: '600', color: 'var(--color-text)' },
  { tag: [t.heading4, t.heading5, t.heading6], fontWeight: '600', color: 'var(--color-text-secondary)' },
  { tag: t.strong, fontWeight: '700' },
  { tag: t.emphasis, fontStyle: 'italic' },
  { tag: t.strikethrough, textDecoration: 'line-through' },
  { tag: t.link, color: 'var(--color-primary)', textDecoration: 'underline' },
  { tag: t.url, color: 'var(--color-primary)' },
  { tag: t.monospace, fontFamily: 'var(--font-mono)', color: 'var(--color-text-secondary)' },
  { tag: t.quote, color: 'var(--color-text-secondary)', fontStyle: 'italic' },
  { tag: t.list, color: 'var(--color-text-secondary)' },
  { tag: t.meta, color: 'var(--color-text-muted)' },
  { tag: t.processingInstruction, color: 'var(--color-text-muted)' },
  { tag: t.contentSeparator, color: 'var(--color-border-strong)' },
])

/** 编辑器主题(基于设计令牌) */
const editorTheme = EditorView.theme({
  '&': {
    backgroundColor: 'transparent',
    color: 'var(--color-text)',
    height: '100%',
    fontSize: 'var(--fs-sm)',
    fontFamily: 'var(--font-mono)',
  },
  '&.cm-focused': {
    outline: 'none',
  },
  '.cm-scroller': {
    fontFamily: 'var(--font-mono)',
    fontSize: 'var(--fs-sm)',
    lineHeight: 'var(--lh-relaxed)',
    padding: 'var(--space-4) var(--space-5)',
  },
  '.cm-content': {
    caretColor: 'var(--color-primary)',
    padding: 0,
  },
  '.cm-line': {
    padding: '0 2px',
  },
  '.cm-gutters': {
    backgroundColor: 'var(--color-surface-alt)',
    color: 'var(--color-text-muted)',
    border: 'none',
    borderRight: '1px solid var(--color-border)',
    fontFamily: 'var(--font-mono)',
    fontSize: 'var(--fs-sm)',
  },
  '.cm-gutter': {
    minWidth: '52px',
    padding: '0 var(--space-2) 0 0',
    /* 不设上下 padding:.cm-scroller 已有 padding-top/bottom,
       gutter 作为 scroller 的 flex 子元素已被推下去,
       再加上下 padding 会导致行号比文字低一个 padding 量,造成错位。 */
  },
  '.cm-gutterElement': {
    padding: 0,
    /* 不设固定 line-height:CodeMirror 按每行实际高度动态设置 gutterElement 的 height,
       若此处覆盖 line-height 会导致行号数字垂直位置与内容行错位。 */
  },
  '.cm-activeLine': {
    backgroundColor: 'var(--color-surface-alt)',
  },
  '.cm-activeLineGutter': {
    backgroundColor: 'var(--color-surface-alt)',
    color: 'var(--color-text-secondary)',
  },
  '.cm-selectionBackground, .cm-content ::selection': {
    backgroundColor: 'var(--color-primary-light)',
  },
  '&.cm-focused .cm-selectionBackground': {
    backgroundColor: 'var(--color-primary-light)',
  },
  '.cm-cursor': {
    borderLeftColor: 'var(--color-primary)',
    borderLeftWidth: '2px',
  },
  // 焦点态:左侧主色条(与旧 .md-editor:focus 对齐)
  '&.cm-focused .cm-scroller': {
    boxShadow: 'inset 2px 0 0 var(--color-primary)',
  },
  // placeholder
  '.cm-placeholder': {
    color: 'var(--color-text-muted)',
    fontStyle: 'italic',
  },
  // 禁用态
  '&.cm-disabled .cm-content': {
    opacity: '0.7',
  },
})

/** 构建编辑器扩展集合 */
function buildExtensions() {
  return [
    history(),
    lineNumbers(),
    highlightActiveLine(),
    EditorView.lineWrapping,
    markdown({ codeLanguages: [] }),
    syntaxHighlighting(mdHighlightStyle),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    keymap.of([
      indentWithTab,
      ...defaultKeymap,
      ...historyKeymap,
    ]),
    EditorState.tabSize.of(2),
    indentUnit.of('  '),
    editableCompartment.of(EditorView.editable.of(!props.disabled)),
    placeholderCompartment.of(cmPlaceholder(props.placeholder)),
    editorTheme,
    EditorView.contentAttributes.of({ spellcheck: 'false' }),
    // 内容变更 → v-model;撤销栈深度变化 → historyChange
    EditorView.updateListener.of((update) => {
      if (update.docChanged) {
        emit('update:modelValue', update.state.doc.toString())
      }
      // 撤销/恢复本身也会触发 update(非 docChanged 场景),需在每次 update 重算深度
      const nextUndo = undoDepth(update.state) > 0
      const nextRedo = redoDepth(update.state) > 0
      if (nextUndo !== canUndo.value || nextRedo !== canRedo.value) {
        canUndo.value = nextUndo
        canRedo.value = nextRedo
        emit('historyChange', { canUndo: nextUndo, canRedo: nextRedo })
      }
    }),
  ]
}

onMounted(() => {
  if (!hostRef.value) return
  view = new EditorView({
    state: EditorState.create({
      doc: props.modelValue ?? '',
      extensions: buildExtensions(),
    }),
    parent: hostRef.value,
  })
})

onBeforeUnmount(() => {
  // 卸载时通知父组件重置按钮态(切预览 / 切文件场景)
  emit('historyChange', { canUndo: false, canRedo: false })
  view?.destroy()
  view = null
})

// 外部 modelValue 变化 → 同步到编辑器(仅当与当前 doc 不同时,避免光标跳动)
watch(
  () => props.modelValue,
  (newVal) => {
    if (!view) return
    const current = view.state.doc.toString()
    if (newVal !== current) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: newVal ?? '' },
      })
    }
  },
)

// placeholder 变化
watch(
  () => props.placeholder,
  (newVal) => {
    if (!view) return
    view.dispatch({
      effects: placeholderCompartment.reconfigure(cmPlaceholder(newVal)),
    })
  },
)

// disabled 变化
watch(
  () => props.disabled,
  (newVal) => {
    if (!view) return
    view.dispatch({
      effects: editableCompartment.reconfigure(EditorView.editable.of(!newVal)),
    })
  },
)

defineExpose({
  /** 聚焦编辑器 */
  focus: () => view?.focus(),
  /** 获取当前文档长度(字符数) */
  getLength: () => view?.state.doc.length ?? 0,
  /** 撤回一步(调用 CodeMirror undo 命令) */
  undo: () => {
    if (view) undo(view)
  },
  /** 恢复一步(调用 CodeMirror redo 命令) */
  redo: () => {
    if (view) redo(view)
  },
})
</script>

<template>
  <div
    ref="hostRef"
    class="cm-host"
    :class="{ disabled }"
  />
</template>

<style scoped>
.cm-host {
  height: 100%;
  width: 100%;
  overflow: hidden;
  background: var(--color-surface);
}

.cm-host.disabled {
  cursor: not-allowed;
}

/* :deep 穿透到 CodeMirror 内部 DOM */
.cm-host :deep(.cm-editor) {
  height: 100%;
}

.cm-host :deep(.cm-editor.cm-focused) {
  outline: none;
}

.cm-host :deep(.cm-scroller) {
  overflow: auto;
}
</style>
