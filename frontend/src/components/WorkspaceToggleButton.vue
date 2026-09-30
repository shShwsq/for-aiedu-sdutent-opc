<script setup lang="ts">
/**
 * 侧栏开关按钮
 *
 * 方框 + 一竖线图标。展开态额外显示另一侧一竖线(侧栏+主区)。
 * side='left'(默认):竖线偏左,用于左侧栏;展开时右侧加一竖线。
 * side='right':竖线偏右,用于右侧栏;展开时左侧加一竖线。
 */
withDefaults(
  defineProps<{
    /** 当前是否折叠 */
    collapsed: boolean
    /** 折叠态鼠标悬浮提示 */
    expandTitle?: string
    /** 展开态鼠标悬浮提示 */
    collapseTitle?: string
    /** 侧栏位置:left=左侧栏(竖线偏左),right=右侧栏(竖线偏右) */
    side?: 'left' | 'right'
  }>(),
  {
    expandTitle: '展开侧栏',
    collapseTitle: '折叠侧栏',
    side: 'left',
  },
)

defineEmits<{
  toggle: []
}>()
</script>

<template>
  <button
    class="workspace-toggle"
    :class="{ 'is-active': !collapsed }"
    :title="collapsed ? expandTitle : collapseTitle"
    :aria-label="collapsed ? expandTitle : collapseTitle"
    @click="$emit('toggle')"
  >
    <svg
      class="workspace-toggle-icon"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <!-- 外框 -->
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <!-- 始终显示的竖线:left 侧栏在 x=9(偏左),right 侧栏在 x=15(偏右) -->
      <line :x1="side === 'left' ? 9 : 15" y1="6" :x2="side === 'left' ? 9 : 15" y2="18" />
      <!-- 展开态:另一侧再加一竖线(侧栏+主区) -->
      <line v-if="!collapsed" :x1="side === 'left' ? 15 : 9" y1="6" :x2="side === 'left' ? 15 : 9" y2="18" />
    </svg>
  </button>
</template>

<style scoped>
.workspace-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  padding: 0;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.workspace-toggle:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.workspace-toggle.is-active {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.workspace-toggle-icon {
  display: block;
  flex-shrink: 0;
}
</style>
