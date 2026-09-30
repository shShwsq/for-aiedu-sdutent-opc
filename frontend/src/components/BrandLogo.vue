<script setup lang="ts">
/**
 * 项目品牌 logo
 *
 * 新版设计:六边形环(交替双色段)+ 双对话气泡(象征 agent2 与 agent1 的协作对话)。
 * - brand 变体:完整图标(六边形环 + 双气泡),用于品牌标志(AppHeader / LoginView / HomeView 欢迎区)
 * - agent2 变体:人物图标(紫色渐变),象征 agent2 作为用户代理的人类角色,用作 agent2 头像
 *
 * 使用固定品牌渐变色(gradA 紫 / gradB 青),不随 currentColor 变化。
 */
withDefaults(
  defineProps<{
    /** 图标尺寸(px) */
    size?: number | string
    /** 变体:brand 完整品牌图标 | agent2 仅 agent2 气泡 */
    variant?: 'brand' | 'agent2'
  }>(),
  {
    size: 24,
    variant: 'brand',
  },
)
</script>

<template>
  <!-- 品牌完整图标:六边形环 + 双对话气泡 -->
  <svg
    v-if="variant === 'brand'"
    :width="size"
    :height="size"
    viewBox="0 0 180 180"
    fill="none"
    aria-hidden="true"
  >
    <defs>
      <linearGradient id="brand-grad-a" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#6C5CE7" />
        <stop offset="100%" stop-color="#4834D4" />
      </linearGradient>
      <linearGradient id="brand-grad-b" x1="100%" y1="0%" x2="0%" y2="100%">
        <stop offset="0%" stop-color="#00CEC9" />
        <stop offset="100%" stop-color="#0984E3" />
      </linearGradient>
    </defs>
    <g transform="translate(90, 90)">
      <!-- 六边形环:纵向压缩 0.8 倍,使左右梯形更矮、整体更紧凑 -->
      <g transform="scale(1, 0.8)">
        <polygon points="-30,-78 30,-78 48,-52 -48,-52" fill="url(#brand-grad-a)" />
        <polygon points="48,-52 78,0 58,18 30,-48" fill="url(#brand-grad-b)" />
        <polygon points="58,18 58,54 30,78 30,42" fill="url(#brand-grad-a)" />
        <polygon points="30,42 30,78 -30,78 -30,42" fill="url(#brand-grad-b)" />
        <polygon points="-30,42 -30,78 -58,54 -58,18" fill="url(#brand-grad-a)" />
        <polygon points="-58,18 -78,0 -48,-52 -30,-48" fill="url(#brand-grad-b)" />
      </g>
      <!-- 双气泡:放大 1.3 倍并平移至六边形环中心(略偏右以视觉居中) -->
      <g transform="translate(-4, -22) scale(1.3)">
        <!-- 左气泡(agent2 / 提问者) -->
        <g transform="translate(-16, -4)">
          <path
            d="M0,0 h22 a6,6 0 0 1 6,6 v14 a6,6 0 0 1 -6,6 h-10 l-8,8 v-8 h-4 a6,6 0 0 1 -6,-6 v-14 a6,6 0 0 1 6,-6 z"
            fill="url(#brand-grad-a)"
          />
          <circle cx="6" cy="13" r="2" fill="#fff" />
          <circle cx="11" cy="13" r="2" fill="#fff" />
          <circle cx="16" cy="13" r="2" fill="#fff" />
        </g>
        <!-- 右气泡(agent1 / 回应者),下移错位,含勾选符号(审计/总结) -->
        <g transform="translate(4, 8)">
          <path
            d="M0,0 h22 a6,6 0 0 1 6,6 v14 a6,6 0 0 1 -6,6 h-10 l-8,8 v-8 h-4 a6,6 0 0 1 -6,-6 v-14 a6,6 0 0 1 6,-6 z"
            fill="url(#brand-grad-b)"
          />
          <path
            d="M5,14 l4,4 l8,-8"
            fill="none"
            stroke="#fff"
            stroke-width="2.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </g>
      </g>
    </g>
  </svg>
  <!-- agent2 头像:人物图标(紫色渐变),象征 agent2 作为用户代理的人类角色 -->
  <svg
    v-else
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    aria-hidden="true"
  >
    <defs>
      <linearGradient id="ua-grad-a" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#6C5CE7" />
        <stop offset="100%" stop-color="#4834D4" />
      </linearGradient>
    </defs>
    <!-- 头部 -->
    <circle cx="12" cy="8" r="4.2" fill="url(#ua-grad-a)" />
    <!-- 身体(圆弧肩膀) -->
    <path
      d="M4 21 C4 16.03 7.58 12.5 12 12.5 C16.42 12.5 20 16.03 20 21 Z"
      fill="url(#ua-grad-a)"
    />
  </svg>
</template>
