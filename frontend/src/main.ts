/**
 * 应用入口
 *
 * 职责:创建 Vue 实例,安装 Pinia + Router,导入全局样式。
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { initTheme } from './composables/useTheme'

// 全局样式:令牌优先(其他样式依赖 CSS 变量),再加载 reset
import './styles/tokens.css'
import './styles/global.css'

// 主题在挂载前应用,避免首屏闪烁(读取 localStorage + 系统偏好)
initTheme()

const app = createApp(App)

app.use(createPinia())
app.use(router)

app.mount('#app')
