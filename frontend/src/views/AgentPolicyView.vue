<script setup lang="ts">
/**
 * 协作策略设置页(/agent-policy)
 *
 * 作为 agent2 检查点评估的用户级默认配置:
 * - 评估频率 K:每 K 个 agent1 迭代做一次轻量检查点评估
 * - 打断权限:agent2 是否能通过中断队列向 agent1 注入追问指令(软中断)
 * - 验证权限:agent2 是否能自行调用工具验证(实验性)
 * - 验证授权模式:验证动作的默认授权模式(直接执行 / 逐动作授权)
 *
 * 与记忆管理(/memory)、模型设置(/models)、CLI 设置(/cli) 并列为主导航项。
 * 后端 API:PUT /memory/preferences/agent_policy(与 user_profile 文本分离保存)。
 *
 * 任务创建时可在 TaskCreateView 做任务级覆盖(不改本页默认值)。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import BaseSelect from '@/components/BaseSelect.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { getPolicyLimits, getPreferences, saveAgentPolicy } from '@/api/memory'
import { useUnsavedGuard } from '@/composables/useUnsavedGuard'
import { extractErrorMessage } from '@/utils/error'
import type { SaveAgentPolicyRequest } from '@/types/memory'

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ============================================================
// 默认策略值(与后端 DEFAULT_AGENT_POLICY 对齐)
// ============================================================

// 协作总轮次上限:从后端 GET /memory/policy-limits 动态拉取(默认 10 兜底)
const MAX_ROUNDS_LIMIT = ref(10)

const DEFAULT_POLICY = {
  agent2_enabled: true,
  max_rounds: 4,
  checkpoint_interval: 10,
  checkpoint_interval_builtin: null as number | null,
  checkpoint_interval_cli: null as number | null,
  allow_interrupt: true,
  max_interrupts_per_round: 2,
  allow_verify: false,
  verifier_auth_mode_default: 'per_action' as 'direct' | 'per_action',
  executor_command_confirm_default: 'always_approve' as 'always_approve' | 'per_command',
}

// ============================================================
// 表单状态
// ============================================================
/** 是否启用 agent2(关闭=单 agent 模式,跳过评估/打断/验证) */
const policyAgent2Enabled = ref(DEFAULT_POLICY.agent2_enabled)
/** agent2 协作总轮次(1-10,仅 agent2 启用时生效) */
const policyMaxRounds = ref(DEFAULT_POLICY.max_rounds)
/** 统一 K 值:每 K 个迭代评估一次(1-20) */
const policyInterval = ref(DEFAULT_POLICY.checkpoint_interval)
/** 内置 agent1 专用 K 值(null=用统一值) */
const policyIntervalBuiltin = ref<number | null>(DEFAULT_POLICY.checkpoint_interval_builtin)
/** CLI agent 专用 K 值(null=用统一值) */
const policyIntervalCli = ref<number | null>(DEFAULT_POLICY.checkpoint_interval_cli)
/** agent2 是否能打断 agent1 */
const policyAllowInterrupt = ref(DEFAULT_POLICY.allow_interrupt)
/** 每轮最多打断次数(防死锁,0-10) */
const policyMaxInterrupts = ref(DEFAULT_POLICY.max_interrupts_per_round)
/** agent2 是否能自己验证(实验性) */
const policyAllowVerify = ref(DEFAULT_POLICY.allow_verify)
/** 验证授权默认模式(任务级可覆盖) */
const policyVerifierAuthMode = ref<'direct' | 'per_action'>(DEFAULT_POLICY.verifier_auth_mode_default)
/** 执行智能体命令确认默认模式(任务级 _executor_command_confirm 可覆盖) */
const policyExecutorCommandConfirm = ref<'always_approve' | 'per_command'>(DEFAULT_POLICY.executor_command_confirm_default)

/** 验证授权模式选项(对齐 BaseSelect {value,label} 结构) */
const verifierAuthModeOptions = computed(() => [
  { value: 'per_action' as 'direct' | 'per_action', label: '逐动作授权(每个动作弹窗确认)' },
  { value: 'direct' as 'direct' | 'per_action', label: '直接执行(不弹窗)' },
])

