<template>
  <div class="dashboard">
    <!-- 统计卡片行 -->
    <el-row :gutter="16" v-loading="statsLoading">
      <el-col :xs="24" :sm="12" :lg="6">
        <StatCard title="今日审核数" :value="stats.today_count" :icon="DocumentChecked" color="#2563eb" />
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <StatCard title="待审核数" :value="stats.pending_count" :icon="Clock" color="#f59e0b" />
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <StatCard title="累计报告数" :value="stats.total_count" :icon="Files" color="#6366f1" />
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <StatCard title="问题报告数" :value="stats.issue_count" :icon="Warning" color="#ef4444" />
      </el-col>
    </el-row>

    <el-row :gutter="16" class="main-row">
      <!-- 左侧：图表 + 最近记录 -->
      <el-col :xs="24" :lg="18">
        <el-row :gutter="16">
          <el-col :xs="24" :md="12">
            <div class="ccc-card chart-card">
              <div class="card-header">近7天审核趋势</div>
              <el-skeleton v-if="trendLoading" :rows="4" animated />
              <el-empty v-else-if="trendError" description="加载失败，请稍后重试" :image-size="80" />
              <el-empty v-else-if="!trend.length" description="暂无数据" :image-size="80" />
              <VChart v-else :option="trendOption" height="300px" />
            </div>
          </el-col>
          <el-col :xs="24" :md="12">
            <div class="ccc-card chart-card">
              <div class="card-header">问题类型分布</div>
              <el-skeleton v-if="distLoading" :rows="4" animated />
              <el-empty v-else-if="distError" description="加载失败，请稍后重试" :image-size="80" />
              <el-empty v-else-if="!distribution.length" description="暂无数据" :image-size="80" />
              <VChart v-else :option="pieOption" height="300px" />
            </div>
          </el-col>
        </el-row>

        <!-- 最近审核记录 -->
        <div class="ccc-card recent-card">
          <div class="card-header recent-header">
            <span>最近审核记录</span>
            <el-link type="primary" :underline="false" @click="$router.push('/reports')">
              更多<el-icon><ArrowRight /></el-icon>
            </el-link>
          </div>
          <el-table v-loading="reportsLoading" :data="recentReports" stripe>
            <el-table-column prop="application_no" label="申请编号" min-width="190" show-overflow-tooltip>
              <template #default="{ row }">{{ row.application_no || '-' }}</template>
            </el-table-column>
            <el-table-column label="企业名称" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.company?.name || '-' }}</template>
            </el-table-column>
            <el-table-column prop="product_unit" label="产品单元" min-width="130" show-overflow-tooltip />
            <el-table-column label="审核结论" width="120" align="center">
              <template #default="{ row }"><ConclusionTag :conclusion="row.conclusion" /></template>
            </el-table-column>
            <el-table-column label="审核状态" width="100" align="center">
              <template #default="{ row }"><StatusTag :status="row.status" /></template>
            </el-table-column>
            <el-table-column label="审核时间" width="150">
              <template #default="{ row }">{{ formatTime(row.review_completed_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="80" align="center" fixed="right">
              <template #default="{ row }">
                <el-link type="primary" :underline="false" @click="$router.push(`/reports/${row.id}`)">
                  查看
                </el-link>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty :description="reportsError ? '加载失败，请稍后重试' : '暂无审核记录'" :image-size="80" />
            </template>
          </el-table>
        </div>
      </el-col>

      <!-- 右侧：快捷操作 -->
      <el-col :xs="24" :lg="6">
        <!-- 快捷上传：按当前用户实际权限显示 -->
        <div
          v-permission="'reports.upload'"
          class="upload-card"
          @click="$router.push('/upload')"
        >
          <el-icon :size="34"><UploadFilled /></el-icon>
          <div class="upload-title">上传检测报告</div>
          <div class="upload-desc">支持 PDF 格式，单个文件不超过 100MB</div>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { formatSystemTime } from '@/utils/datetime'
import {
  DocumentChecked,
  Clock,
  Files,
  Warning,
  UploadFilled,
  ArrowRight
} from '@element-plus/icons-vue'
import type { EChartsCoreOption } from 'echarts/core'
import StatCard from '@/components/StatCard.vue'
import VChart from '@/components/VChart.vue'
import StatusTag from '@/components/StatusTag.vue'
import ConclusionTag from '@/components/ConclusionTag.vue'
import {
  getDashboardStats,
  getDashboardTrend,
  getErrorDistribution
} from '@/api/dashboard'
import { getReports } from '@/api/reports'
import { useAppStore } from '@/stores/app'
import type {
  DashboardStats,
  ErrorDistributionItem,
  ReportItem,
  TrendPoint
} from '@/types'

const app = useAppStore()

