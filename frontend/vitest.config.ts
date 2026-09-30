/// <reference types="vitest" />
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'

// vitest 配置(与 vite.config.ts 分离,避免加载 dev server proxy 配置)
// 单元测试聚焦纯函数,不需要 DOM;jsdom 仅用于偶尔需要 window/localStorage 的工具函数测试
export default defineConfig({
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.{test,spec}.ts'],
    // 不收集 Vue 组件覆盖率(本次只测纯函数),加速启动
    coverage: {
      enabled: false,
    },
  },
})
