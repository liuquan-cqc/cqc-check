<template>
  <!-- 报告状态彩色标签 -->
  <el-tag :type="tagType" :effect="effect" size="small" round>{{ tagText }}</el-tag>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ReportStatus } from '@/types'

const props = withDefaults(
  defineProps<{
    /** 报告状态：pending/extracting/reviewing/done/failed */
    status: ReportStatus | string | null | undefined
    /** 标签风格，默认 light */
    effect?: 'light' | 'dark' | 'plain'
  }>(),
  { effect: 'light' }
)

/** 状态 → 文案映射 */
const TEXT_MAP: Record<string, string> = {
  pending: '待处理',
  extracting: '提取中',
  reviewing: '审核中',
  done: '已完成',
  failed: '失败'
}

/** 状态 → el-tag 类型映射 */
const TYPE_MAP: Record<string, 'info' | 'primary' | 'success' | 'danger' | 'warning'> = {
  pending: 'info',
  extracting: 'primary',
  reviewing: 'primary',
  done: 'success',
  failed: 'danger'
}

const tagText = computed(() => TEXT_MAP[props.status || ''] || props.status || '-')
const tagType = computed(() => TYPE_MAP[props.status || ''] || 'info')
</script>
