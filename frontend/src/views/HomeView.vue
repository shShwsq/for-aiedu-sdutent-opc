<script setup lang="ts">
/**
 * 首页
 *
 * 登录后的落地页。展示欢迎信息 + 提交任务 CTA。
 * 左侧可展开历史任务栏(WorkspaceSidebar),与任务详情页一致。
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import BrandLogo from '@/components/BrandLogo.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'

const router = useRouter()

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
          :collapsed="workspaceCollapsed"
          expand-title="展开历史任务"
          collapse-title="折叠历史任务"
          data-onboarding="home-workspace-toggle"
          @toggle="toggleWorkspace"
        />
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <div class="welcome-card" data-onboarding="home-welcome-card">
          <div class="welcome-icon">
            <BrandLogo :size="48" />
          </div>
          <h1>欢迎使用 SecondLook</h1>
          <p>双智能体协作系统 · AI助手 执行任务,检查助手 质检结果</p>

          <div class="cta-area">
            <button class="btn-primary" data-onboarding="home-cta" @click="router.push('/tasks/new')">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              提交新任务
            </button>
          </div>

          <div class="feature-grid">
            <div class="feature">
              <div class="feature-icon">
                <BrandLogo :size="24" variant="agent2" />
              </div>
              <h3>检查助手</h3>
              <p>质检 AI助手的审查结果,针对不足追问补全,确保结果可靠</p>
            </div>
            <div class="feature">
              <div class="feature-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <!-- 机器人头部 -->
                  <rect x="4" y="7" width="16" height="12" rx="3" />
                  <!-- 天线 -->
                  <line x1="12" y1="3" x2="12" y2="7" />
                  <circle cx="12" cy="3" r="1.2" fill="currentColor" stroke="none" />
                  <!-- 双眼 -->
                  <circle cx="9" cy="13" r="1.2" fill="currentColor" stroke="none" />
                  <circle cx="15" cy="13" r="1.2" fill="currentColor" stroke="none" />
                  <!-- 底部支架/底座 -->
                  <line x1="8" y1="19" x2="8" y2="21" />
                  <line x1="16" y1="19" x2="16" y2="21" />
                </svg>
              </div>
              <h3>AI助手</h3>
              <p>调用工具执行任务,提交结构化结果供检查助手质检</p>
            </div>
          </div>
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
  /* 手机浏览器地址栏伸缩:用 dvh 保证底部不被遮挡(vh 兜底旧浏览器) */
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
  max-width: var(--content-width);
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-12) var(--space-6);
}

.welcome-card {
  text-align: center;
  padding: var(--space-12);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
}

.welcome-icon {
  display: inline-flex;
  color: var(--color-primary);
  margin-bottom: var(--space-4);
}

.welcome-card h1 {
  font-size: var(--fs-2xl);
  margin-bottom: var(--space-2);
}

.welcome-card > p {
  color: var(--color-text-secondary);
  font-size: var(--fs-base);
}

.cta-area {
  margin: var(--space-8) 0;
}

.btn-primary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 48px;
  padding: 0 var(--space-6);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--space-5);
  margin-top: var(--space-8);
  text-align: left;
}

.feature {
  padding: var(--space-5);
  background: var(--color-surface-alt);
  border-radius: var(--radius-lg);
}

.feature-icon {
  display: inline-flex;
  color: var(--color-primary);
  margin-bottom: var(--space-3);
}

.feature h3 {
  font-size: var(--fs-base);
  margin-bottom: var(--space-2);
}

.feature p {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

/* ---- 响应式:窄屏(手机) ---- */
@media (max-width: 640px) {
  .main {
    padding: var(--space-6) var(--space-3);
  }

  .welcome-card {
    padding: var(--space-6) var(--space-4);
  }

  .welcome-card h1 {
    font-size: var(--fs-xl);
  }

  .cta-area {
    margin: var(--space-6) 0;
  }

  .feature-grid {
    margin-top: var(--space-6);
    gap: var(--space-3);
  }
}
</style>
