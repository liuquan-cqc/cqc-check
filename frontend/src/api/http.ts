import axios, { AxiosError, type AxiosRequestConfig, type AxiosResponse } from 'axios'
import { ElMessage } from 'element-plus'

/** localStorage 中 token / 用户信息的键名（auth store 与 http 层共用） */
export const TOKEN_KEY = 'ccc_token'
export const USER_KEY = 'ccc_user'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function clearAuthStorage(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/** axios 实例：统一 baseURL 与拦截器 */
const http = axios.create({
  baseURL: '/api',
  timeout: 30000
})

// 请求拦截：自动注入 Bearer token
http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：统一错误处理
http.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error: AxiosError<{ detail?: string | { code?: string; message?: string } }>) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail
    const detailText = typeof detail === 'string' ? detail : detail?.message
    if (status === 401) {
      // 未登录或 token 过期：清除凭证并跳转登录页
      clearAuthStorage()
      if (window.location.pathname !== '/login') {
        ElMessage.error('登录已过期，请重新登录')
        const redirect = encodeURIComponent(window.location.pathname + window.location.search)
        window.location.href = `/login?redirect=${redirect}`
      }
    } else if (status === 403) {
      ElMessage.error('无权限操作')
    } else if (!(
      status === 409
      && typeof detail === 'object'
      && ['application_exists', 'correction_category_mismatch'].includes(detail?.code || '')
    )) {
      ElMessage.error(detailText || error.message || '请求失败，请稍后重试')
    }
    return Promise.reject(error)
  }
)

/** 泛型 GET 请求 */
export function get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return http.get(url, config).then((res) => res.data as T)
}

/** 泛型 POST 请求 */
export function post<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  return http.post(url, data, config).then((res) => res.data as T)
}

/** 泛型 PUT 请求 */
export function put<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  return http.put(url, data, config).then((res) => res.data as T)
}

/** 泛型 DELETE 请求 */
export function del<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return http.delete(url, config).then((res) => res.data as T)
}

export default http
