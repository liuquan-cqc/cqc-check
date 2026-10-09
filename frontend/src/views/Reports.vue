<template>
  <div class="reports-page">
    <!-- 顶部筛选卡片 -->
    <div class="ccc-card filter-card">
      <el-form inline @submit.prevent>
        <el-form-item label="状态">
          <el-select v-model="filters.status" placeholder="全部" clearable style="width: 120px">
            <el-option v-for="(text, value) in STATUS_TEXT" :key="value" :label="text" :value="value" />
          </el-select>
        </el-form-item>
        <el-form-item label="结论">
          <el-select v-model="filters.conclusion" placeholder="全部" clearable style="width: 110px">
            <el-option label="合格" value="合格" />
            <el-option label="需修改" value="需修改" />
            <el-option label="待人工复核" value="待人工复核" />
          </el-select>
        </el-form-item>
        <el-form-item label="产品单元">
          <el-input v-model="filters.product_unit" placeholder="如：电力电缆" clearable style="width: 140px" @keyup.enter="onSearch" />
        </el-form-item>
        <el-form-item label="企业">
          <el-input v-model="filters.company" placeholder="企业名称" clearable style="width: 160px" @keyup.enter="onSearch" />
        </el-form-item>
        <el-form-item label="上传人">
          <el-select v-model="filters.uploader_id" placeholder="全部" clearable filterable style="width: 140px">
            <el-option label="历史数据（未记录）" value="0" />
            <el-option v-for="user in uploaderOptions" :key="user.id" :label="user.username" :value="String(user.id)" />
          </el-select>
        </el-form-item>
        <el-form-item label="日期">
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            style="width: 240px"
          />
        </el-form-item>
        <el-form-item label="关键词">
          <el-input
            v-model="filters.q"
            placeholder="申请编号 / 企业名模糊搜索"
            clearable
            style="width: 200px"
            @keyup.enter="onSearch"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :icon="Search" @click="onSearch">查询</el-button>
          <el-button :icon="RefreshLeft" @click="onReset">重置</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- 列表卡片 -->
    <div class="ccc-card table-card">
      <div class="table-toolbar">
        <span class="table-title">报告列表（共 {{ total }} 条）</span>
        <el-button :icon="Download" :loading="exporting" @click="onExportCsv">导出CSV</el-button>
      </div>

      <el-table v-loading="loading" :data="items" stripe style="width: 100%">
        <el-table-column prop="application_no" label="申请编号" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">{{ row.application_no || '-' }}</template>
        </el-table-column>
        <el-table-column label="企业名称" min-width="300">
          <template #default="{ row }">
            <div v-if="row.company" class="company-cell">
              <el-tooltip :content="row.company.name" placement="top" :show-after="500">
                <span class="company-name">{{ row.company.name }}</span>
              </el-tooltip>
              <el-tooltip :content="companyStatusTooltip(row.company)" placement="top">
                <el-tag :type="companyStatusType(row.company.operating_status)" size="small" effect="light">
                  {{ companyStatusText(row.company.operating_status) }}
                </el-tag>
              </el-tooltip>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="product_unit" label="产品单元" min-width="130" show-overflow-tooltip>
          <template #default="{ row }">{{ row.product_unit || '-' }}</template>
        </el-table-column>
        <el-table-column label="上传人" width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ row.uploader?.username || '历史数据' }}</template>
        </el-table-column>
        <el-table-column label="版本" width="78" align="center">
          <template #default="{ row }">
            <el-tag :type="row.revision_no > 1 ? 'primary' : 'info'" size="small" effect="plain">
              V{{ row.revision_no || 1 }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>
        <el-table-column label="结论" width="120" align="center">
          <template #default="{ row }"><ConclusionTag :conclusion="row.conclusion" /></template>
        </el-table-column>
        <el-table-column prop="review_completed_at" label="审核时间" width="160" sortable>
          <template #default="{ row }">{{ formatTime(row.review_completed_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-link type="primary" :underline="false" @click="goDetail(row)">查看详情</el-link>
            <el-link
              v-permission="'reports.review'"
              type="warning"
              :underline="false"
              class="op-link"
              @click="onReReview(row)"
            >
              重新审核
            </el-link>
            <el-link v-permission="'reports.download'" type="success" :underline="false" class="op-link" @click="onDownload(row)">下载</el-link>
            <el-link
              v-permission="'reports.delete'"
              type="danger"
              :underline="false"
              class="op-link"
              @click="onDelete(row)"
            >
              删除
            </el-link>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无符合条件的报告" :image-size="90" />
        </template>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="size"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, RefreshLeft, Download } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { formatSystemTime } from '@/utils/datetime'
