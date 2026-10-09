import { get, post, put, del } from './http'
import type { Company } from '@/types'

/** 企业列表：GET /api/companies → [{id, name, address, report_count, last_review_at}] */
export function getCompanies(params?: { page?: number; size?: number; q?: string }) {
  return get<Company[]>('/companies', { params })
}

/** 新建企业：POST /api/companies */
export function createCompany(data: Partial<Company>) {
  return post<Company>('/companies', data)
}

/** 更新企业：PUT /api/companies/{id} */
export function updateCompany(id: number, data: Partial<Company>) {
  return put<Company>(`/companies/${id}`, data)
}

/** 删除企业：DELETE /api/companies/{id} */
export function deleteCompany(id: number) {
  return del<void>(`/companies/${id}`)
}

export function verifyCompanyStatus(id: number) {
  return post<Company>(`/companies/${id}/verify-status`)
}

export interface CompanyVerifyBatchResult {
  total: number
  succeeded: number
  failed: number
  items: Array<{ id: number; name: string; ok: boolean; status?: string; error?: string }>
}

export function verifyCompanyStatusBatch(onlyUnverified = true) {
  return post<CompanyVerifyBatchResult>('/companies/verify-status-batch', { only_unverified: onlyUnverified })
}

export function queueCompanyBrowserVerification(id: number) {
  return post<{ queued: boolean; task_id: number; company_id: number; company_name: string }>(`/companies/${id}/browser-verification`)
}

export function queueCompanyBrowserVerificationBatch() {
  return post<{ queued: number; daily_limit: number; task_ids: number[] }>('/companies/browser-verification-batch')
}
