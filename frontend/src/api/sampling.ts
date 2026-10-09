import { del, get, post } from './http'

export interface SamplingModel {
  id: string
  display_name: string
  voltage: string
  subunit?: string | null
  series: string
  specification_groups: Array<Record<string, any>>
}

export interface SamplingUnit {
  unit_code: string
  unit_name: string
  status: string
  enabled: boolean
  version: string
  open_questions: Array<Record<string, any>>
  models: SamplingModel[]
}

export interface SamplingCatalog {
  rule_package_version: string
  units: SamplingUnit[]
}

export interface SamplingSample {
  id?: string
  application_index: number
  application_no: string
  unit_code: string
  model_ref: string
  model: string
  voltage: string
  group_id: string
  section: number | null
  cores: number | null
  shape: string
  special_expression?: string
  color?: 'white' | 'black' | null
  any_spec: boolean
  shape_explicit: boolean
  specification: string
  reasons: string[]
  rule_refs: string[]
  material_coverage: string[]
}

export interface SamplingResult {
  rule_package_version: string
  rule_status: string
  validation_status: string
  final_confirmation_allowed: boolean
  hard_gap_count: number
  warnings: string[]
  samples: SamplingSample[]
  material_analysis: Array<Record<string, any>>
  copy_text: string
  applications: Array<Record<string, any>>
  manual_gaps?: string[]
}

export interface SamplingTaskListItem {
  id: number
  task_no: string
  mode: 'fast' | 'merged'
  application_nos: string[]
  status: string
  current_version_id: number
  current_version_no: number
  sample_count: number
  validation_status: string
  hard_gap_count: number
  creator_name: string
  created_at: string
  updated_at: string
}

export interface SamplingScopeEntry {
  raw_text: string
  normalized_text?: string
  status: 'recognized' | 'needs_review' | 'unrecognized'
  model_ref?: string
  model?: string
  voltage?: string
  unit_code?: string
  section_min?: number | null
  section_max?: number | null
  core_min?: number | null
  core_max?: number | null
  shapes?: string[]
  include_special?: boolean
  scope_expression?: string
  issues: string[]
}

export interface SamplingScopeParseResult {
  entries: SamplingScopeEntry[]
  normalized_scope_text: string
  models: Array<Record<string, any>>
  groups: Array<{ unit_code: string; models: Array<Record<string, any>> }>
  unit_code: string | null
  unit_codes: string[]
  recognized_count: number
  unrecognized_count: number
  review_count: number
  can_apply: boolean
  issues: string[]
}

export interface SamplingMaterialSupplier {
  name: string
  cqc_numbers: string[]
}

export interface SamplingMaterialRecognitionGroup {
  group_code: string
  category: string
  category_label: string
  compatibility_group: string
  total_items: number
  cqc_filed_items: number
  model_refs: string[]
  suppliers: SamplingMaterialSupplier[]
  material_brands: string[]
  needs_review: boolean
  source: string
}

export interface SamplingMaterialRecognitionResult {
  documents: Array<{ filename: string; extraction_method: string; text_length: number; warnings: string[] }>
  groups: SamplingMaterialRecognitionGroup[]
  warnings: string[]
  group_count: number
  review_count: number
}

export function getSamplingCatalog() {
  return get<SamplingCatalog>('/sampling/catalog')
}

export function previewSampling(data: Record<string, any>) {
  return post<SamplingResult>('/sampling/preview', data)
}

export function parseSamplingScope(text: string) {
  return post<SamplingScopeParseResult>('/sampling/parse-scope', { text })
}

export function recognizeSamplingMaterials(files: File[]) {
  const form = new FormData()
  files.forEach(file => form.append('files', file, file.name))
  return post<SamplingMaterialRecognitionResult>('/sampling/recognize-materials', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000
  })
}

export function createSamplingTask(data: Record<string, any>) {
  return post<{ id: number; task_no: string; version_id: number; result: SamplingResult }>('/sampling/tasks', data)
}

export function getSamplingTasks(q = '') {
  return get<SamplingTaskListItem[]>('/sampling/tasks', { params: q ? { q } : {} })
}

export function getSamplingTask(id: number) {
  return get<any>(`/sampling/tasks/${id}`)
}

export function deleteSamplingTask(id: number) {
  return del<{ ok: boolean; task_no: string; deleted_version_count: number }>(`/sampling/tasks/${id}`)
}

export function createSamplingVersion(id: number, data: Record<string, any>) {
  return post<{ version_id: number; version_no: number; result: SamplingResult }>(`/sampling/tasks/${id}/versions`, data)
}
