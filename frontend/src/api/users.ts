import { get, post, put } from './http'
import type { User } from '@/types'

/** 用户列表：GET /api/users → [{id, username, role, status, created_at}]（仅 admin） */
export function getUsers() {
  return get<User[]>('/users')
}

/** 新建用户：POST /api/users */
export function createUser(data: { username: string; password: string; role: string; permissions_override?: string[] | null }) {
  return post<User>('/users', data)
}

/** 更新用户：PUT /api/users/{id}（重置密码 / 改角色 / 启用禁用） */
export function updateUser(id: number, data: { password?: string; role?: string; permissions_override?: string[] | null; status?: string }) {
  return put<User>(`/users/${id}`, data)
}
