import { get, post, put } from './http'

export interface SystemSettings {
  basic: Record<string, any>
  notifications: Record<string, any>
  ocr: Record<string, any>
  ai: Record<string, any>
  company_registry: Record<string, any>
  files: Record<string, any>
  workflow: Record<string, any>
  security: Record<string, any>
  roles: Record<string, string[]>
}

export interface ClientSettings {
  basic: Record<string, any>
  permissions: string[]
}

export interface AuditLogItem {
  id: number
  username: string
  module: string
  action: string
  detail: string
  ip_address: string
  success: boolean
  created_at: string
}

export function getSystemSettings() {
  return get<SystemSettings>('/settings')
}

export function getClientSettings() {
  return get<ClientSettings>('/settings/client')
}

export function saveSystemSettings(section: string, data: Record<string, any>) {
  const writableData = Object.fromEntries(
    Object.entries(data).filter(([key]) => !key.endsWith('_configured') && key !== 'paddleocr_status')
  )
  return put<Record<string, any>>(`/settings/${section}`, writableData)
}

export function testAiConnection() {
  return post<{ ok: boolean; message: string; provider: string; model: string; response: string }>('/settings/ai/test')
}

export function testIntranetContentReviewConnection() {
  return post<{ ok: boolean; message: string; provider: string; model: string; response: string }>('/settings/ai/preflight/test')
}

export function testPaddleConnection() {
  return post<{ ok: boolean; message: string; recognition_completed: boolean }>('/settings/ocr/paddle/test', { confirm_test_image: true })
}

export function testTableVisionConnection() {
  return post<{ ok: boolean; message: string; provider: string; model: string }>('/settings/ai/table-vision/test')
}

export function testCompanyRegistryConnection() {
  return post<{ ok: boolean; message: string; company: string; status: string }>('/settings/company-registry/test')
}

export function getAuditLogs(params: { module?: string; q?: string; page?: number; size?: number }) {
  return get<{ total: number; items: AuditLogItem[] }>('/settings/logs', { params })
}
