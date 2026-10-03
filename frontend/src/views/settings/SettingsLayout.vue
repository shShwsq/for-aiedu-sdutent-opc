<script setup lang="ts">
/**
 * 设置页布局(嵌套路由父组件)
 *
 * 提供共享的页面外壳:AppHeader + WorkspaceSidebar + 左侧设置导航 + 右侧 RouterView。
 * 子路由:account / models / cli / policy / practice(练习功能开关关闭时隐藏入口)
 */
import { onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { ensureFeaturesLoaded, practiceEnabled } from '@/composables/useFeatures'

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// 练习功能开关:关闭时隐藏「练习设置」导航项(进页拉取一次并缓存)
onMounted(() => {
  ensureFeaturesLoaded()
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

      <!-- 导航 + 内容整体居中 -->
      <div class="settings-shell">
        <!-- 左侧设置导航 -->
        <nav class="settings-nav">
          <RouterLink to="/settings/account" class="nav-item">
            <span class="nav-label">账户设置</span>
            <span class="nav-node" aria-hidden="true" />
          </RouterLink>
          <RouterLink to="/settings/models" class="nav-item">
            <span class="nav-label">模型设置</span>
            <span class="nav-node" aria-hidden="true" />
          </RouterLink>
          <RouterLink to="/settings/cli" class="nav-item">
            <span class="nav-label">CLI 设置</span>
            <span class="nav-node" aria-hidden="true" />
          </RouterLink>
          <RouterLink to="/settings/policy" class="nav-item">
            <span class="nav-label">协作策略</span>
            <span class="nav-node" aria-hidden="true" />
          </RouterLink>
          <RouterLink v-if="practiceEnabled" to="/settings/practice" class="nav-item">
            <span class="nav-label">练习设置</span>
            <span class="nav-node" aria-hidden="true" />
          </RouterLink>
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

/* ---- 左侧设置导航(无边框;右侧竖线轨道 + 节点标记当前项) ---- */
.settings-nav {
  flex-shrink: 0;
  width: 168px;
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: var(--space-6) var(--space-4);
  overflow-y: auto;
}

.nav-item {
  position: relative;
  display: block;
  padding: 10px 36px 10px var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  text-decoration: none;
  border-radius: var(--radius-md);
  transition: color var(--transition-fast), background var(--transition-fast);
}

/* 节点间的连接竖线(位于导航项右侧,与节点同心) */
.nav-item::before {
  content: '';
  position: absolute;
  right: 20px;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--color-border);
}

/* 首尾项竖线各截去一半,使线只在节点之间延伸 */
.nav-item:first-child::before {
  top: 50%;
}

.nav-item:last-child::before {
  bottom: 50%;
}

/* 状态节点:空心圆,当前项填充主色并带光环 */
.nav-node {
  position: absolute;
  right: 16px;
  top: 50%;
  transform: translateY(-50%);
  box-sizing: border-box;
  width: 10px;
  height: 10px;
  border: 2px solid var(--color-border-strong);
  border-radius: 50%;
  background: var(--color-surface);
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

.nav-item.router-link-active .nav-node {
  border-color: var(--color-primary);
  background: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
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
    width: 148px;
    padding: var(--space-4) var(--space-2);
  }

  .nav-item {
    padding: 8px 36px 8px var(--space-2);
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
    padding: var(--space-2) var(--space-3);
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* 横向 tab 布局下隐藏竖线轨道与节点 */
  .nav-item::before,
  .nav-node {
    display: none;
  }
}
</style>