import StatusTag from '@/components/StatusTag.vue'
import ConclusionTag from '@/components/ConclusionTag.vue'
import { deleteReport, downloadReview, getReports, getReportUploaders, reReview } from '@/api/reports'
import { useAppStore } from '@/stores/app'
import type { ReportItem, ReportQuery, ReportUploaderOption } from '@/types'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

/** 状态 → 中文文案（筛选下拉与 CSV 导出共用） */
const STATUS_TEXT: Record<string, string> = {
  pending: '待处理',
  extracting: '提取中',
  reviewing: '审核中',
  done: '已完成',
  failed: '失败'
}

/** 筛选条件（与 URL query 同步） */
const filters = reactive({
  status: '',
  conclusion: '',
  company: '',
  uploader_id: '',
  product_unit: '',
  q: '',
  start: '',
  end: ''
})
/** 日期范围选择器的值（映射到 filters.start / filters.end） */
const dateRange = ref<[string, string] | null>(null)

const page = ref(1)
const size = ref(Number(app.systemConfig.page_size) || 20)
const total = ref(0)
const items = ref<ReportItem[]>([])
const uploaderOptions = ref<ReportUploaderOption[]>([])
const loading = ref(false)
const exporting = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | null = null

/** 时间格式化 */
function formatTime(time?: string | null): string {
  return formatSystemTime(time, app.systemConfig.timezone)
}

const COMPANY_STATUS_TEXT: Record<string, string> = {
  active: '存续', cancelled: '注销', revoked: '吊销', moved: '迁出', other: '其他', unknown: '待核验'
}

function companyStatusText(status?: string): string {
  return COMPANY_STATUS_TEXT[status || 'unknown'] || '待核验'
}

function companyStatusType(status?: string): 'success' | 'danger' | 'warning' | 'info' {
  if (status === 'active') return 'success'
  if (['cancelled', 'revoked'].includes(status || '')) return 'danger'
  if (['moved', 'other'].includes(status || '')) return 'warning'
  return 'info'
}

function companyStatusTooltip(company: NonNullable<ReportItem['company']>): string {
  if (!company.status_source) return '尚未核验企业工商状态'
  const checkedAt = company.status_checked_at ? formatTime(company.status_checked_at) : '未记录时间'
  return `核验来源：${company.status_source}；核验时间：${checkedAt}`
}

/** 从 URL query 恢复筛选条件（刷新/从详情页返回时保留） */
function initFromQuery() {
  const q = route.query
  filters.status = (q.status as string) || ''
  filters.conclusion = (q.conclusion as string) || ''
  filters.company = (q.company as string) || ''
  filters.uploader_id = (q.uploader_id as string) || ''
  filters.product_unit = (q.product_unit as string) || ''
  filters.q = (q.q as string) || ''
  filters.start = (q.start as string) || ''
  filters.end = (q.end as string) || ''
  dateRange.value = filters.start && filters.end ? [filters.start, filters.end] : null
  page.value = Number(q.page) > 0 ? Number(q.page) : 1
  size.value = [10, 20, 50, 100].includes(Number(q.size)) ? Number(q.size) : (Number(app.systemConfig.page_size) || 20)
}

