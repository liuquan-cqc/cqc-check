/**
 * 后端 API 类型定义
 * 与 FastAPI 后端接口契约保持一致
 */

/** 用户角色：admin 全部权限 / reviewer 上传+重审+查看 / viewer 仅查看+下载 */
export type UserRole = 'admin' | 'reviewer' | 'viewer'

/** 登录用户信息 */
export interface User {
  id: number
  username: string
  role: UserRole | string
  /** 为空时继承角色权限；数组表示该账号使用专属权限。 */
  permissions_override?: string[] | null
  status: string
  created_at?: string
}

/** 登录响应 */
export interface LoginResponse {
  token: string
  user: User
}

/** 仪表盘统计数据 */
export interface DashboardStats {
  today_count: number
  pending_count: number
  total_count: number
  issue_count: number
}

/** 近7天审核趋势数据点 */
export interface TrendPoint {
  /** 日期，格式 MM-DD */
  date: string
  count: number
}

/** 问题类型分布数据项 */
export interface ErrorDistributionItem {
  name: string
  value: number
}

/** 企业信息 */
export interface Company {
  id: number
  name: string
  address?: string
  unified_social_credit_code?: string | null
  operating_status?: 'unknown' | 'active' | 'cancelled' | 'revoked' | 'moved' | 'other' | string
  status_source?: string | null
  status_checked_at?: string | null
  /** 企业累计报告数（企业列表接口返回） */
  report_count?: number
  /** 最近审核时间（企业列表接口返回） */
  last_review_at?: string
  created_at?: string
}

/** 报告处理状态 */
export type ReportStatus = 'pending' | 'extracting' | 'reviewing' | 'done' | 'failed'

/** 报告列表项 */
export interface ReportItem {
  id: number
  report_no: string
  application_no: string
  original_filename?: string | null
  file_sha256?: string | null
  product_unit: string
  status: ReportStatus
  conclusion: string | null
  created_at: string
  updated_at: string
  review_completed_at: string | null
  company: Company | null
  uploaded_by_id?: number | null
  uploader?: User | null
  result_json?: ReviewContent | null
  error_msg?: string | null
  root_report_id: number | null
  parent_report_id: number | null
  revision_no: number
  revision_note?: string | null
}

/** 报告分页列表响应 */
export interface ReportListResponse {
  total: number
  items: ReportItem[]
}

/** 报告详情（GET /api/reports/{id}） */
export interface ReportDetailData {
  id: number
  report_no: string
  application_no: string
  product_unit: string
  product_desc: string | null
  status: ReportStatus
  conclusion: string | null
  created_at: string
  updated_at: string
  review_completed_at: string | null
  error_msg: string | null
  company: Company | null
  samples_json?: string | null
  result_json?: string | null
  root_report_id: number | null
  parent_report_id: number | null
  revision_no: number
  revision_note?: string | null
}

/** 审核明细单项（severity: must_fix 必须改 / suggestion 建议 / ok 正确） */
export interface ReviewItem {
  item: string
  reported: string
  should_be: string
  standard: string
  severity: 'must_fix' | 'suggestion' | 'ok' | string
  /** 是否确实需要修改或人工复核；与文字说明分离，避免“确认正确”被算成问题 */
  action_required?: boolean
  action_type?: 'correction' | 'manual_review' | 'none' | string
  review_action?: string
}

/** 每个样品的可追溯逐项核对证据。 */
export interface ReviewCheck {
  category: string
  item: string
  reported: string
  required: string
  verdict: 'pass' | 'fail' | 'not_applicable' | 'manual_review' | string
  basis: string
  note?: string
  source_pages?: number[]
  evidence_status?: 'located' | 'rule_derived' | 'not_located' | string
}

/** 审核意见书中的样品 */
export interface ReviewSample {
  model: string
  voltage: string
  spec: string
  items: ReviewItem[]
  checks?: ReviewCheck[]
}

/** 审核意见书内容结构 */
export interface ReviewContent {
  report_items?: ReviewItem[]
  report_checks?: ReviewCheck[]
  application_no: string
  report_no: string
  company: string
  product_desc: string
  product_unit: string
  conclusion: string
  samples: ReviewSample[]
  remarks: string[]
  /** 审核明细 Markdown 全文 */
  detail: string
  revision_comparison?: RevisionComparison
  _review_meta?: {
    privacy_preflight?: PrivacyPreflightMeta
    external_content_review?: ExternalContentReviewMeta
    [key: string]: unknown
  }
  _deterministic_validation?: {
    required_item_coverage?: {
      required_total?: number
      required_covered?: number
      conditional_total?: number
      conditional_covered?: number
      conditional_unknown?: number
      missing?: unknown[]
      unmatched_samples?: string[]
    }
    [key: string]: unknown
  }
}

export interface PrivacyLogicCheck {
  item: string
  result: 'pass' | 'warning' | 'manual_review' | string
  note: string
}

export interface PrivacyPreflightMeta {
  mode: 'shadow' | 'enforce' | string
  semantic_model_used?: boolean
  batch_count: number
  company_replacements: number
  application_replacements: number
  model_errors: number
  outbound_findings: number
  logic_checks: PrivacyLogicCheck[]
}

export interface ExternalContentReviewMeta {
  provider: string
  model: string
  batch_count: number
  successful_batches: number
  failed_batches: number
  cache_reused_batches: number
  logic_checks: PrivacyLogicCheck[]
}

export interface RevisionComparisonItem {
  sample: string
  original_item: string
  original_reported: string
  required_correction: string
  new_evidence: string
  status: 'resolved' | 'unresolved' | 'manual_review' | string
  note: string
}

export interface RevisionComparison {
  parent_report_id: number
  original_issue_count: number
  resolved_count: number
  unresolved_count: number
  manual_review_count: number
  new_issue_count: number
  status: string
  items: RevisionComparisonItem[]
  new_issues: Array<ReviewItem & { sample?: string }>
}

/** 审核意见书（GET /api/reports/{id}/review） */
export interface ReviewOut {
  report_id: number
  version: number
  conclusion: string
  content: ReviewContent
  markdown?: string
}

/** 报告列表查询参数 */
export interface ReportQuery {
  page?: number
  size?: number
  status?: ReportStatus | ''
  conclusion?: string
  company?: string
  uploader_id?: number
  product_unit?: string
  q?: string
  start?: string
  end?: string
  include_versions?: boolean
}

/** 报告列表“上传人”筛选选项。 */
export interface ReportUploaderOption {
  id: number
  username: string
}

/** 上传冲突：完全重复，或同一申请编号应进入更正版本链。 */
export interface UploadConflictDetail {
  code: 'exact_duplicate' | 'application_exists' | 'application_mismatch' | string
  message: string
  report_id: number
  root_report_id?: number
  application_no?: string | null
  report_no?: string | null
  revision_no?: number
  next_revision_no?: number
  task_no?: string
  original_category?: string
  original_category_label?: string
  detected_category?: string
  detected_category_label?: string
}