// ===== 统计数据 =====
const stats = reactive<DashboardStats>({
  today_count: 0,
  pending_count: 0,
  total_count: 0,
  issue_count: 0
})
const statsLoading = ref(false)

// ===== 图表数据 =====
const trend = ref<TrendPoint[]>([])
const distribution = ref<ErrorDistributionItem[]>([])
const trendLoading = ref(false)
const distLoading = ref(false)
const trendError = ref(false)
const distError = ref(false)

// ===== 最近审核记录 =====
const recentReports = ref<ReportItem[]>([])
const reportsLoading = ref(false)
const reportsError = ref(false)

/** 图表文字颜色随主题切换 */
const axisColor = computed(() => (app.isDark ? '#a3a6ad' : '#606266'))
const splitLineColor = computed(() => (app.isDark ? 'rgba(255,255,255,0.08)' : '#ebeef5'))

/** 近7天审核趋势：主色蓝圆角柱状图 */
const trendOption = computed<EChartsCoreOption>(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 16, top: 24, bottom: 28 },
  xAxis: {
    type: 'category',
    data: trend.value.map((t) => t.date),
    axisLine: { lineStyle: { color: splitLineColor.value } },
    axisLabel: { color: axisColor.value }
  },
  yAxis: {
    type: 'value',
    minInterval: 1,
    axisLabel: { color: axisColor.value },
    splitLine: { lineStyle: { color: splitLineColor.value } }
  },
  series: [
    {
      type: 'bar',
      data: trend.value.map((t) => t.count),
      barWidth: '45%',
      itemStyle: { color: '#2563eb', borderRadius: [4, 4, 0, 0] }
    }
  ]
}))

/** 问题类型分布：环形饼图，带图例与百分比 */
const pieOption = computed<EChartsCoreOption>(() => ({
  tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
  legend: {
    orient: 'vertical',
    right: 8,
    top: 'middle',
    textStyle: { color: axisColor.value, fontSize: 12 },
    formatter: (name: string) => {
      const item = distribution.value.find((d) => d.name === name)
      const total = distribution.value.reduce((s, d) => s + d.value, 0)
      const pct = total && item ? Math.round((item.value / total) * 100) : 0
      return `${name}  ${pct}%`
    }
  },
  color: ['#2563eb', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6', '#94a3b8'],
  series: [
    {
      type: 'pie',
      radius: ['48%', '72%'],
      center: ['35%', '50%'],
      avoidLabelOverlap: true,
      label: { show: false },
      emphasis: { label: { show: true, fontWeight: 600, formatter: '{b}\n{d}%' } },
      data: distribution.value
    }
  ]
}))

/** 时间格式化 */
function formatTime(time?: string | null): string {
  return formatSystemTime(time, app.systemConfig.timezone)
}

/** 加载全部数据：各接口独立 loading，失败互不影响 */
async function loadAll() {
  statsLoading.value = true
  trendLoading.value = true
  distLoading.value = true
  reportsLoading.value = true

  getDashboardStats()
    .then((res) => Object.assign(stats, res))
    .catch(() => undefined) // 错误提示由 http 拦截器统一弹出
    .finally(() => (statsLoading.value = false))

  getDashboardTrend()
    .then((res) => (trend.value = res || []))
    .catch(() => (trendError.value = true))
    .finally(() => (trendLoading.value = false))

  getErrorDistribution()
    .then((res) => (distribution.value = res || []))
    .catch(() => (distError.value = true))
    .finally(() => (distLoading.value = false))

  getReports({ page: 1, size: 10 })
    .then((res) => (recentReports.value = res.items || []))
    .catch(() => (reportsError.value = true))
    .finally(() => (reportsLoading.value = false))
}

onMounted(loadAll)
</script>

<style scoped>
.dashboard :deep(.el-col) {
  margin-bottom: 16px;
}

.main-row {
  margin-top: 0;
}

.chart-card {
  min-height: 360px;
}

.card-header {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 14px;
}

.recent-card {
  margin-bottom: 16px;
}

.recent-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

/* ===== 快捷上传卡片 ===== */
.upload-card {
  background: linear-gradient(135deg, #2563eb 0%, #1e4fbc 100%);
  border-radius: var(--ccc-card-radius);
  box-shadow: var(--ccc-card-shadow);
  padding: 26px 20px;
  color: #fff;
  text-align: center;
  cursor: pointer;
  transition: transform var(--ccc-transition), box-shadow var(--ccc-transition);
}

.upload-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(37, 99, 235, 0.35);
}

.upload-title {
  font-size: 16px;
  font-weight: 600;
  margin-top: 10px;
}

.upload-desc {
  font-size: 12px;
  opacity: 0.8;
  margin-top: 6px;
}
</style>