/** 执行智能体命令确认模式选项 */
const executorConfirmOptions = computed(() => [
  { value: 'always_approve' as 'always_approve' | 'per_command', label: '自动批准(不弹窗,注入 YOLO 模式)' },
  { value: 'per_command' as 'always_approve' | 'per_command', label: '逐命令确认(危险命令弹窗批准)' },
])

/** 是否分别配置内置/CLI 的 K 值(高级) */
const policyAdvanced = ref(false)

/** 策略原始值(脏检查基准,hydrate 时写入) */
const originalPolicy = ref({
  agent2Enabled: DEFAULT_POLICY.agent2_enabled,
  maxRounds: DEFAULT_POLICY.max_rounds,
  interval: DEFAULT_POLICY.checkpoint_interval,
  intervalBuiltin: DEFAULT_POLICY.checkpoint_interval_builtin as number | null,
  intervalCli: DEFAULT_POLICY.checkpoint_interval_cli as number | null,
  allowInterrupt: DEFAULT_POLICY.allow_interrupt,
  maxInterrupts: DEFAULT_POLICY.max_interrupts_per_round,
  allowVerify: DEFAULT_POLICY.allow_verify,
  verifierAuthMode: DEFAULT_POLICY.verifier_auth_mode_default,
  executorCommandConfirm: DEFAULT_POLICY.executor_command_confirm_default,
})

/** agent 策略是否有未保存改动 */
const policyDirty = computed(() => {
  return (
    policyAgent2Enabled.value !== originalPolicy.value.agent2Enabled ||
    policyMaxRounds.value !== originalPolicy.value.maxRounds ||
    policyInterval.value !== originalPolicy.value.interval ||
    policyIntervalBuiltin.value !== originalPolicy.value.intervalBuiltin ||
    policyIntervalCli.value !== originalPolicy.value.intervalCli ||
    policyAllowInterrupt.value !== originalPolicy.value.allowInterrupt ||
    policyMaxInterrupts.value !== originalPolicy.value.maxInterrupts ||
    policyAllowVerify.value !== originalPolicy.value.allowVerify ||
    policyVerifierAuthMode.value !== originalPolicy.value.verifierAuthMode ||
    policyExecutorCommandConfirm.value !== originalPolicy.value.executorCommandConfirm
  )
})

// 未保存改动时切换路由弹窗提醒(保存并离开复用 handleSave)
useUnsavedGuard(policyDirty, () => handleSave())

/** 重置策略表单为系统默认值(不立即保存) */
function resetPolicyToDefault(): void {
  policyAgent2Enabled.value = DEFAULT_POLICY.agent2_enabled
  policyMaxRounds.value = DEFAULT_POLICY.max_rounds
  policyInterval.value = DEFAULT_POLICY.checkpoint_interval
  policyIntervalBuiltin.value = DEFAULT_POLICY.checkpoint_interval_builtin
  policyIntervalCli.value = DEFAULT_POLICY.checkpoint_interval_cli
  policyAllowInterrupt.value = DEFAULT_POLICY.allow_interrupt
  policyMaxInterrupts.value = DEFAULT_POLICY.max_interrupts_per_round
  policyAllowVerify.value = DEFAULT_POLICY.allow_verify
  policyVerifierAuthMode.value = DEFAULT_POLICY.verifier_auth_mode_default
  policyExecutorCommandConfirm.value = DEFAULT_POLICY.executor_command_confirm_default
  policyAdvanced.value = false
}

/**
 * 协作总轮次输入处理:只允许非负整数,实时过滤非数字字符,钳制到 [1, MAX_ROUNDS_LIMIT]
 * - 禁止负号、小数点、字母等非法字符
 * - 超过上限自动钳制
 * - 临时空值允许(让用户能删除后重新输入),由 @blur 兜底
 */
function onMaxRoundsInput(e: Event): void {
  const input = e.target as HTMLInputElement
  // 只保留数字字符,过滤负号/小数点/字母
  const filtered = input.value.replace(/\D/g, '')
  if (filtered !== input.value) {
    input.value = filtered
  }
  if (filtered === '') return  // 临时空,不更新 ref
  let n = parseInt(filtered, 10)
  if (n > MAX_ROUNDS_LIMIT.value) {
    n = MAX_ROUNDS_LIMIT.value
    input.value = String(n)
  }
  if (n < 1) n = 1
  policyMaxRounds.value = n
}

