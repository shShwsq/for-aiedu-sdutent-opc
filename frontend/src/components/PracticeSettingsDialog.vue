<script setup lang="ts">
/**
 * 练习设置弹窗
 *
 * 复用 PasswordDialog 的视觉语言(mask + card + header/body/footer)。
 * 五项设置(均为全局生效,切换/选中即保存):
 * - 自动生成练习题开关(产出的候选题仍需在任务详情页预览确认才入库)
 * - 学习主题:出题提示词按主题切换出题视角(网络安全/架构设计/通用代码能力)
 * - 出题前恢复工作区:沙箱已清理时重新 clone 仓库,供出题时查阅源码
 * - 出题思考模式:覆盖出题模型的思考开关(跟随配置/强制开/强制关)
 * - 默认出题模型:用户级默认(任务级配置优先,未设置则回退 env 默认);
 *   可开「始终用默认出题模型」忽略任务级配置
 *
 * emit 由父组件调 API 持久化并 toast,父组件更新 props 后弹窗内状态同步。
 */
import { computed, ref, watch } from 'vue'

import type { LLMConfigItemOut } from '@/types/model_configs'
import type { LearningTopic, PracticeThinkingMode } from '@/types/memory'

const props = defineProps<{
  /** 是否显示 */
  open: boolean
  /** 自动生成练习题当前开关状态 */
  autoGenerate: boolean
  /** 当前学习主题 */
  learningTopic: LearningTopic
  /** 出题前恢复工作区开关状态 */
  restoreWorkspace: boolean
  /** 出题思考模式(follow=跟随模型配置/on=强制开/off=强制关) */
  thinkingMode: PracticeThinkingMode
  /** 默认出题模型配置 id(空串=跟随系统默认) */
  defaultModelId: string
  /** 始终用默认出题模型(忽略任务自带模型配置) */
  forceDefaultLlm: boolean
  /** 用户已保存的 LLM 配置列表(下拉选项来源) */
  llmConfigs: LLMConfigItemOut[]
  /** 保存中状态(禁用交互与关闭) */
  loading: boolean
  /** 清空中状态(同 loading,禁用交互与关闭) */
  clearing: boolean
  /** 错误信息(父组件 API 失败时传入) */
  error?: string
}>()

const emit = defineEmits<{
  (e: 'toggle'): void
  (e: 'topic', topic: LearningTopic): void
  (e: 'toggle-restore'): void
  (e: 'thinking', mode: PracticeThinkingMode): void
  (e: 'model', configId: string): void
  (e: 'toggle-force-default'): void
  (e: 'clear-records'): void
  (e: 'clear-all'): void
  (e: 'cancel'): void
}>()

/** 保存或清空中:统一禁用全部交互 */
const busy = computed(() => props.loading || props.clearing)

// ---- 危险操作:展开的确认态,重新打开弹窗时重置 ----
/** 当前展开的确认:none / 清空练习记录 / 清空全部数据 */
const confirmMode = ref<'none' | 'records' | 'all'>('none')
/** 清空全部数据的输入确认(输入「清空」后方可执行) */
const confirmText = ref('')
/** 当前打开的说明气泡(点问号展开,再点或点别处收起) */
const helpOpen = ref<'none' | 'records' | 'all'>('none')

function toggleHelp(which: 'records' | 'all'): void {
  helpOpen.value = helpOpen.value === which ? 'none' : which
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      confirmMode.value = 'none'
      confirmText.value = ''
      helpOpen.value = 'none'
    }
  },
)

/** 主题选项(与后端 LEARNING_TOPICS 对齐) */
const TOPIC_OPTIONS: Array<{ value: LearningTopic; label: string; desc: string }> = [
  { value: 'security', label: '网络安全', desc: '漏洞识别、成因判断、修复方式' },
  { value: 'architecture', label: '架构设计', desc: '模块边界、设计模式、选型权衡' },
  { value: 'coding', label: '通用代码能力', desc: 'bug 识别、代码坏味道、最佳实践' },
]

/** 思考模式选项(与后端 THINKING_MODES 对齐) */
const THINKING_OPTIONS: Array<{ value: PracticeThinkingMode; label: string; desc: string }> = [
  { value: 'follow', label: '跟随模型配置', desc: '使用出题模型配置自身的思考开关(默认)' },
  { value: 'on', label: '强制开启', desc: '出题更慢,题目质量可能更高' },
  { value: 'off', label: '强制关闭', desc: '出题更快;模型思考模式下工具调用异常导致出不出题时可尝试' },
]