/** 组装接口查询参数（剔除空值） */
function buildQuery(): ReportQuery {
  const params: ReportQuery = { page: page.value, size: size.value }
  if (filters.status) params.status = filters.status as ReportQuery['status']
  if (filters.conclusion) params.conclusion = filters.conclusion
  if (filters.company) params.company = filters.company
  if (filters.uploader_id !== '') params.uploader_id = Number(filters.uploader_id)
  if (filters.product_unit) params.product_unit = filters.product_unit
  if (filters.q) params.q = filters.q
  if (filters.start) params.start = filters.start
  if (filters.end) params.end = filters.end
  return params
}

/** 筛选条件同步到 URL（router.replace 不产生历史记录） */
function syncUrl() {
  const query: Record<string, string> = { page: String(page.value), size: String(size.value) }
  Object.entries(filters).forEach(([k, v]) => {
    if (v) query[k] = v
  })
  router.replace({ query })
}

/** 拉取列表 */
async function fetchList(silent = false) {
  if (!silent) loading.value = true
  try {
    const res = await getReports(buildQuery())
    items.value = res.items || []
    total.value = res.total || 0
  } catch {
    // 错误提示由 http 拦截器统一弹出
  } finally {
    if (!silent) loading.value = false
  }
}

/** 查询：同步日期范围并回到第一页 */
function onSearch() {
  filters.start = dateRange.value?.[0] || ''
  filters.end = dateRange.value?.[1] || ''
  page.value = 1
  fetchList()
  syncUrl()
}

/** 重置筛选 */
function onReset() {
  Object.keys(filters).forEach((k) => ((filters as Record<string, string>)[k] = ''))
  dateRange.value = null
  page.value = 1
  fetchList()
  syncUrl()
}

function onPageChange(p: number) {
  page.value = p
  fetchList()
  syncUrl()
}

function onSizeChange(s: number) {
  size.value = s
  page.value = 1
  fetchList()
  syncUrl()
}

/** 查看详情 */
function goDetail(row: ReportItem) {
  router.push(`/reports/${row.id}`)
}

/** 重新审核（admin/reviewer） */
async function onReReview(row: ReportItem) {
  try {
    await ElMessageBox.confirm(
      `确认对申请「${row.application_no || row.report_no || row.id}」重新发起审核吗？`,
      '重新审核',
      { type: 'warning', confirmButtonText: '重新审核', cancelButtonText: '取消' }
    )
  } catch {
    return // 用户取消
  }
  try {
    await reReview(row.id)
    ElMessage.success('已提交重新审核，可稍后刷新查看进度')
    fetchList()
  } catch {
    ElMessage.error('重新审核提交失败')
  }
}

/** 下载审核意见书（blob 带 token） */
async function onDownload(row: ReportItem) {
  try {
    await downloadReview(row.id, row.application_no || row.report_no)
    ElMessage.success('审核意见书已开始下载')
  } catch {
    ElMessage.error('下载失败，请稍后重试')
  }
}

