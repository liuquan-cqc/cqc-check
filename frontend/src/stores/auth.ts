import { defineStore } from 'pinia'
import { fetchMeApi, loginApi } from '@/api/auth'
import { TOKEN_KEY, USER_KEY, clearAuthStorage } from '@/api/http'
import type { User } from '@/types'

/** 从 localStorage 恢复用户信息（刷新页面后保持登录态） */
function loadUser(): User | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? (JSON.parse(raw) as User) : null
  } catch {
    return null
  }
}

/** 认证状态：token + 当前用户 */
export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: loadUser() as User | null
  }),
  getters: {
    /** 是否已登录 */
    isLoggedIn: (state) => !!state.token
  },
  actions: {
    /** 登录：保存 token 与用户信息 */
    async login(username: string, password: string) {
      const res = await loginApi({ username, password })
      this.token = res.token
      this.user = res.user
      localStorage.setItem(TOKEN_KEY, res.token)
      localStorage.setItem(USER_KEY, JSON.stringify(res.user))
      return res
    },
    /** 拉取当前用户最新信息（登录后或页面刷新时校验） */
    async fetchMe() {
      const user = await fetchMeApi()
      this.user = user
      localStorage.setItem(USER_KEY, JSON.stringify(user))
      return user
    },
    /** 退出登录：清除本地凭证 */
    logout() {
      this.token = ''
      this.user = null
      clearAuthStorage()
    },
    /** 角色判断：当前用户角色是否在允许列表中 */
    hasRole(...roles: string[]): boolean {
      const role = this.user?.role
      if (!role) return false
      return roles.includes(role)
    }
  }
})