function handleToggle(loading: boolean): void {
  if (loading) return
  emit('toggle')
}

function handleTopic(loading: boolean, topic: LearningTopic, current: LearningTopic): void {
  if (loading || topic === current) return
  emit('topic', topic)
}

function handleToggleRestore(loading: boolean): void {
  if (loading) return
  emit('toggle-restore')
}

function handleThinking(
  loading: boolean,
  mode: PracticeThinkingMode,
  current: PracticeThinkingMode,
): void {
  if (loading || mode === current) return
  emit('thinking', mode)
}

function handleModelChange(
  loading: boolean,
  event: Event,
  current: string,
): void {
  const value = (event.target as HTMLSelectElement).value
  if (loading || value === current) return
  emit('model', value)
}

function handleToggleForceDefault(loading: boolean): void {
  if (loading) return
  emit('toggle-force-default')
}

function handleCancel(loading: boolean): void {
  if (loading) return // 保存中不允许关闭
  emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleCancel(busy)">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>练习设置</h3>
            <button
              class="dialog-close"
              :disabled="busy"
              aria-label="关闭"
              @click="handleCancel(busy)"
            >×</button>
          </header>

          <div class="dialog-body" @click="helpOpen = 'none'">
            <div class="setting-row" @click="handleToggle(busy)">
              <div class="setting-info">
                <span class="setting-title">自动生成练习题</span>
                <span class="setting-desc">
                  审计任务完成后自动生成练习题候选题(全局生效,仍需预览确认才入库)
                </span>
              </div>
              <button
                type="button"
                role="switch"
                :aria-checked="autoGenerate"
                :class="['switch', { 'switch-on': autoGenerate }]"
                :disabled="busy"
                @click.stop="handleToggle(busy)"
              >
                <span class="switch-thumb" />
              </button>
            </div>

            <!-- 学习主题:出题提示词按主题切换出题视角,选中即保存 -->
            <div class="setting-block">
              <span class="setting-title">学习主题</span>
              <span class="setting-desc">出题时按主题切换出题视角,生成贴合当前学习目标的题目</span>
              <div class="topic-list" role="radiogroup" aria-label="学习主题">
                <button
                  v-for="opt in TOPIC_OPTIONS"
                  :key="opt.value"
                  type="button"
                  role="radio"
                  :aria-checked="learningTopic === opt.value"
                  :class="['topic-option', { 'topic-active': learningTopic === opt.value }]"
                  :disabled="busy"
                  @click="handleTopic(busy, opt.value, learningTopic)"
                >
                  <span class="topic-radio">
                    <span v-if="learningTopic === opt.value" class="topic-radio-dot" />
                  </span>
                  <span class="topic-text">
                    <span class="topic-label">{{ opt.label }}</span>
                    <span class="topic-desc">{{ opt.desc }}</span>
                  </span>
                </button>
              </div>
            </div>

            <div class="setting-row" @click="handleToggleRestore(busy)">
              <div class="setting-info">
                <span class="setting-title">出题前恢复工作区</span>
                <span class="setting-desc">
                  出题时沙箱已清理(任务完成超过 1 小时)则重新克隆仓库,
                  让出题过程能查阅真实源码提高题目质量(会消耗克隆时间)
                </span>
              </div>
              <button
                type="button"
                role="switch"
                :aria-checked="restoreWorkspace"
                :class="['switch', { 'switch-on': restoreWorkspace }]"
                :disabled="busy"
                @click.stop="handleToggleRestore(busy)"
              >
                <span class="switch-thumb" />
              </button>
            </div>

            <!-- 出题思考模式:覆盖出题模型的思考开关,选中即保存 -->
            <div class="setting-block">
              <span class="setting-title">出题思考模式</span>
              <span class="setting-desc">
                是否让出题模型开启思考(慢想)模式;开启后出题更慢但题目质量可能更高,
                关闭则生成更快——若某模型思考模式下反复读代码却出不出题,可强制关闭
              </span>
              <div class="topic-list" role="radiogroup" aria-label="出题思考模式">
                <button
                  v-for="opt in THINKING_OPTIONS"
                  :key="opt.value"
                  type="button"
                  role="radio"
                  :aria-checked="thinkingMode === opt.value"
                  :class="['topic-option', { 'topic-active': thinkingMode === opt.value }]"
                  :disabled="busy"
                  @click="handleThinking(busy, opt.value, thinkingMode)"
                >
                  <span class="topic-radio">
                    <span v-if="thinkingMode === opt.value" class="topic-radio-dot" />
                  </span>
                  <span class="topic-text">
                    <span class="topic-label">{{ opt.label }}</span>
                    <span class="topic-desc">{{ opt.desc }}</span>
                  </span>
                </button>
              </div>
            </div>

            <!-- 默认出题模型:用户级默认,任务级配置优先,选中即保存 -->
            <div class="setting-block">
              <span class="setting-title">默认出题模型</span>
              <span class="setting-desc">
                生成练习题时默认使用的模型(手动出题与自动出题均生效);
                任务自带模型配置时优先用任务配置,选「跟随系统默认」则用环境配置
              </span>
              <select
                class="model-select"
                aria-label="默认出题模型"
                :value="defaultModelId"
                :disabled="busy"
                @change="handleModelChange(busy, $event, defaultModelId)"
              >
                <option value="">跟随系统默认</option>
                <option v-for="cfg in llmConfigs" :key="cfg.id" :value="cfg.id">
                  {{ cfg.name }}({{ cfg.provider }} / {{ cfg.model }})
                </option>
              </select>
              <span v-if="!llmConfigs.length" class="model-hint">
                暂无已保存的模型配置,可先到「模型设置」中添加
              </span>

              <!-- 始终用默认出题模型:忽略任务自带模型配置,全局统一用上面的默认模型 -->
              <div class="force-default-row" @click="handleToggleForceDefault(busy)">
                <div class="setting-info">
                  <span class="setting-title force-title">始终用默认出题模型</span>
                  <span class="setting-desc">
                    忽略任务自带的模型配置,所有任务出题统一用上面的默认模型
                  </span>
                </div>
                <button
                  type="button"
                  role="switch"
                  :aria-checked="forceDefaultLlm"
                  :class="['switch', { 'switch-on': forceDefaultLlm }]"
                  :disabled="busy"
                  @click.stop="handleToggleForceDefault(busy)"
                >
                  <span class="switch-thumb" />
                </button>
              </div>
            </div>

            <!-- 危险操作:数据清空不可逆,均需二次确认 -->
            <div class="danger-block">
              <span class="setting-title danger-title">危险操作</span>

              <!-- 清空练习记录:进度归零,保留题库;说明由问号气泡展示 -->
              <div class="danger-row">
                <div class="danger-row-head">
                  <span class="danger-name">清空练习记录</span>
                  <button
                    class="help-btn"
                    :aria-expanded="helpOpen === 'records'"
                    aria-label="查看「清空练习记录」说明"
                    @click.stop="toggleHelp('records')"
                  >?</button>
                  <div v-if="helpOpen === 'records'" class="help-bubble" @click.stop>
                    清除历史会话与作答记录,重置知识点掌握度与复习计划;题库保留,题目难度重置
                  </div>
                </div>
                <button
                  v-if="confirmMode !== 'records'"
                  class="btn-danger"
                  :disabled="busy"
                  @click="confirmMode = 'records'"
                >清空</button>
                <div v-else class="danger-confirm">
                  <span class="danger-confirm-text">不可恢复,确认清空?</span>
                  <div class="danger-confirm-actions">
                    <button class="btn-danger" :disabled="busy" @click="emit('clear-records')">确认清空</button>
                    <button class="btn-plain" :disabled="busy" @click="confirmMode = 'none'">取消</button>
                  </div>
                </div>
              </div>

              <!-- 清空全部数据:连题库一并删除,需输入「清空」;说明由问号气泡展示 -->
              <div class="danger-row">
                <div class="danger-row-head">
                  <span class="danger-name">清空全部数据</span>
                  <button
                    class="help-btn"
                    :aria-expanded="helpOpen === 'all'"
                    aria-label="查看「清空全部数据」说明"
                    @click.stop="toggleHelp('all')"
                  >?</button>
                  <div v-if="helpOpen === 'all'" class="help-bubble help-bubble-up" @click.stop>
                    在「清空练习记录」基础上,连题库(含待确认候选题)与知识点一并删除,回到全新状态
                  </div>
                </div>
                <button
                  v-if="confirmMode !== 'all'"
                  class="btn-danger"
                  :disabled="busy"
                  @click="confirmMode = 'all'"
                >清空全部</button>
                <div v-else class="danger-all-confirm">
                  <span class="danger-confirm-text">输入「清空」确认删除:</span>
                  <div class="danger-all-actions">
                    <input
                      v-model="confirmText"
                      class="danger-input"
                      type="text"
                      placeholder="清空"
                      :disabled="busy"
                      @keyup.enter="confirmText === '清空' && emit('clear-all')"
                    >
                    <button
                      class="btn-danger"
                      :disabled="busy || confirmText !== '清空'"
                      @click="emit('clear-all')"
                    >确认删除</button>
                    <button class="btn-plain" :disabled="busy" @click="confirmMode = 'none'">取消</button>
                  </div>
                </div>
              </div>
            </div>

            <p v-if="loading" class="saving-hint">
              <span class="btn-spinner" /> 保存中...
            </p>
            <p v-else-if="clearing" class="saving-hint">
              <span class="btn-spinner" /> 清空中...
            </p>
          </div>

          <footer class="dialog-footer">
            <span v-if="error" class="validation-error">{{ error }}</span>
            <span v-else></span>
            <div class="footer-actions">
              <button
                class="btn btn-primary"
                :disabled="busy"
                @click="handleCancel(busy)"
              >完成</button>
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

/* ---- 设置项 ---- */
.setting-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast);
}

