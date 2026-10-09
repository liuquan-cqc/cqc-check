<template>
  <div class="companies-page">
    <div class="ccc-card">
      <!-- 工具栏 -->
      <div class="toolbar">
        <el-input
          v-model="keyword"
          placeholder="按企业名称搜索"
          clearable
          style="width: 240px"
          :prefix-icon="Search"
          @keyup.enter="fetchList"
          @clear="fetchList"
        />
        <div class="toolbar-actions">
          <el-button :loading="batchVerifying" @click="onBatchVerify">生成今日核验批次</el-button>
          <el-button type="primary" :icon="Plus" @click="openDialog()">新增企业</el-button>
        </div>
      </div>

      <!-- 企业表格 -->
      <el-table v-loading="loading" :data="list" stripe>
        <el-table-column prop="name" label="企业名称" min-width="260" show-overflow-tooltip />
        <el-table-column label="存续状态" width="110" align="center">
          <template #default="{ row }">
            <el-tooltip :content="statusTooltip(row)" placement="top">
              <el-tag :type="statusTagType(row.operating_status)" size="small">
                {{ statusText(row.operating_status) }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="address" label="地址" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.address || '-' }}</template>
        </el-table-column>
        <el-table-column prop="report_count" label="报告数" width="90" align="center">
          <template #default="{ row }">{{ row.report_count ?? 0 }}</template>
        </el-table-column>
        <el-table-column label="最近审核时间" width="160">
          <template #default="{ row }">{{ formatTime(row.last_review_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="270" fixed="right">
          <template #default="{ row }">
            <el-link type="success" :underline="false" :disabled="verifyingId === row.id" @click="onVerify(row)">
              {{ verifyingId === row.id ? '入队中' : '加入核验队列' }}
            </el-link>
            <el-link type="primary" :underline="false" class="op-link" @click="openDialog(row)">编辑</el-link>
            <el-link type="primary" :underline="false" class="op-link" @click="goReports(row)">
              查看报告
            </el-link>
            <el-link type="danger" :underline="false" class="op-link" @click="onDelete(row)">删除</el-link>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无企业数据" :image-size="90" />
        </template>
      </el-table>
    </div>

    <!-- 新增 / 编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑企业' : '新增企业'"
      width="440px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="80px">
        <el-form-item label="企业名称" prop="name">
          <el-input v-model="form.name" placeholder="请输入企业名称" maxlength="100" />
        </el-form-item>
        <el-form-item label="地址" prop="address">
          <el-input v-model="form.address" type="textarea" :rows="2" placeholder="选填" maxlength="200" />
        </el-form-item>
        <el-form-item label="信用代码" prop="unified_social_credit_code">
          <el-input v-model="form.unified_social_credit_code" placeholder="选填，统一社会信用代码" maxlength="32" />
        </el-form-item>
        <el-form-item label="存续状态" prop="operating_status">
          <el-select v-model="form.operating_status" style="width: 100%">
            <el-option label="待核验" value="unknown" />
            <el-option label="存续/在业" value="active" />
            <el-option label="注销" value="cancelled" />
            <el-option label="吊销" value="revoked" />
            <el-option label="迁出" value="moved" />
            <el-option label="其他" value="other" />
          </el-select>
        </el-form-item>
        <el-form-item label="核验来源" prop="status_source">
          <el-input
            v-model="form.status_source"
            type="textarea"
            :rows="2"
            :disabled="form.operating_status === 'unknown'"
            placeholder="例：国家企业信用信息公示系统，或单位内部企业接口"
            maxlength="500"
          />
        </el-form-item>
        <el-alert type="info" :closable="false" show-icon>
          只有填写可追溯核验来源后，“存续/在业”才会在报告列表标绿。
        </el-alert>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Search, Plus } from '@element-plus/icons-vue'
import { formatSystemTime } from '@/utils/datetime'
import { useAppStore } from '@/stores/app'
import { createCompany, deleteCompany, getCompanies, queueCompanyBrowserVerification, queueCompanyBrowserVerificationBatch, updateCompany } from '@/api/companies'
import type { Company } from '@/types'

const router = useRouter()
const app = useAppStore()

const list = ref<Company[]>([])
const loading = ref(false)
const keyword = ref('')
const verifyingId = ref<number | null>(null)
const batchVerifying = ref(false)

// ===== 新增/编辑对话框 =====
const dialogVisible = ref(false)
const saving = ref(false)
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance>()
const form = reactive({
  name: '', address: '', unified_social_credit_code: '', operating_status: 'unknown', status_source: ''
})

