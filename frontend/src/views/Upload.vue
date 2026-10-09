<template>
  <div class="upload-page">
    <!-- 顶部提示条 -->
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="仅支持 PDF 格式检测报告，建议单个文件不超过 100MB；支持批量拖拽上传"
      class="tip-bar"
    />

    <!-- 拖拽上传区 -->
    <div class="ccc-card upload-card">
      <el-upload
        class="uploader"
        drag
        multiple
        accept=".pdf,application/pdf"
        :show-file-list="false"
        :before-upload="beforeUpload"
        :http-request="customUpload"
      >
        <el-icon class="upload-icon"><UploadFilled /></el-icon>
        <div class="el-upload__text">将 PDF 文件拖到此处，或 <em>点击选择文件</em></div>
        <div class="upload-hint">支持多文件批量上传，上传后系统自动完成 提取 → 审核 → 生成意见书</div>
      </el-upload>
    </div>

    <!-- 上传队列 -->
    <div v-if="queue.length" class="ccc-card queue-card">
      <div class="card-header">
        <span>上传队列（{{ queue.length }}）</span>
        <el-button text size="small" @click="clearFinished">清除已完成/失败记录</el-button>
      </div>

      <div v-for="item in queue" :key="item.key" class="queue-item">
        <el-icon class="file-icon" :size="28"><Document /></el-icon>

        <div class="file-main">
          <div class="file-line">
            <span class="file-name" :title="item.name">{{ item.name }}</span>
            <span class="file-size">{{ item.sizeText }}</span>
          </div>

          <!-- 上传中：显示进度条 -->
          <el-progress
            v-if="item.status === 'uploading'"
            :percentage="item.progress"
            :stroke-width="8"
            class="file-progress"
          />

          <!-- 上传成功后：阶段状态 + 文案 -->
          <div v-else class="file-phase">
            <StatusTag :status="item.status" />
            <span class="phase-text">{{ phaseText(item) }}</span>
          </div>

          <!-- 失败原因 -->
          <div v-if="item.status === 'failed' && item.errorMsg" class="error-msg">
            {{ item.errorMsg }}
          </div>
        </div>

        <!-- 操作区 -->
        <div class="file-actions">
          <template v-if="item.status === 'done'">
            <el-button type="primary" size="small" @click="goDetail(item)">查看结果</el-button>
          </template>
          <template v-else-if="item.status === 'failed'">
            <el-button
              v-if="item.suggestedParentId"
              type="primary"
              size="small"
              :loading="item.retrying"
              @click="uploadAsCorrection(item)"
            >
              上传为 V{{ item.suggestedNextVersion || '?' }}
            </el-button>
            <el-button
              v-if="item.independentConflict"
              type="success"
              size="small"
              :loading="item.retrying"
              @click="uploadAsIndependentCategory(item)"
            >
              作为{{ item.independentConflict.detected_category_label || '独立类别' }}上传
            </el-button>
            <el-button
              v-if="item.existingReportId || item.suggestedParentId"
              size="small"
              @click="goExisting(item)"
            >
              查看已有报告
            </el-button>
            <el-button v-else-if="!item.independentConflict" size="small" :icon="RefreshRight" :loading="item.retrying" @click="retry(item)">
              重试
            </el-button>
          </template>
          <el-button
            text
            size="small"
            :icon="Delete"
            class="remove-btn"
            @click="removeItem(item)"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Document, RefreshRight, Delete } from '@element-plus/icons-vue'
import StatusTag from '@/components/StatusTag.vue'
import { getReportStatus, reReview, uploadCorrectionVersion, uploadIndependentCategory, uploadReport } from '@/api/reports'
import type { ReportStatus, UploadConflictDetail } from '@/types'

const router = useRouter()

/** 上传队列项：status 为 uploading（本地上传中）或后端报告状态 */
interface QueueItem {
  key: string
  file: File
  /** 原始文件名（含扩展名） */
  name: string
  /** 默认备注：文件名去扩展名 */
  note: string
  sizeText: string
  /** 上传进度 0-100 */
  progress: number
  status: 'uploading' | ReportStatus
  /** 上传成功后后端返回的报告 ID */
  reportId?: number
  /** 内容重复时指向已有报告；同申请编号时指向建议加入的最新版本。 */
  existingReportId?: number
  suggestedParentId?: number
  suggestedNextVersion?: number
  independentConflict?: UploadConflictDetail
  errorMsg: string
  retrying: boolean
  timer?: ReturnType<typeof setInterval>
}

const queue = reactive<QueueItem[]>([])
/** 所有轮询定时器，离开页面时统一清理 */
const timers = new Set<ReturnType<typeof setInterval>>()

/** 文件大小格式化 */
function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  return Math.max(1, Math.round(bytes / 1024)) + ' KB'
}

