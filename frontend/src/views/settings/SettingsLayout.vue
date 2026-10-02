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

/* ---- 左侧设置导航 ---- */
.settings-nav {
  flex-shrink: 0;
  width: 200px;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-5) var(--space-3);
  border-right: 1px solid var(--color-border);
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
    width: 160px;
    padding: var(--space-3) var(--space-2);
  }

  .nav-item {
    padding: var(--space-2);
    font-size: var(--fs-xs);
  }
}

@media (max-width: 640px) {
  .page-body {
    flex-direction: column;
  }

  .settings-nav {
    width: 100%;
    flex-direction: row;
    border-right: none;
    border-bottom: 1px solid var(--color-border);
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
