import { defineStore } from 'pinia'
import { getClientSettings } from '@/api/settings'

export type ThemeMode = 'light' | 'dark'

const THEME_KEY = 'ccc_theme'

/** 读取初始主题：优先 localStorage，否则跟随系统 prefers-color-scheme */
function initialTheme(): ThemeMode {
  const saved = localStorage.getItem(THEME_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light'
}

/** 把主题应用到 <html> 根元素（Element Plus 暗色方案依赖 html.dark） */
function applyTheme(theme: ThemeMode) {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

/** 应用全局状态：主题模式 + 侧边栏折叠 */
export const useAppStore = defineStore('app', {
  state: () => ({
    theme: initialTheme() as ThemeMode,
    sidebarCollapsed: false,
    systemConfig: {
      system_name: '电线电缆检测报告审核系统',
      system_version: '1.0.0',
      copyright: '中国质量认证中心',
      primary_color: '#2563eb',
      logo_data_url: '',
      theme_mode: 'system'
    } as Record<string, any>,
    permissions: [] as string[],
    configLoaded: false
  }),
  getters: {
    isDark: (state) => state.theme === 'dark'
  },
  actions: {
    /** 初始化主题（main.ts 启动时调用一次） */
    initTheme() {
      applyTheme(this.theme)
    },
    async loadSystemConfig() {
      try {
        const result = await getClientSettings()
        this.systemConfig = { ...this.systemConfig, ...result.basic }
        this.permissions = result.permissions || []
        const color = this.systemConfig.primary_color || '#2563eb'
        document.documentElement.style.setProperty('--ccc-primary', color)
        document.documentElement.style.setProperty('--el-color-primary', color)
        document.documentElement.dataset.tableDensity = this.systemConfig.table_density || 'comfortable'
        const configuredTheme = this.systemConfig.theme_mode
        if (!localStorage.getItem(THEME_KEY) && (configuredTheme === 'light' || configuredTheme === 'dark')) {
          this.theme = configuredTheme
          applyTheme(this.theme)
        }
      } finally {
        this.configLoaded = true
      }
    },
    hasPermission(permission: string) {
      return this.permissions.includes(permission)
    },
    /** 切换深色/浅色并持久化 */
    toggleTheme() {
      this.theme = this.theme === 'dark' ? 'light' : 'dark'
      localStorage.setItem(THEME_KEY, this.theme)
      applyTheme(this.theme)
    },
    /** 切换侧边栏折叠 */
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed
    }
  }
})
