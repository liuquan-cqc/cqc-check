import { get, post, put } from './http'

export type CorrectionStatus = 'pending' | 'approved' | 'rejected'

export interface CorrectionRecord {
  id: number
  report_id: number
  report_label: string
  company_name: string
  product_unit: string
  correction_type: string
  correction_type_text: string
  original_json: Record<string, any>
  corrected_json: Record<string, any>
  description: string
  standard_ref: string
  status: CorrectionStatus
  submitter_name: string
  reviewer_name: string
  review_note: string
  created_at: string
  reviewed_at?: string | null
}

export interface ReviewRuleRecord {
  id: number
  title: string
  category: string
  product_unit: string
  applicable_conditions: string
  rule_text: string
  standard_ref: string
  source_correction_id?: number | null
  status: 'draft' | 'active' | 'retired'
  created_at: string
  updated_at: string
}

export interface ReviewVersionRecord {
  id: number
  version_no: number
  name: string
  description: string
  status: 'draft' | 'active' | 'archived'
  rule_count: number
  rules_json: ReviewRuleRecord[]
  metrics_json: Record<string, any>
  created_at: string
  published_at?: string | null
}

export interface RegressionCaseRecord {
  id: number
  report_id: number
  report_label: string
  name: string
  expected_conclusion: string
  expected_issue_count: number
  enabled: boolean
  notes: string
  created_at: string
}

export interface RegressionRunRecord {
  id: number
  version_id?: number | null
  total: number
  passed: number
  failed: number
  results_json: Array<Record<string, any>>
  created_at: string
}

export function getIterationSummary() { return get<any>('/iteration/summary') }
export function createCorrection(data: Record<string, any>) { return post<{ id: number; status: string }>('/iteration/corrections', data) }
export function getReportCorrections(reportId: number | string) { return get<CorrectionRecord[]>(`/iteration/reports/${reportId}/corrections`) }
export function getCorrections(status = '') { return get<CorrectionRecord[]>('/iteration/corrections', { params: status ? { status } : {} }) }
export function reviewCorrection(id: number, data: Record<string, any>) { return put(`/iteration/corrections/${id}/review`, data) }
export function getRules() { return get<ReviewRuleRecord[]>('/iteration/rules') }
export function createRule(data: Record<string, any>) { return post<ReviewRuleRecord>('/iteration/rules', data) }
export function updateRule(id: number, data: Record<string, any>) { return put<ReviewRuleRecord>(`/iteration/rules/${id}`, data) }
export function getVersions() { return get<ReviewVersionRecord[]>('/iteration/versions') }
export function createVersion(data: Record<string, any>) { return post<ReviewVersionRecord>('/iteration/versions', data) }
export function publishVersion(id: number) { return post<ReviewVersionRecord>(`/iteration/versions/${id}/publish`) }
export function rollbackVersion(id: number) { return post<ReviewVersionRecord>(`/iteration/versions/${id}/rollback`) }
export function getRegressionCases() { return get<RegressionCaseRecord[]>('/iteration/regression-cases') }
export function createRegressionCase(data: Record<string, any>) { return post('/iteration/regression-cases', data) }
export function toggleRegressionCase(id: number, enabled: boolean) { return put(`/iteration/regression-cases/${id}/enabled`, { enabled }) }
export function getRegressionRuns() { return get<RegressionRunRecord[]>('/iteration/regression-runs') }
export function runRegression() { return post<RegressionRunRecord>('/iteration/regression-runs') }

// 知识库与结构化规则库浏览（只读）
export interface KnowledgeNode {
  name: string
  path: string
  is_file: boolean
  children?: KnowledgeNode[]
}
export interface KnowledgeFile {
  path: string
  content: string
}
export interface RulebaseData {
  parameter_rules: Array<Record<string, any>>
  rule_versions: Array<Record<string, any>>
  rule_change_log: Array<Record<string, any>>
}
export interface RulebaseTreeNode {
  name: string
  type: string
  count: number
  meta: Record<string, any>
  children?: RulebaseTreeNode[]
}
export interface RulebaseTreeData {
  summary: {
    series: number
    standards: number
    models: number
    test_items: number
    matrix_rules: number
    parameter_rules: number
    total_rules: number
    version: string
    version_name: string
  }
  tree: RulebaseTreeNode
}

export function getKnowledgeTree() { return get<KnowledgeNode[]>('/iteration/knowledge') }
export function getKnowledgeFile(path: string) { return get<KnowledgeFile>(`/iteration/knowledge/${path}`) }
export function getRulebase() { return get<RulebaseData>('/iteration/rulebase') }
export function getRulebaseTree() { return get<RulebaseTreeData>('/iteration/rulebase-tree') }
