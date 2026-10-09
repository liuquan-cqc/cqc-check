import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import 'element-plus/dist/index.css'
// Element Plus 暗色方案（配合 html.dark 类名切换）
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@/styles/index.css'

import App from './App.vue'
import router from './router'
import { vPermission } from '@/directives/permission'
import { useAppStore } from '@/stores/app'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
// 全量引入 Element Plus，中文语言包
app.use(ElementPlus, { locale: zhCn })

// 全量注册 Element Plus 图标，方便各页面直接使用
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

// 按钮级权限指令
app.directive('permission', vPermission)

// 启动时应用持久化的主题
useAppStore().initTheme()

app.mount('#app')
