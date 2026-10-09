import http, { get, post, del } from './http'
import type { ReportItem, ReportListResponse, ReportQuery, ReportStatus, ReportUploaderOption, ReviewOut } from '@/types'

/**
 * 报告相关接口
 * 契约见 SPEC/任务说明：/api/reports 系列
 */

/** 报告处理进度（GET /api/reports/{id}/status） */
export interface ReportStatusOut {
  report_id: number
  status: ReportStatus
  conclusion: string | null
  error_msg: string | null
  updated_at: string
  task_status?: string
  attempt?: number
  stage?: string
  progress_current: number
  progress_total: number
  progress_percent: number
  progress_message: string
  application_no?: string | null
  report_no?: string | null
  company_name?: string | null
  product_unit?: string | null
}

/**
 * 报告列表：GET /api/reports
 * 支持分页与筛选（状态/结论/企业/产品单元/关键词/日期区间）
 */
export function getReports(params: ReportQuery): Promise<ReportListResponse> {
  return get<ReportListResponse>('/reports', { params })
}

/** 当前报告数据中实际出现过的上传人，供列表筛选使用。 */
export function getReportUploaders(): Promise<ReportUploaderOption[]> {
  return get<ReportUploaderOption[]>('/reports/uploaders')
}

/** 报告详情：GET /api/reports/{id}（报告详情页使用） */
export function getReportDetail<T = unknown>(id: number | string): Promise<T> {
  return get<T>(`/reports/${id}`)
}

/**
 * 上传 PDF 报告：POST /api/reports/upload（multipart/form-data，字段名 file）
 * onProgress 回调上传进度百分比（0-100）
 */
export function uploadReport(file: File, onProgress?: (percent: number) => void): Promise<ReportItem> {
  const formData = new FormData()
  formData.append('file', file)
  return post<ReportItem>('/reports/upload', formData, {
    // 大文件上传放宽超时到 5 分钟
    timeout: 5 * 60 * 1000,
    onUploadProgress: (e) => {
      if (onProgress) {
        onProgress(e.total ? Math.round((e.loaded / e.total) * 100) : 0)
      }
    }
  })
}

/** 上传实验室更正版本（V2/V3），保留原 PDF 并自动创建审核任务。 */
export function uploadCorrectionVersion(
  id: number | string,
  file: File,
  revisionNote: string,
  onProgress?: (percent: number) => void
): Promise<ReportItem> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('revision_note', revisionNote)
  return post<ReportItem>(`/reports/${id}/correction-version`, formData, {
    timeout: 5 * 60 * 1000,
    onUploadProgress: (e) => onProgress?.(e.total ? Math.round((e.loaded / e.total) * 100) : 0)
  })
}

/** 类别与原报告不同时，沿用已核验申请和任务，创建独立类别版本链。 */
export function uploadIndependentCategory(
  id: number | string,
  file: File,
  onProgress?: (percent: number) => void
): Promise<ReportItem> {
  const formData = new FormData()
  formData.append('file', file)
  return post<ReportItem>(`/reports/${id}/independent-category`, formData, {
    timeout: 5 * 60 * 1000,
    onUploadProgress: (e) => onProgress?.(e.total ? Math.round((e.loaded / e.total) * 100) : 0)
  })
}

/** 获取同一报告的 V1/V2/V3 时间线。 */
export function getReportVersions(id: number | string): Promise<ReportItem[]> {
  return get<ReportItem[]>(`/reports/${id}/versions`)
}

/** 审核进度轮询：GET /api/reports/{id}/status */
export function getReportStatus(id: number | string): Promise<ReportStatusOut> {
  return get<ReportStatusOut>(`/reports/${id}/status`)
}

/** 重新审核：POST /api/reports/{id}/re-review（admin/reviewer） */
export function reReview<T = unknown>(id: number | string): Promise<T> {
  return post<T>(`/reports/${id}/re-review`)
}

/** 删除报告：DELETE /api/reports/{id}（仅 admin） */
export function deleteReport(id: number | string): Promise<void> {
  return del<void>(`/reports/${id}`)
}

/**
 * 下载审核意见书（Markdown）：GET /api/reports/{id}/download
 * 必须用 axios blob 方式携带 token，不能直接 window.open
 */
export async function downloadReview(id: number | string, reportNo?: string): Promise<void> {
  const res = await http.get(`/reports/${id}/download`, {
    responseType: 'blob',
    timeout: 60000
  })
  const url = URL.createObjectURL(res.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `审核意见书_${reportNo || id}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/** 审核意见书：GET /api/reports/{id}/review（未完成审核时后端返回 404） */
export function getReportReview(id: number | string): Promise<ReviewOut> {
  return get<ReviewOut>(`/reports/${id}/review`)
}

export interface TrialStatusOut {
  mode: '8089_formal_trial' | 'disabled'
  human_final_required: boolean
  formal_fallback_kept: boolean
  fallback_url: string
  start_after_report_id: number
  target: number
  total_enrolled: number
  finalized: number
  pending_human_final: number
  current_clean_streak: number
  max_clean_streak: number
  severe_event_records: number
  eligible_to_replace_8080: boolean
  remaining_clean_reports: number
  promotion_message: string
}

export interface TrialRecordOut {
  report_id: number
  sequence_no: number
  human_status: 'pending' | 'confirmed' | 'corrected' | 'returned' | string
  final_decision?: string | null
  severe_miss_detected: boolean
  sensitive_info_externalized: boolean
  incorrect_auto_release: boolean
  safety_evidence: Record<string, unknown>
  review_note?: string | null
  reviewed_by?: number | null
  reviewed_at?: string | null
}

export interface TrialFinalizeIn {
  final_decision: 'confirmed' | 'corrected' | 'returned'
  severe_miss_detected: boolean
  sensitive_info_externalized: boolean
  incorrect_auto_release: boolean
  review_note: string
}

export function getTrialStatus(): Promise<TrialStatusOut | null> {
  return http.get<TrialStatusOut>('/trial/status', {
    // 正式 8080 不部署试运行路由；将 404 作为“未启用试运行”处理，
    // 避免全局错误拦截器把可选能力误报成“接口不存在”。
    validateStatus: (status) => (status >= 200 && status < 300) || status === 404
  }).then((res) => res.status === 404 ? null : res.data)
}

export function getTrialRecord(id: number | string): Promise<TrialRecordOut> {
  return get<TrialRecordOut>(`/trial/reports/${id}`)
}

export function finalizeTrialReport(id: number | string, payload: TrialFinalizeIn): Promise<TrialRecordOut> {
  return post<TrialRecordOut>(`/trial/reports/${id}/finalize`, payload)
}

/**
 * 拉取报告原文 PDF：GET /api/reports/{id}/pdf
 * axios blob 带 token 拉取，转为 blob URL 供 iframe 预览；
 * 调用方负责在不需要时 URL.revokeObjectURL
 */
export async function getReportPdfUrl(id: number | string): Promise<string> {
  return (await getReportPdfDocument(id)).url
}

export async function getReportPdfDocument(id: number | string): Promise<{ url: string; pageCount: number | null }> {
  const res = await http.get(`/reports/${id}/pdf`, {
    responseType: 'blob',
    timeout: 120000
  })
  const blob = new Blob([res.data as Blob], { type: 'application/pdf' })
  const rawCount = res.headers['x-pdf-page-count']
  const count = typeof rawCount === 'string' && /^[1-9]\d*$/.test(rawCount) ? Number(rawCount) : NaN
  return { url: URL.createObjectURL(blob), pageCount: Number.isSafeInteger(count) ? count : null }
}