const formRules: FormRules = {
  name: [{ required: true, message: '请输入企业名称', trigger: 'blur' }]
}

/** 时间格式化 */
function formatTime(time?: string): string {
  return formatSystemTime(time, app.systemConfig.timezone)
}

const STATUS_TEXT: Record<string, string> = {
  active: '存续', cancelled: '注销', revoked: '吊销', moved: '迁出', other: '其他', unknown: '待核验'
}

function statusText(status?: string): string {
  return STATUS_TEXT[status || 'unknown'] || '待核验'
}

function statusTagType(status?: string): 'success' | 'danger' | 'warning' | 'info' {
  if (status === 'active') return 'success'
  if (['cancelled', 'revoked'].includes(status || '')) return 'danger'
  if (['moved', 'other'].includes(status || '')) return 'warning'
  return 'info'
}

function statusTooltip(company: Company): string {
  if (!company.status_source) return '尚未核验工商状态'
  return `来源：${company.status_source}；核验时间：${formatTime(company.status_checked_at || undefined)}`
}

/** 拉取企业列表 */
async function fetchList() {
  loading.value = true
  try {
    list.value = await getCompanies(keyword.value ? { q: keyword.value } : undefined)
  } catch {
    // 错误提示由 http 拦截器统一弹出
  } finally {
    loading.value = false
  }
}

/** 打开新增（无参数）或编辑（传入行数据）对话框 */
function openDialog(row?: Company) {
  editingId.value = row?.id ?? null
  form.name = row?.name || ''
  form.address = row?.address || ''
  form.unified_social_credit_code = row?.unified_social_credit_code || ''
  form.operating_status = row?.operating_status || 'unknown'
  form.status_source = row?.status_source || ''
  dialogVisible.value = true
}

/** 保存（新增或编辑） */
async function onSave() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  if (form.operating_status !== 'unknown' && !form.status_source.trim()) {
    ElMessage.warning('请填写企业状态的核验来源')
    return
  }
  saving.value = true
  try {
    const payload = {
      name: form.name,
      address: form.address,
      unified_social_credit_code: form.unified_social_credit_code,
      operating_status: form.operating_status,
      status_source: form.operating_status === 'unknown' ? '' : form.status_source
    }
    if (editingId.value) {
      await updateCompany(editingId.value, payload)
      ElMessage.success('企业信息已更新')
    } else {
      await createCompany(payload)
      ElMessage.success('企业创建成功')
    }
    dialogVisible.value = false
    fetchList()
  } catch {
    ElMessage.error('保存失败，请稍后重试')
  } finally {
    saving.value = false
  }
}

/** 删除企业：二次确认；409 表示存在关联报告 */
async function onDelete(row: Company) {
  try {
    await ElMessageBox.confirm(
      `确认删除企业「${row.name}」吗？删除后不可恢复。`,
      '删除确认',
      { type: 'error', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return // 用户取消
  }
  try {
    await deleteCompany(row.id)
    ElMessage.success('删除成功')
    fetchList()
  } catch (e: unknown) {
    const status = (e as { response?: { status?: number } })?.response?.status
    if (status === 409) {
      ElMessage.error('该企业存在关联报告，无法删除')
    } else {
      ElMessage.error('删除失败，请稍后重试')
    }
  }
}

async function onVerify(row: Company) {
  verifyingId.value = row.id
  try {
    await queueCompanyBrowserVerification(row.id)
    ElMessage.success(`已加入队列：${row.name}；请在 Edge 扩展中开始核验`)
  } finally {
    verifyingId.value = null
  }
}

async function onBatchVerify() {
  try {
    await ElMessageBox.confirm(
      '系统将按“每日上限”把待核验企业加入队列。生成队列不会立即访问天眼查，需在已登录的 Edge 扩展中点击开始。',
      '批量核验确认',
      { type: 'warning', confirmButtonText: '开始核验', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  batchVerifying.value = true
  try {
    const result = await queueCompanyBrowserVerificationBatch()
    ElMessage.success(`已加入 ${result.queued} 家（每日上限 ${result.daily_limit} 家）；请在 Edge 扩展中点击开始`)
  } finally {
    batchVerifying.value = false
  }
}

/** 跳转到报告列表并按该企业筛选（列表页支持 URL query 恢复） */
function goReports(row: Company) {
  router.push({ path: '/reports', query: { company: row.name } })
}

onMounted(fetchList)
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.toolbar-actions {
  display: flex;
  gap: 10px;
}

.op-link {
  margin-left: 12px;
}
</style>
