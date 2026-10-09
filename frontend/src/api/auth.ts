import { get, post } from './http'
import type { LoginResponse, User } from '@/types'

/** 认证相关接口 */
export interface LoginParams {
  username: string
  password: string
}

/** 登录：POST /api/auth/login */
export function loginApi(data: LoginParams): Promise<LoginResponse> {
  return post<LoginResponse>('/auth/login', data)
}

/** 获取当前用户：GET /api/auth/me */
export function fetchMeApi(): Promise<User> {
  return get<User>('/auth/me')
}