.setting-row:hover {
  border-color: var(--color-border-strong);
}

.setting-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.setting-title {
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.setting-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

/* ---- 学习主题 ---- */
.setting-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.topic-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.topic-option {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}

.topic-option:hover:not(:disabled) {
  border-color: var(--color-border-strong);
}

.topic-option:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.topic-active {
  border-color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
}

.topic-radio {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  border: 2px solid var(--color-border-strong);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.topic-active .topic-radio {
  border-color: var(--color-primary);
}

.topic-radio-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
}

.topic-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.topic-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.topic-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

/* ---- 默认出题模型 ---- */
.model-select {
  width: 100%;
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast);
}

.model-select:hover:not(:disabled) {
  border-color: var(--color-border-strong);
}

.model-select:focus {
  outline: none;
  border-color: var(--color-primary);
}

.model-select:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.model-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* 始终用默认出题模型开关行(位于默认出题模型下拉下方) */
.force-default-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-border);
  cursor: pointer;
}

.force-title {
  font-size: var(--fs-sm);
}

/* ---- 开关 ---- */
.switch {
  flex-shrink: 0;
  position: relative;
  width: 40px;
  height: 22px;
  margin-top: 2px;
  border: none;
  border-radius: var(--radius-full);
  background: var(--color-border-strong);
  cursor: pointer;
  padding: 0;
  transition: background var(--transition-fast);
}