/** 删除报告（仅 admin，红字二次确认） */
async function onDelete(row: ReportItem) {
  try {
    await ElMessageBox.confirm(
      `删除后申请「${row.application_no || row.report_no || row.id}」及其审核结果将不可恢复，确认删除吗？`,
      '删除确认',
      { type: 'error', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return // 用户取消
  }
  try {
    await deleteReport(row.id)
    ElMessage.success('删除成功')
    // 当前页删空后回退一页
    if (items.value.length === 1 && page.value > 1) page.value -= 1
    fetchList()
    syncUrl()
  } catch {
    ElMessage.error('删除失败，请稍后重试')
  }
}

/** CSV 字段转义（含逗号/引号/换行时加引号包裹） */
function csvEscape(value: string): string {
  const s = value ?? ''
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** 将审核问题项整理成适合 CSV 阅读的一行“不通过原因”。 */
function buildFailureReason(report: ReportItem): string {
  if (report.conclusion === '合格') return '无'

  const reasons: string[] = []
  for (const sample of report.result_json?.samples || []) {
    const sampleName = [sample.model, sample.spec].filter(Boolean).join(' ')
    for (const item of sample.items || []) {
      if (!['must_fix', 'suggestion'].includes(item.severity) || item.action_required === false) continue
      const level = item.severity === 'must_fix' ? '必须修改' : '建议复核'
      const values = [
        item.reported ? `报告值：${item.reported}` : '',
        (item.review_action || item.should_be) ? `处理：${item.review_action || item.should_be}` : ''
      ].filter(Boolean).join('，')
      reasons.push(
        `${level}${sampleName ? `｜${sampleName}` : ''}｜${item.item || '未命名问题'}${values ? `（${values}）` : ''}`
      )
    }
  }
  if (reasons.length) return reasons.join('；')

  const remarks = report.result_json?.remarks?.filter(Boolean) || []
  if (remarks.length) return remarks.join('；')
  if (report.status === 'failed') return `审核失败${report.error_msg ? `：${report.error_msg}` : ''}`
  if (report.conclusion?.includes('需修改')) return report.result_json?.detail || report.conclusion
  return '审核尚未完成'
}

/** 导出 CSV：按当前筛选条件分页拉全量，前端拼接后 blob 下载 */
async function onExportCsv() {
  exporting.value = true
  try {
    const all: ReportItem[] = []
    // 后端单页上限为 100；逐页拉取，避免 size=500 被接口拒绝导致无法导出。
    const pageSize = 100
    let p = 1
    // 分页循环拉取全部数据
    for (;;) {
      const res = await getReports({ ...buildQuery(), page: p, size: pageSize })
      all.push(...(res.items || []))
      if (all.length >= (res.total || 0) || (res.items || []).length < pageSize) break
      p++
    }
    if (!all.length) {
      ElMessage.warning('当前筛选条件下没有可导出的数据')
      return
    }
    const header = ['申请编号', '报告编号', '企业', '企业状态', '状态核验来源', '产品单元', '上传人', '版本', '更正说明', '状态', '结论', '不通过原因', '审核时间']
    const lines = all.map((r) =>
      [
        r.application_no || '',
        r.report_no || '',
        r.company?.name || '',
        companyStatusText(r.company?.operating_status),
        r.company?.status_source || '',
        r.product_unit || '',
        r.uploader?.username || '历史数据（未记录）',
        `V${r.revision_no || 1}`,
        r.revision_note || '',
        STATUS_TEXT[r.status] || r.status,
        r.conclusion || '',
        buildFailureReason(r),
        formatTime(r.review_completed_at)
      ]
        .map(csvEscape)
        .join(',')
    )
    // BOM 头保证 Excel 打开中文不乱码
    const csv = '\uFEFF' + [header.join(','), ...lines].join('\r\n')
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `报告列表_${dayjs().format('YYYYMMDD_HHmm')}.csv`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${all.length} 条记录`)
  } catch {
    ElMessage.error('导出失败，请稍后重试')
  } finally {
    exporting.value = false
  }
}

onMounted(() => {
  initFromQuery()
  getReportUploaders().then((result) => (uploaderOptions.value = result || [])).catch(() => undefined)
  fetchList()
  refreshTimer = setInterval(() => {
    if (items.value.some((item) => ['pending', 'extracting', 'reviewing'].includes(item.status))) {
      fetchList(true)
    }
  }, 5000)
})

onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.filter-card {
  margin-bottom: 16px;
  padding-bottom: 2px;
}

.company-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.company-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.filter-card :deep(.el-form-item) {
  margin-bottom: 12px;
  margin-right: 16px;
}

.table-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.table-title {
  font-size: 15px;
  font-weight: 600;
}

.op-link {
  margin-left: 12px;
}

.pagination-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