/** 上传前校验：仅接受 PDF */
function beforeUpload(file: File): boolean {
  const isPdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name)
  if (!isPdf) {
    ElMessage.error(`「${file.name}」不是 PDF 文件，已拒绝上传`)
    return false
  }
  if (file.size > 100 * 1024 * 1024) {
    ElMessage.warning(`「${file.name}」超过 100MB，上传可能较慢`)
  }
  return true
}

/** el-upload 自定义上传：加入队列并执行上传 */
function customUpload(options: { file: File }) {
  const file = options.file
  const item: QueueItem = reactive({
    key: `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    file,
    name: file.name,
    note: file.name.replace(/\.pdf$/i, ''),
    sizeText: formatSize(file.size),
    progress: 0,
    status: 'uploading',
    errorMsg: '',
    retrying: false
  }) as QueueItem
  queue.unshift(item)
  doUpload(item)
}

/** 执行实际上传 */
async function doUpload(item: QueueItem) {
  item.status = 'uploading'
  item.errorMsg = ''
  item.progress = 0
  item.existingReportId = undefined
  item.suggestedParentId = undefined
  item.suggestedNextVersion = undefined
  item.independentConflict = undefined
  try {
    const res = await uploadReport(item.file, (p) => (item.progress = p))
    acceptUploadResult(item, res.id, res.status || 'pending')
  } catch (error) {
    const conflict = uploadConflict(error)
    if (conflict?.code === 'exact_duplicate') {
      item.status = 'failed'
      item.existingReportId = conflict.report_id
      item.errorMsg = conflict.message || '该PDF已经上传过，请查看已有报告'
      return
    }
    if (conflict?.code === 'application_exists') {
      item.status = 'failed'
      item.suggestedParentId = conflict.report_id
      item.suggestedNextVersion = conflict.next_revision_no
      item.errorMsg = `申请编号 ${conflict.application_no || ''} 已存在，建议加入原报告版本链`
      try {
        await ElMessageBox.confirm(
          `系统识别到相同申请编号，是否将「${item.name}」作为 V${conflict.next_revision_no || '?'} 更正版本上传？`,
          '发现已有申请',
          { type: 'warning', confirmButtonText: '作为更正版本上传', cancelButtonText: '暂不上传' }
        )
        await uploadAsCorrection(item)
      } catch {
        // 用户取消时保留操作按钮，之后仍可继续上传为更正版本。
      }
      return
    }
    item.status = 'failed'
    item.errorMsg = '文件上传失败，请检查网络后重试'
  }
}

function uploadConflict(error: unknown): UploadConflictDetail | null {
  const detail = (error as { response?: { status?: number; data?: { detail?: unknown } } })?.response?.data?.detail
  if (!detail || typeof detail !== 'object' || !(detail as UploadConflictDetail).code) return null
  return detail as UploadConflictDetail
}

function acceptUploadResult(item: QueueItem, reportId: number, status: ReportStatus) {
  item.reportId = reportId
  item.status = status
  item.errorMsg = ''
  item.suggestedParentId = undefined
  item.suggestedNextVersion = undefined
  item.independentConflict = undefined
  if (item.status === 'done' || item.status === 'failed') onFinal(item)
  else startPolling(item)
}

async function uploadAsCorrection(item: QueueItem) {
  if (!item.suggestedParentId) return
  item.retrying = true
  item.status = 'uploading'
  item.errorMsg = ''
  item.progress = 0
  try {
    const res = await uploadCorrectionVersion(
      item.suggestedParentId,
      item.file,
      '同一申请编号的新文件，作为实验室更正版本上传',
      (p) => (item.progress = p)
    )
    acceptUploadResult(item, res.id, res.status || 'pending')
    ElMessage.success(`已作为 V${res.revision_no || item.suggestedNextVersion || ''} 更正版本上传`)
  } catch (error) {
    const conflict = uploadConflict(error)
    item.status = 'failed'
    if (conflict?.code === 'exact_duplicate') {
      item.existingReportId = conflict.report_id
      item.suggestedParentId = undefined
      item.errorMsg = conflict.message
    } else if (conflict?.code === 'correction_category_mismatch') {
      item.suggestedParentId = undefined
      item.independentConflict = conflict
      item.errorMsg = `${conflict.message}（原类别：${conflict.original_category_label || '-'}；新类别：${conflict.detected_category_label || '-'}）`
    } else {
      item.errorMsg = conflict?.message || '更正版本上传失败，请稍后重试'
    }
  } finally {
    item.retrying = false
  }
}

async function uploadAsIndependentCategory(item: QueueItem) {
  const conflict = item.independentConflict
  if (!conflict?.report_id) return
  item.retrying = true
  item.status = 'uploading'
  item.errorMsg = ''
  item.progress = 0
  try {
    const created = await uploadIndependentCategory(
      conflict.report_id,
      item.file,
      (percent) => { item.progress = percent }
    )
    acceptUploadResult(item, created.id, created.status || 'pending')
    ElMessage.success(`已作为${conflict.detected_category_label || '独立类别'} V${created.revision_no || 1} 上传`)
  } catch (error) {
    item.status = 'failed'
    const detail = uploadConflict(error)
    item.errorMsg = detail?.message || '独立类别上传失败，请稍后重试'
  } finally {
    item.retrying = false
  }
}

/** 启动审核进度轮询（每 3 秒） */
function startPolling(item: QueueItem) {
  stopPolling(item)
  item.timer = setInterval(async () => {
    if (!item.reportId) return
    try {
      const s = await getReportStatus(item.reportId)
      item.status = s.status
      item.errorMsg = s.error_msg || ''
      if (s.status === 'done' || s.status === 'failed') {
        stopPolling(item)
        onFinal(item)
      }
    } catch {
      // 单次轮询失败忽略，等待下一轮
    }
  }, 3000)
  timers.add(item.timer)
}

function stopPolling(item: QueueItem) {
  if (item.timer) {
    clearInterval(item.timer)
    timers.delete(item.timer)
    item.timer = undefined
  }
}

/** 到达终态（done/failed）的处理 */
function onFinal(item: QueueItem) {
  if (item.status === 'done') {
    ElMessage.success(`「${item.note}」审核完成，可点击"查看结果"查看审核意见书`)
  }
}

/** 阶段文案 */
function phaseText(item: QueueItem): string {
  const map: Record<string, string> = {
    pending: '已排队，等待系统处理',
    extracting: '正在提取报告文本…',
    reviewing: 'AI 审核中，请稍候…',
    done: '审核完成',
    failed: item.errorMsg ? '处理失败' : '处理失败，可点击重试'
  }
  return map[item.status] || ''
}

/** 重试：未上传成功→重新上传；已上传但审核失败→发起重新审核并恢复轮询 */
async function retry(item: QueueItem) {
  item.retrying = true
  try {
    if (!item.reportId) {
      await doUpload(item)
    } else {
      await reReview(item.reportId)
      item.status = 'pending'
      item.errorMsg = ''
      startPolling(item)
    }
  } catch {
    // 错误提示由拦截器统一弹出
  } finally {
    item.retrying = false
  }
}

/** 跳转审核结果详情页 */
function goDetail(item: QueueItem) {
  if (item.reportId) router.push(`/reports/${item.reportId}`)
}

function goExisting(item: QueueItem) {
  const id = item.existingReportId || item.suggestedParentId
  if (id) router.push(`/reports/${id}`)
}

/** 从队列移除单项 */
function removeItem(item: QueueItem) {
  stopPolling(item)
  const idx = queue.indexOf(item)
  if (idx >= 0) queue.splice(idx, 1)
}

/** 清除所有已到终态的记录 */
function clearFinished() {
  for (let i = queue.length - 1; i >= 0; i--) {
    const item = queue[i]
    if (item.status === 'done' || item.status === 'failed') {
      stopPolling(item)
      queue.splice(i, 1)
    }
  }
}

// 离开页面时清除所有轮询定时器
onBeforeUnmount(() => {
  timers.forEach((t) => clearInterval(t))
  timers.clear()
})
</script>

<style scoped>
.tip-bar {
  margin-bottom: 16px;
}

.upload-card {
  padding: 12px;
  margin-bottom: 16px;
}

/* 拖拽上传区占满卡片 */
.uploader :deep(.el-upload) {
  width: 100%;
}

.uploader :deep(.el-upload-dragger) {
  width: 100%;
  padding: 48px 20px;
  border-radius: var(--ccc-card-radius);
  transition: border-color var(--ccc-transition), background-color var(--ccc-transition);
}

/* 拖拽悬停高亮态 */
.uploader :deep(.el-upload-dragger.is-dragover) {
  border-color: var(--ccc-primary);
  background-color: var(--el-color-primary-light-9);
}

.upload-icon {
  font-size: 56px;
  color: var(--ccc-primary);
  margin-bottom: 12px;
}

.upload-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* ===== 上传队列 ===== */
.queue-card .card-header {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.queue-item {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 8px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.file-icon {
  color: var(--ccc-primary);
  flex-shrink: 0;
}

.file-main {
  flex: 1;
  min-width: 0;
}

.file-line {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.file-name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-size {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}

.file-progress {
  margin-top: 8px;
  max-width: 480px;
}

.file-phase {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 10px;
}

.phase-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.error-msg {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
}

.file-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.remove-btn {
  color: var(--el-text-color-secondary);
}
</style>