.switch-on {
  background: var(--color-primary);
}

.switch:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.switch-thumb {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  transition: transform var(--transition-fast);
}

.switch-on .switch-thumb {
  transform: translateX(18px);
}

.saving-hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
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

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, currentColor 30%, transparent);
  border-top-color: currentColor;
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

/* ---- 危险操作区 ---- */
.danger-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid color-mix(in srgb, var(--color-danger) 40%, var(--color-border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--color-danger) 3%, transparent);
}

.danger-title {
  color: var(--color-danger);
}

.danger-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.danger-name {
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

/* 问号帮助:点击弹出说明气泡,再点或点别处收起 */
.danger-row-head {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.help-btn {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  line-height: 1;
  color: var(--color-text-muted);
  background: transparent;
  border: 1px solid var(--color-border-strong);
  border-radius: 50%;
  cursor: pointer;
  padding: 0;
  transition: all var(--transition-fast);
}

.help-btn:hover,
.help-btn[aria-expanded='true'] {
  color: var(--color-text);
  border-color: var(--color-text-muted);
}

.help-bubble {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  z-index: 10;
  width: 248px;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}

/* 底部行向上弹出,避免被滚动容器裁剪 */
.help-bubble-up {
  top: auto;
  bottom: calc(100% + 6px);
}

.danger-confirm {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-1);
}

.danger-confirm-text {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-danger);
}

.danger-confirm-actions {
  display: flex;
  gap: var(--space-2);
}

.danger-all-confirm {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-2);
}

.danger-all-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.danger-input {
  width: 88px;
  height: 30px;
  padding: 0 var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  outline: none;
}

.danger-input:focus {
  border-color: var(--color-danger);
}

.danger-input:disabled {
  opacity: 0.5;
}

.btn-danger {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-danger);
  background: transparent;
  border: 1px solid color-mix(in srgb, var(--color-danger) 50%, transparent);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-danger) 10%, transparent);
}

.btn-danger:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-plain {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-plain:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.btn-plain:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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