/** 协作总轮次失焦:若为空,填默认值 1 */
function onMaxRoundsBlur(e: Event): void {
  const input = e.target as HTMLInputElement
  if (input.value === '') {
    input.value = '1'
    policyMaxRounds.value = 1
  }
}

// ============================================================
// 加载 / 保存
// ============================================================
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const updatedAt = ref<string | null>(null)

/** 顶部居中 toast */
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 5000)
}

async function loadPolicy(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [data, limits] = await Promise.all([getPreferences(), getPolicyLimits()])
    MAX_ROUNDS_LIMIT.value = limits.max_rounds
    updatedAt.value = data.updated_at ?? null
    const policy = data.agent_policy
    policyAgent2Enabled.value = policy?.agent2_enabled ?? DEFAULT_POLICY.agent2_enabled
    policyMaxRounds.value = policy?.max_rounds ?? DEFAULT_POLICY.max_rounds
    policyInterval.value = policy?.checkpoint_interval ?? DEFAULT_POLICY.checkpoint_interval
    policyIntervalBuiltin.value = policy?.checkpoint_interval_builtin ?? DEFAULT_POLICY.checkpoint_interval_builtin
    policyIntervalCli.value = policy?.checkpoint_interval_cli ?? DEFAULT_POLICY.checkpoint_interval_cli
    policyAllowInterrupt.value = policy?.allow_interrupt ?? DEFAULT_POLICY.allow_interrupt
    policyMaxInterrupts.value = policy?.max_interrupts_per_round ?? DEFAULT_POLICY.max_interrupts_per_round
    policyAllowVerify.value = policy?.allow_verify ?? DEFAULT_POLICY.allow_verify
    policyVerifierAuthMode.value = policy?.verifier_auth_mode_default ?? DEFAULT_POLICY.verifier_auth_mode_default
    policyExecutorCommandConfirm.value = policy?.executor_command_confirm_default ?? DEFAULT_POLICY.executor_command_confirm_default
    // 高级模式:仅当任一专用 K 值非 null 时展开
    policyAdvanced.value = policyIntervalBuiltin.value !== null || policyIntervalCli.value !== null
    // 同步原始值(脏检查基准)
    originalPolicy.value = {
      agent2Enabled: policyAgent2Enabled.value,
      maxRounds: policyMaxRounds.value,
      interval: policyInterval.value,
      intervalBuiltin: policyIntervalBuiltin.value,
      intervalCli: policyIntervalCli.value,
      allowInterrupt: policyAllowInterrupt.value,
      maxInterrupts: policyMaxInterrupts.value,
      allowVerify: policyAllowVerify.value,
      verifierAuthMode: policyVerifierAuthMode.value,
      executorCommandConfirm: policyExecutorCommandConfirm.value,
    }
  } catch (err) {
    loadError.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

async function handleSave(): Promise<boolean> {
  if (!policyDirty.value || saving.value) return false
  saving.value = true
  try {
    const body: SaveAgentPolicyRequest = {
      agent2_enabled: policyAgent2Enabled.value,
      max_rounds: policyMaxRounds.value,
      checkpoint_interval: policyInterval.value,
      // 关闭高级模式时,专用 K 值强制为 null(用统一值)
      checkpoint_interval_builtin: policyAdvanced.value ? policyIntervalBuiltin.value : null,
      checkpoint_interval_cli: policyAdvanced.value ? policyIntervalCli.value : null,
      allow_interrupt: policyAllowInterrupt.value,
      max_interrupts_per_round: policyAllowInterrupt.value ? policyMaxInterrupts.value : 0,
      allow_verify: policyAllowVerify.value,
      verifier_auth_mode_default: policyVerifierAuthMode.value,
      executor_command_confirm_default: policyExecutorCommandConfirm.value,
    }
    const data = await saveAgentPolicy(body)
    updatedAt.value = data.updated_at ?? null
    // 重新 hydrate(后端可能规范化字段)
    const policy = data.agent_policy
    if (policy) {
      policyAgent2Enabled.value = policy.agent2_enabled
      policyMaxRounds.value = policy.max_rounds
      policyInterval.value = policy.checkpoint_interval
      policyIntervalBuiltin.value = policy.checkpoint_interval_builtin
      policyIntervalCli.value = policy.checkpoint_interval_cli
      policyAllowInterrupt.value = policy.allow_interrupt
      policyMaxInterrupts.value = policy.max_interrupts_per_round
      policyAllowVerify.value = policy.allow_verify
      policyVerifierAuthMode.value = policy.verifier_auth_mode_default
      policyExecutorCommandConfirm.value = policy.executor_command_confirm_default
      policyAdvanced.value = policyIntervalBuiltin.value !== null || policyIntervalCli.value !== null
    }
    originalPolicy.value = {
      agent2Enabled: policyAgent2Enabled.value,
      maxRounds: policyMaxRounds.value,
      interval: policyInterval.value,
      intervalBuiltin: policyIntervalBuiltin.value,
      intervalCli: policyIntervalCli.value,
      allowInterrupt: policyAllowInterrupt.value,
      maxInterrupts: policyMaxInterrupts.value,
      allowVerify: policyAllowVerify.value,
      verifierAuthMode: policyVerifierAuthMode.value,
      executorCommandConfirm: policyExecutorCommandConfirm.value,
    }
    showToast('协作策略已保存', 'success')
    return true
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
    return false
  } finally {
    saving.value = false
  }
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '从未保存'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

// ============================================================
// 字段帮助气泡:圆圈问号按钮,点击显示说明,点击外部关闭
// 同一时刻只展开一个字段气泡,点开新字段时旧的自动收起
// ============================================================
/** 各字段帮助说明(点击问号按钮展示) */
const FIELD_HELP: Record<string, string> = {
  agent2_enabled:
    '开启后,检查助手参与协作(初始评估、检查点评估、打断、验证)。关闭后退化为单 agent 模式:AI助手 跑 1 轮直接产出结果,不做覆盖度评估、不打断、不验证。适合简单任务或用户完全信任 AI助手的场景。',
  max_rounds:
    '检查助手与 AI助手之间的协作总轮次。每轮含 AI助手执行 + 检查助手评估。轮次越多覆盖越全面但耗时越长。仅检查助手启用时生效。上限为 10。',
  checkpoint_interval:
    '检查助手每 K 个 AI助手迭代做一次轻量检查点评估,判断方向是否跑偏。K 越小评估越频繁(更早纠偏,但开销更大),K 越大开销越小(但跑偏更晚发现)。',
  max_interrupts:
    '防死锁上限:单轮协作中检查助手最多打断 AI助手的次数。超过此上限后即使发现跑偏也只观察不干预,把控制权交还 AI助手。',
  allow_interrupt:
    '开启后,检查助手在检查点评估发现跑偏时,可通过中断队列向 AI助手注入追问指令(软中断),不强行终止当前迭代。',
  allow_verify:
    '开启后,检查助手可自行调用工具验证 AI助手的产出。目前为实验性开关,默认关闭。任务还需在提交时配置测试环境 URL 才会真正触发验证。',
  verifier_auth_mode:
    '仅在开启「自行验证」时生效。逐动作授权:每个验证动作(HTTP 请求 / PoC 脚本)执行前弹窗让用户确认;直接执行:验证动作自动执行不弹窗。此为用户级默认,任务创建或运行时可单独覆盖。',
  executor_command_confirm:
    '控制执行智能体(内置执行器 / Qoder / DeepSeek / Codex)执行危险命令时是否弹窗确认。自动批准:所有命令直接执行不弹窗(速度快,适合可信任务);逐命令确认:每个危险命令执行前弹窗让用户批准(更安全,防容器破坏/资源耗尽)。此为用户级默认,任务创建时可单独覆盖。注意:Codex CLI 受非交互模式限制,仅支持自动批准,选择「逐命令确认」时会降级并警告。',
  policy_advanced:
    '高级选项。内置执行器和外部 CLI agent 的迭代节奏可能不同,可分别设置评估频率。留空则使用统一 K 值。',
}

/** 当前展开帮助气泡的字段 key(null=无展开) */
const openHelpKey = ref<string | null>(null)
/** 字段帮助气泡容器 DOM(按 key 索引,用于点击外部判断) */
const fieldHelpRefs = new Map<string, HTMLElement>()

/** 切换某字段帮助气泡:已展开则收起,未展开则展开(同时收起其他字段) */
function toggleFieldHelp(key: string): void {
  openHelpKey.value = openHelpKey.value === key ? null : key
}

/** 点击帮助容器外部时关闭气泡 */
function onDocClick(e: MouseEvent): void {
  if (openHelpKey.value) {
    const el = fieldHelpRefs.get(openHelpKey.value)
    if (!el || !el.contains(e.target as Node)) {
      openHelpKey.value = null
    }
  }
}

onMounted(() => {
  loadPolicy()
  document.addEventListener('click', onDocClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
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
        <div class="main-col">
          <!-- 页头 -->
          <div class="page-header">
            <div>
              <h1>协作策略</h1>
              <p class="page-subtitle">
                检查助手检查点评估的用户级默认。任务创建时可单独覆盖。
              </p>
            </div>
            <div class="header-meta">
              <span class="meta-label">最后保存</span>
              <span class="meta-value">{{ loading ? '加载中…' : formatTime(updatedAt) }}</span>
            </div>
          </div>

          <!-- 加载态 -->
          <div v-if="loading" class="loading-box">
            <span class="status-spinner" aria-label="加载中" />
            <span>正在加载策略配置…</span>
          </div>

          <!-- 加载失败 -->
          <div v-else-if="loadError" class="alert alert-error" role="alert">
            <span>加载失败:{{ loadError }}</span>
            <button class="btn-link" @click="loadPolicy">重试</button>
          </div>

          <!-- 策略表单(无卡片,平铺更简洁) -->
          <section v-else class="policy-form">
            <!-- 启用 agent2 开关(最核心,控制全局) -->
            <label class="policy-toggle-row policy-toggle-primary">
              <input v-model="policyAgent2Enabled" class="switch" type="checkbox" :disabled="saving" />
              <span>启用检查助手</span>
              <div
                :ref="(el) => { if (el) fieldHelpRefs.set('agent2_enabled', el as HTMLElement); else fieldHelpRefs.delete('agent2_enabled') }"
                class="field-help-wrap"
              >
                <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('agent2_enabled')">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                </button>
                <Transition name="help-fade">
                  <div v-if="openHelpKey === 'agent2_enabled'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.agent2_enabled }}</div>
                </Transition>
              </div>
            </label>

            <!-- 单 agent 模式提示:agent2 关闭时说明下方依赖字段为何隐藏 -->
            <p v-if="!policyAgent2Enabled" class="policy-single-hint">
              当前为单 agent 模式:AI助手 跑 1 轮直接产出结果,不做覆盖度评估、打断与验证。
            </p>

            <!-- agent2 依赖字段:关闭时整组隐藏(v-show 保留值,保存 payload 不变) -->
            <Transition name="collapse">
              <div v-show="policyAgent2Enabled" class="policy-dependent">
            <!-- 协作总轮次(仅 agent2 启用时生效) -->
            <label class="policy-field policy-field-maxrounds">
              <div class="field-head">
                <span class="policy-label">协作总轮次</span>
                <div
                  :ref="(el) => { if (el) fieldHelpRefs.set('max_rounds', el as HTMLElement); else fieldHelpRefs.delete('max_rounds') }"
                  class="field-help-wrap"
                >
                  <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('max_rounds')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                  </button>
                  <Transition name="help-fade">
                    <div v-if="openHelpKey === 'max_rounds'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.max_rounds }}</div>
                  </Transition>
                </div>
              </div>
              <input
                :value="policyMaxRounds"
                @input="onMaxRoundsInput"
                @blur="onMaxRoundsBlur"
                type="text"
                inputmode="numeric"
                pattern="[0-9]*"
                class="policy-input"
                :disabled="saving"
              />
              <span class="policy-hint">上限 {{ MAX_ROUNDS_LIMIT }}</span>
            </label>
  
            <div class="policy-grid">
              <label class="policy-field">
                <div class="field-head">
                  <span class="policy-label">评估频率 K</span>
                  <div
                    :ref="(el) => { if (el) fieldHelpRefs.set('checkpoint_interval', el as HTMLElement); else fieldHelpRefs.delete('checkpoint_interval') }"
                    class="field-help-wrap"
                  >
                    <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('checkpoint_interval')">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                    </button>
                    <Transition name="help-fade">
                      <div v-if="openHelpKey === 'checkpoint_interval'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.checkpoint_interval }}</div>
                    </Transition>
                  </div>
                </div>
                <input
                  v-model.number="policyInterval"
                  type="number" min="1" max="20"
                  class="policy-input"
                  :disabled="saving"
                />
              </label>
  
              <label class="policy-field">
                <div class="field-head">
                  <span class="policy-label">每轮最大打断</span>
                  <div
                    :ref="(el) => { if (el) fieldHelpRefs.set('max_interrupts', el as HTMLElement); else fieldHelpRefs.delete('max_interrupts') }"
                    class="field-help-wrap"
                  >
                    <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('max_interrupts')">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                    </button>
                    <Transition name="help-fade">
                      <div v-if="openHelpKey === 'max_interrupts'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.max_interrupts }}</div>
                    </Transition>
                  </div>
                </div>
                <input
                  v-model.number="policyMaxInterrupts"
                  type="number" min="0" max="10"
                  class="policy-input"
                  :disabled="saving || !policyAllowInterrupt"
                />
              </label>
            </div>
  
            <label class="policy-toggle-row">
              <input v-model="policyAllowInterrupt" class="switch" type="checkbox" :disabled="saving || !policyAgent2Enabled" />
              <span>允许检查助手打断 AI助手</span>
              <div
                :ref="(el) => { if (el) fieldHelpRefs.set('allow_interrupt', el as HTMLElement); else fieldHelpRefs.delete('allow_interrupt') }"
                class="field-help-wrap"
              >
                <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('allow_interrupt')">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                </button>
                <Transition name="help-fade">
                  <div v-if="openHelpKey === 'allow_interrupt'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.allow_interrupt }}</div>
                </Transition>
              </div>
            </label>
  
            <label class="policy-toggle-row">
              <input v-model="policyAllowVerify" class="switch" type="checkbox" :disabled="saving || !policyAgent2Enabled" />
              <span>允许检查助手自行验证 <span class="policy-experimental">(实验性)</span></span>
              <div
                :ref="(el) => { if (el) fieldHelpRefs.set('allow_verify', el as HTMLElement); else fieldHelpRefs.delete('allow_verify') }"
                class="field-help-wrap"
              >
                <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('allow_verify')">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                </button>
                <Transition name="help-fade">
                  <div v-if="openHelpKey === 'allow_verify'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.allow_verify }}</div>
                </Transition>
              </div>
            </label>
  
            <Transition name="collapse">
              <div v-show="policyAllowVerify" class="verifier-config">
                <label class="policy-field">
                  <div class="field-head">
                    <span class="policy-label">验证授权模式</span>
                    <div
                      :ref="(el) => { if (el) fieldHelpRefs.set('verifier_auth_mode', el as HTMLElement); else fieldHelpRefs.delete('verifier_auth_mode') }"
                      class="field-help-wrap"
                    >
                      <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('verifier_auth_mode')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                      </button>
                      <Transition name="help-fade">
                        <div v-if="openHelpKey === 'verifier_auth_mode'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.verifier_auth_mode }}</div>
                      </Transition>
                    </div>
                  </div>
                  <BaseSelect
                    v-model="policyVerifierAuthMode"
                    :options="verifierAuthModeOptions"
                    :disabled="saving"
                    class="policy-select"
                    aria-label="验证授权模式"
                  />
                </label>
              </div>
            </Transition>
              </div>
            </Transition>

            <!-- 执行智能体命令确认模式(独立于 agent2,始终可用) -->
            <label class="policy-field policy-field-command-confirm">
              <div class="field-head">
                <span class="policy-label">执行智能体命令确认模式</span>
                <div
                  :ref="(el) => { if (el) fieldHelpRefs.set('executor_command_confirm', el as HTMLElement); else fieldHelpRefs.delete('executor_command_confirm') }"
                  class="field-help-wrap"
                >
                  <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('executor_command_confirm')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                  </button>
                  <Transition name="help-fade">
                    <div v-if="openHelpKey === 'executor_command_confirm'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.executor_command_confirm }}</div>
                  </Transition>
                </div>
              </div>
              <BaseSelect
                v-model="policyExecutorCommandConfirm"
                :options="executorConfirmOptions"
                :disabled="saving"
                class="policy-select"
                aria-label="执行智能体命令确认模式"
              />
            </label>

            <Transition name="collapse">
              <div v-show="policyAgent2Enabled" class="policy-dependent">
            <label class="policy-toggle-row">
              <input v-model="policyAdvanced" class="switch" type="checkbox" :disabled="saving" />
              <span>分别配置内置 / CLI agent 的 K 值</span>
              <div
                :ref="(el) => { if (el) fieldHelpRefs.set('policy_advanced', el as HTMLElement); else fieldHelpRefs.delete('policy_advanced') }"
                class="field-help-wrap"
              >
                <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('policy_advanced')">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                </button>
                <Transition name="help-fade">
                  <div v-if="openHelpKey === 'policy_advanced'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.policy_advanced }}</div>
                </Transition>
              </div>
            </label>
  
            <Transition name="collapse">
              <div v-show="policyAdvanced" class="policy-grid">
                <label class="policy-field">
                  <span class="policy-label">内置执行器 K</span>
                  <input
                    v-model.number="policyIntervalBuiltin"
                    type="number" min="1" max="20"
                    class="policy-input"
                    placeholder="留空用统一值"
                    :disabled="saving"
                  />
                </label>
                <label class="policy-field">
                  <span class="policy-label">CLI agent K</span>
                  <input
                    v-model.number="policyIntervalCli"
                    type="number" min="1" max="20"
                    class="policy-input"
                    placeholder="留空用统一值"
                    :disabled="saving"
                  />
                </label>
              </div>
            </Transition>
              </div>
            </Transition>
  
            <!-- 操作区 -->
            <div class="policy-actions">
              <button
                type="button"
                class="btn btn-secondary"
                :disabled="saving || !policyDirty"
                @click="resetPolicyToDefault"
              >恢复默认</button>
  
              <button
                type="button"
                class="btn btn-primary"
                :disabled="saving || !policyDirty"
                @click="handleSave"
              >
                <span v-if="saving" class="btn-spinner" />
                {{ saving ? '保存中…' : '保存' }}
              </button>
  
              <span v-if="policyDirty" class="dirty-dot-hint">有未保存改动</span>
            </div>
        </section>
        </div>
      </main>
    </div>

    <!-- 浮动提示弹窗 -->
    <Teleport to="body">
      <Transition name="toast-slide">
        <div
          v-if="toast"
          :class="['toast-popup', toast.type === 'error' ? 'toast-error' : 'toast-success']"
          role="status"
          aria-live="polite"
        >
          <span class="toast-icon" aria-hidden="true">
            <svg
              v-if="toast.type === 'success'"
              width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            >
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <polyline points="22 4 12 14.01 9 11.01" />
            </svg>
            <svg
              v-else
              width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </span>
          <span class="toast-msg">{{ toast.msg }}</span>
        </div>
      </Transition>
    </Teleport>
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

.main {
  flex: 1;
  min-width: 0;
  /* 全宽滚动容器:垂直滚动条贴界面右边,内容在 main-col 内居中 */
  overflow-y: auto;
}

.main-col {
  max-width: 760px;
  margin: 0 auto;
  padding: var(--space-6) var(--space-5) var(--space-8);
}

/* ---- 页头 ---- */
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
}

/* ---- 响应式:窄屏(手机) ---- */
@media (max-width: 640px) {
  .main-col {
    padding: var(--space-4) var(--space-3) var(--space-6);
  }

  /* 页头标题与操作按钮上下堆叠 */
  .page-header {
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-2);
  }
}

.page-header h1 {
  font-size: var(--fs-xl);
  margin: 0;
}

.page-subtitle {
  margin: var(--space-1) 0 0;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.header-meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
}

.meta-label {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.meta-value {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}

/* ---- 加载/错误态 ---- */
.loading-box {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-5);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

.alert {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: status-spin 0.8s linear infinite;
}

@keyframes status-spin {
  to { transform: rotate(360deg); }
}

/* ---- 策略表单(无卡片,直接平铺) ---- */
.policy-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.policy-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
}

.policy-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.policy-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.policy-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
}

.policy-input {
  width: 100%;
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.policy-input:hover:not(:disabled):not(:focus) {
  border-color: var(--color-primary-border);
}

.policy-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.policy-input:disabled {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

/* BaseSelect 撑满字段宽度(验证授权 / CLI 命令确认) */
.policy-select {
  width: 100%;
}

.policy-select .base-select-trigger {
  width: 100%;
}

/* ---- 字段头部:标签 + 帮助按钮(问号) ---- */
.field-head {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

/* ---- 字段级帮助按钮(问号)+ 说明气泡 ---- */
.field-help-wrap {
  position: relative;
  flex-shrink: 0;
  display: inline-flex;
}

.field-help-btn {
  width: 18px;
  height: 18px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
  transition: color var(--transition-fast);
}

.field-help-btn:hover {
  color: var(--color-primary);
}

.field-help-btn svg {
  width: 16px;
  height: 16px;
  display: block;
}

.field-help-popover {
  position: absolute;
  top: calc(100% + var(--space-1));
  left: 0;
  z-index: 20;
  width: max-content;
  min-width: 2em;
  max-width: min(300px, 80vw);
  max-height: 240px;
  overflow-y: auto;
  padding: var(--space-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  word-break: break-word;
}

/* 帮助气泡出现/消失动画 */
.help-fade-enter-active,
.help-fade-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.help-fade-enter-from,
.help-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

.policy-toggle-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.policy-toggle-row:hover {
  background: var(--color-surface-alt);
}

.policy-toggle-row input {
  cursor: pointer;
}

.policy-toggle-row input:disabled {
  cursor: not-allowed;
}

/* 主开关行(启用 agent2):加强视觉权重 */
.policy-toggle-primary {
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  font-weight: var(--fw-medium);
}

/* 单 agent 模式提示(agent2 关闭时展示) */
.policy-single-hint {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

/* agent2 依赖字段容器:作为 policy-form 的单个 flex 项,需恢复内部字段间距 */
.policy-dependent {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

/* ---- Switch 拨动开关(替代原生 checkbox 外观) ---- */
input.switch {
  appearance: none;
  flex-shrink: 0;
  width: 36px;
  height: 20px;
  margin: 0;
  position: relative;
  border-radius: var(--radius-full);
  background: var(--color-border-strong);
  cursor: pointer;
  transition: background var(--transition-fast);
}

input.switch::before {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--color-surface);
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.2);
  transition: transform var(--transition-fast);
}

input.switch:checked {
  background: var(--color-primary);
}

input.switch:checked::before {
  transform: translateX(16px);
}

input.switch:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

input.switch:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.policy-experimental {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-style: italic;
}

/* 验证授权模式配置(仅 allow_verify 开启时显示) */
.verifier-config {
  margin-left: var(--space-5);
  padding: var(--space-2) 0;
}

.verifier-config .policy-field {
  max-width: 360px;
}

/* 执行智能体命令确认模式(独立于 agent2,始终可用) */
.policy-field-command-confirm {
  max-width: 360px;
  margin-bottom: var(--space-2);
}

/* 数字字段限宽,避免单字段独占整行显得空旷 */
.policy-field-maxrounds {
  max-width: 240px;
}

/* ---- 操作区 ---- */
.policy-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border);
}

.btn {
  height: 36px;
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
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

.dirty-dot-hint {
  font-size: var(--fs-xs);
  color: var(--color-primary);
  font-weight: var(--fw-medium);
}

.btn-link {
  background: none;
  border: none;
  padding: 2px 6px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.btn-link:hover {
  text-decoration: underline;
}

/* ---- collapse 过渡 ---- */
.collapse-enter-active,
.collapse-leave-active {
  transition: opacity var(--transition-fast), max-height var(--transition-fast);
  overflow: hidden;
}

.collapse-enter-from,
.collapse-leave-to {
  opacity: 0;
  max-height: 0;
}

.collapse-enter-to,
.collapse-leave-from {
  opacity: 1;
  max-height: 600px;
}

/* ---- toast ---- */
.toast-popup {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  min-width: 280px;
  max-width: 420px;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  box-shadow: var(--shadow-lg);
  border: 1px solid transparent;
}

.toast-success {
  background: var(--color-success-light);
  color: var(--color-success);
  border-color: #bbf7d0;
}

.toast-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border-color: #fecaca;
}

.toast-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  margin-top: 1px;
}

.toast-msg {
  flex: 1;
  word-break: break-word;
  white-space: pre-wrap;
}

.toast-slide-enter-active,
.toast-slide-leave-active {
  transition: opacity var(--transition-base), transform var(--transition-base);
}

.toast-slide-enter-from,
.toast-slide-leave-to {
  opacity: 0;
  transform: translate(-50%, -12px);
}
</style>
