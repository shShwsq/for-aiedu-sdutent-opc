<script setup lang="ts">
/**
 * 知识点看板页(自适应练习的「知识点掌握全景」视图)
 *
 * - 五栏看板:薄弱 / 待复习 / 已巩固 / 学习中 / 未开始,
 *   分栏状态由后端派生(与组卷的薄弱判定同一套常量),前端只渲染
 * - 每张卡片 = 一个知识点:SM-2 记忆状态 + 作答统计 + 题库题数
 * - 卡片「专项练习」→ 跳 /practice?topic=<key>,由练习页接管组卷
 *   (练习逻辑全部留在 PracticeView,本页不发组卷请求)
 *
 * 原「自适应练习」页的薄弱知识点板块已移到本页的薄弱栏。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { listKnowledgePoints } from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import type { BoardStatus, KnowledgePointCard } from '@/types/practice'

const router = useRouter()

// ============================================================
// 历史任务侧栏(与首页/其他视图一致的折叠模式)
// ============================================================
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ============================================================
// 看板数据(后端已按分栏优先级排好序,前端按 status 分组保持顺序)
// ============================================================
const cards = ref<KnowledgePointCard[]>([])
const loading = ref(true)
const loadError = ref('')

async function loadBoard(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    cards.value = await listKnowledgePoints()
  } catch (err) {
    loadError.value = extractErrorMessage(err)
    cards.value = []
  } finally {
    loading.value = false
  }
}

/** 看板五栏定义(标题 + 分栏口径说明;顺序即展示顺序) */
const BOARD_COLUMNS: { status: BoardStatus; title: string; hint: string }[] = [
  { status: 'weak', title: '薄弱', hint: '错误率 > 40% 且作答 ≥ 3 次' },
  { status: 'due', title: '待复习', hint: '记忆曲线已到期' },
  { status: 'mastered', title: '已巩固', hint: '连续答对且正确率 ≥ 75%' },
  { status: 'learning', title: '学习中', hint: '有作答记录,记忆形成中' },
  { status: 'fresh', title: '未开始', hint: '有题目但从未作答' },
]

const columns = computed(() =>
  BOARD_COLUMNS.map((col) => ({
    ...col,
    cards: cards.value.filter((c) => c.board_status === col.status),
  })),
)

/** 卡片总数概览(页头统计行) */
const totalCount = computed(() => cards.value.length)

// ============================================================
// 交互
// ============================================================
/** 专项练习:带 topic 参数跳练习页,由其自动发起该知识点的组卷 */
function startFocus(c: KnowledgePointCard): void {
  router.push({ name: 'practice', query: { topic: c.knowledge_key } })
}

