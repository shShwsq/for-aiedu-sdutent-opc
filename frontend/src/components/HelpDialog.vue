<script setup lang="ts">
/**
 * 帮助文档弹窗
 *
 * 点击顶栏问号按钮打开,展示 frontend/src/data/help.md 渲染后的内容。
 * 所有路由下行为一致:统一查看完整帮助文档,而非路由特定的引导片段。
 *
 * 渲染:marked 解析 Markdown → DOMPurify 清理 → v-html 注入。
 * 样式:用 :deep() 选择器覆盖 marked 输出的 h1/h2/p/ul/li 等元素,套用项目 tokens。
 */
import { computed, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

// 以 raw 字符串形式导入 help.md(vite ?raw 后缀,构建期 inline)
import helpMarkdown from '@/data/help.md?raw'
import { ensureFeaturesLoaded, practiceEnabled } from '@/composables/useFeatures'

/**
 * 练习专属章节的包裹注释标记(help.md 中成对出现,练习功能关闭时整段移除)。
 * 在 marked 解析前裁剪,注释不会进入渲染结果。
 */
const PRACTICE_SECTION_RE = /<!--\s*practice-section-start\s*-->[\s\S]*?<!--\s*practice-section-end\s*-->/g

/** 按练习功能开关裁剪后的 Markdown(关闭部署移除全部练习专属章节) */
const effectiveMarkdown = computed(() =>
  practiceEnabled.value ? helpMarkdown : helpMarkdown.replace(PRACTICE_SECTION_RE, ''),
)

const props = defineProps<{
  /** 是否显示弹窗 */
  open: boolean
}>()

const emit = defineEmits<{
  /** 关闭弹窗(用户主动取消 / 点击遮罩 / ESC) */
  (e: 'close'): void
}>()

/** 缓存渲染后的 HTML(避免每次 open 都重新解析) */
const renderedHtml = ref('')

// 内容或练习开关变化时重新渲染(开关异步拉取,变化后自动重渲染)
watch(
  () => effectiveMarkdown.value,
  (md) => {
    // marked.parse 在 async:false 下同步返回 string
    const raw = marked.parse(md, { async: false }) as string
    renderedHtml.value = DOMPurify.sanitize(raw)
  },
  { immediate: true },
)

/** 弹窗标题(从 Markdown 第一行 h1 提取,作为 header 显示) */
const title = computed(() => {
  const match = effectiveMarkdown.value.match(/^#\s+(.+)$/m)
  return match?.[1] ?? '帮助文档'
})

/** 监听 ESC 键关闭 */
function handleKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && props.open) {
    e.preventDefault()
    emit('close')
  }
}

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      // 帮助文案按练习功能开关区分,打开前先确保开关已拉取(避免短暂显示默认开启版)
      void ensureFeaturesLoaded()
      window.addEventListener('keydown', handleKeydown)
      document.body.style.overflow = 'hidden'
    } else {
      window.removeEventListener('keydown', handleKeydown)
      document.body.style.overflow = ''
    }
  },
)

function handleClose(): void {
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div
        v-if="open"
        class="dialog-mask"
        @click.self="handleClose"
      >
        <div
          class="dialog-card"
          role="dialog"
          aria-modal="true"
          :aria-label="title"
        >
          <header class="dialog-header">
            <h3>{{ title }}</h3>
            <button
              class="dialog-close"
              aria-label="关闭"
              @click="handleClose"
            >×</button>
          </header>

          <div class="dialog-body">
            <div class="markdown-body" v-html="renderedHtml" />
          </div>

          <footer class="dialog-footer">
            <button class="btn btn-primary" type="button" @click="handleClose">
              知道了
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
  padding: var(--space-4);
}

.dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl, 0 20px 40px rgba(0, 0, 0, 0.15));
  width: 100%;
  max-width: 720px;
  max-height: 85vh;
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

.dialog-close:hover {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.dialog-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5) var(--space-6);
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.btn {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

/* ---- Markdown 渲染样式(覆盖 marked 输出元素) ---- */
.markdown-body {
  color: var(--color-text);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
}

.markdown-body :deep(h1) {
  display: none; /* 标题已由 dialog-header 显示,正文不重复 */
}

.markdown-body :deep(h2) {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  margin: var(--space-5) 0 var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text);
}

.markdown-body :deep(h3) {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  margin: var(--space-4) 0 var(--space-2);
  color: var(--color-text);
}

.markdown-body :deep(h4) {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  margin: var(--space-3) 0 var(--space-2);
  color: var(--color-text);
}

.markdown-body :deep(p) {
  margin: 0 0 var(--space-3);
  color: var(--color-text-secondary);
}

.markdown-body :deep(ul),
.markdown_body :deep(ol) {
  margin: 0 0 var(--space-3);
  padding-left: var(--space-5);
  color: var(--color-text-secondary);
}

.markdown-body :deep(li) {
  margin-bottom: var(--space-1);
}

.markdown-body :deep(li > ul),
.markdown-body :deep(li > ol) {
  margin-top: var(--space-1);
  margin-bottom: 0;
}

.markdown-body :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.9em;
  background: var(--color-surface-alt);
  color: var(--color-primary);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
}

.markdown-body :deep(pre) {
  background: var(--color-surface-alt);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  overflow-x: auto;
  margin: 0 0 var(--space-3);
}

.markdown-body :deep(pre code) {
  background: transparent;
  color: var(--color-text);
  padding: 0;
}

.markdown-body :deep(blockquote) {
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-4);
  border-left: 3px solid var(--color-primary-border);
  background: var(--color-primary-light);
  color: var(--color-text-secondary);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.markdown-body :deep(blockquote p) {
  margin: 0;
  color: var(--color-text-secondary);
}

.markdown-body :deep(a) {
  color: var(--color-primary);
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-border);
  transition: all var(--transition-fast);
}

.markdown-body :deep(a:hover) {
  color: var(--color-primary-hover);
  border-bottom-style: solid;
}

.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: var(--space-5) 0;
}

.markdown-body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 var(--space-3);
  font-size: var(--fs-sm);
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--color-border);
  padding: var(--space-2) var(--space-3);
  text-align: left;
}

.markdown-body :deep(th) {
  background: var(--color-surface-alt);
  font-weight: var(--fw-semibold);
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
