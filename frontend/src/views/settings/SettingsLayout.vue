<script setup lang="ts">
/**
 * 设置页布局(嵌套路由父组件)
 *
 * 提供共享的页面外壳:AppHeader + WorkspaceSidebar + 左侧设置导航 + 右侧 RouterView。
 * 子路由:account / models / cli / policy
 */
import { ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'

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
          @toggle="toggleWorkspace"
        />
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <!-- 导航 + 内容整体居中 -->
      <div class="settings-shell">
        <!-- 左侧设置导航 -->
        <nav class="settings-nav">
          <RouterLink to="/settings/account" class="nav-item">账户设置</RouterLink>
          <RouterLink to="/settings/models" class="nav-item">模型设置</RouterLink>
          <RouterLink to="/settings/cli" class="nav-item">CLI 设置</RouterLink>
          <RouterLink to="/settings/policy" class="nav-item">协作策略</RouterLink>
        </nav>

        <!-- 右侧子路由内容 -->
        <main class="settings-content">
          <RouterView />
        </main>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
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

/* ---- 导航 + 内容居中容器 ---- */
.settings-shell {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-width: 0;
  max-width: 1180px;
  margin: 0 auto;
  overflow: hidden;
}

/* ---- 左侧设置导航(无边框,靠留白与 active 高亮区分) ---- */
.settings-nav {
  flex-shrink: 0;
  width: 168px;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-6) var(--space-4);
  overflow-y: auto;
}

.nav-item {
  display: block;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  text-decoration: none;
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
}

.nav-item:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.nav-item.router-link-active {
  color: var(--color-primary);
  background: var(--color-primary-light);
  font-weight: var(--fw-semibold);
}

/* ---- 右侧内容区 ---- */
.settings-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
}

/* ---- 响应式:窄屏(手机) ---- */
@media (max-width: 768px) {
  .settings-nav {
    width: 140px;
    padding: var(--space-4) var(--space-2);
  }

  .nav-item {
    padding: var(--space-2);
    font-size: var(--fs-xs);
  }
}

@media (max-width: 640px) {
  .settings-shell {
    flex-direction: column;
    max-width: none;
  }

  .settings-nav {
    width: 100%;
    flex-direction: row;
    padding: var(--space-2) var(--space-3);
    overflow-x: auto;
    overflow-y: hidden;
    gap: var(--space-2);
  }

  .nav-item {
    white-space: nowrap;
    flex-shrink: 0;
  }
}
</style>