// ============================================================
// 展示辅助
// ============================================================
function formatPercent(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(0)}%`
}

/** 到期时间:已过期 N 天 / N 天后到期 */
function formatDue(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const diffDays = Math.ceil((d.getTime() - Date.now()) / 86400000)
  if (diffDays <= 0) return `已过期 ${-diffDays} 天`
  return `${diffDays} 天后到期`
}

onMounted(() => {
  loadBoard()
})
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
          :collapsed="workspaceCollapsed"
          expand-title="展开历史任务"
          collapse-title="折叠历史任务"
          @toggle="toggleWorkspace"
        />
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <header class="page-head">
          <div class="page-head-row">
            <h1>知识点看板</h1>
            <div class="page-head-actions">
              <button class="board-back-btn" title="去自适应练习" @click="router.push({ name: 'practice' })">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M5 12h14" />
                  <path d="m12 5 7 7-7 7" />
                </svg>
                去练习
              </button>
            </div>
          </div>
          <p>共 {{ totalCount }} 个知识点 · 按记忆状态分栏 · 点卡片可发起该知识点的专项练习</p>
        </header>

        <div v-if="loading" class="placeholder"><span class="status-spinner" /> 加载中...</div>
        <div v-else-if="loadError" class="placeholder error-text">
          加载失败: {{ loadError }}
          <button class="btn-link" @click="loadBoard">重试</button>
        </div>
        <p v-else-if="totalCount === 0" class="panel-empty">
          暂无知识点 — 到已完成审计任务的详情页生成并确认练习题后,知识点卡片会出现在这里
        </p>

        <!-- 五栏看板:薄弱 / 待复习 / 已巩固 / 学习中 / 未开始 -->
        <div v-else class="board">
          <section
            v-for="col in columns"
            :key="col.status"
            :class="['board-col', `board-col-${col.status}`]"
            :aria-label="`${col.title}知识点`"
          >
            <header class="col-head">
              <span :class="['col-title', `col-title-${col.status}`]">{{ col.title }}</span>
              <span class="col-count">{{ col.cards.length }}</span>
            </header>
            <p class="col-hint">{{ col.hint }}</p>

            <p v-if="col.cards.length === 0" class="col-empty">暂无</p>
            <div v-else class="col-cards">
              <article
                v-for="c in col.cards"
                :key="c.knowledge_key"
                :class="['kp-card', { 'kp-card-weak': col.status === 'weak' }]"
              >
                <div class="kp-head">
                  <span class="kp-name" :title="c.knowledge_name">{{ c.knowledge_name }}</span>
                  <span class="kp-key">{{ c.knowledge_key }}</span>
                </div>
                <div v-if="c.languages.length > 0" class="kp-tags">
                  <span v-for="lang in c.languages" :key="lang" class="tag tag-lang">{{ lang }}</span>
                </div>
                <div class="kp-stats">
                  <span v-if="c.attempts > 0">
                    正确率 {{ formatPercent(c.accuracy) }} · {{ c.attempts }} 次作答
                  </span>
                  <span v-else class="kp-muted">尚未作答</span>
                  <span v-if="col.status === 'due'" class="kp-due">{{ formatDue(c.due_at) }}</span>
                </div>
                <div class="kp-foot">
                  <span class="kp-count">{{ c.question_count }} 道题</span>
                  <button
                    class="btn-secondary btn-small"
                    :disabled="c.question_count === 0"
                    :title="c.question_count === 0 ? '该知识点暂无入库题目' : `只练习「${c.knowledge_name}」的题目`"
                    @click="startFocus(c)"
                  >专项练习</button>
                </div>
              </article>
            </div>
          </section>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  /* 手机地址栏伸缩兜底 */
  height: 100dvh;
  overflow: hidden;
  background: var(--color-bg);
}

.page-body {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-height: 0;
  overflow: hidden;
}

/* 看板五列需要更宽的画布,比练习页(860px)放宽 */
.main {
  flex: 1;
  min-height: 0;
  min-width: 0;
  max-width: 1280px;
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-8) var(--space-6);
}

.page-head {
  margin-bottom: var(--space-6);
}

.page-head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.page-head h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.page-head-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.board-back-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.board-back-btn:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.page-head p {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.error-text {
  color: var(--color-danger);
}

.btn-link {
  padding: 0;
  font-size: var(--fs-sm);
  color: var(--color-primary);
  background: transparent;
  border: none;
  cursor: pointer;
}

.panel-empty {
  margin: 0;
  padding: var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
}

/* ============ 看板布局:五栏并排,窄屏堆叠 ============ */
.board {
  display: grid;
  grid-template-columns: repeat(5, minmax(200px, 1fr));
  gap: var(--space-4);
  align-items: start;
}

.board-col {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
}

.col-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.col-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

/* 栏标题按语义着色:薄弱红 / 待复习主色 / 已巩固绿 / 学习中蓝 / 未开始灰 */
.col-title-weak { color: var(--color-danger); }
.col-title-due { color: var(--color-primary); }
.col-title-mastered { color: var(--color-success); }
.col-title-learning { color: var(--color-text); }
.col-title-fresh { color: var(--color-text-muted); }

.col-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  padding: 0 var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 999px;
}

.col-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.col-empty {
  margin: var(--space-2) 0 0;
  padding: var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  text-align: center;
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
}

.col-cards {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* ============ 知识点卡片 ============ */
.kp-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

/* 薄弱栏卡片加红色描边突出 */
.kp-card-weak {
  border-color: var(--color-danger);
}

.kp-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.kp-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.kp-key {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  word-break: break-all;
}

.kp-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

.tag {
  display: inline-flex;
  align-items: center;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  border-radius: 999px;
}

.tag-lang {
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.kp-stats {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.kp-muted {
  color: var(--color-text-muted);
}

.kp-due {
  color: var(--color-primary);
}

.kp-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.kp-count {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.btn-secondary {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-secondary:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* spinner */
.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: status-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes status-spin {
  to { transform: rotate(360deg); }
}

/* ---- 响应式:中屏 3 列,窄屏单列堆叠 ---- */
@media (max-width: 1100px) {
  .board {
    grid-template-columns: repeat(2, minmax(200px, 1fr));
  }
}

@media (max-width: 640px) {
  .main {
    padding: var(--space-4) var(--space-3) var(--space-6);
  }

  .board {
    grid-template-columns: 1fr;
  }

  .page-head-row {
    flex-wrap: wrap;
  }
}
</style>
