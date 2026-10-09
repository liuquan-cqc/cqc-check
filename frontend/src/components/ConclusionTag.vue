<template>
  <!-- 审核结论标签：合格→绿；需修改→橙红；待人工复核→蓝灰 -->
  <el-tag v-if="conclusion" :type="tagType" size="small" round>{{ conclusion }}</el-tag>
  <span v-else class="conclusion-empty">-</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  /** 审核结论，如 "合格" / "需修改3处" / "待人工复核1处" */
  conclusion: string | null | undefined
}>()

const tagType = computed<'success' | 'warning' | 'danger' | 'info'>(() => {
  const text = props.conclusion || ''
  if (text.includes('合格')) return 'success'
  // 从 "需修改N处" 中提取数量，超过 5 处标红
  const match = text.match(/需修改\s*(\d+)\s*处/)
  if (match) {
    return Number(match[1]) > 5 ? 'danger' : 'warning'
  }
  if (text.includes('需修改') || text.includes('不通过')) return 'warning'
  if (text.includes('待人工复核')) return 'info'
  return 'info'
})
</script>

<style scoped>
.conclusion-empty {
  color: var(--el-text-color-secondary);
}
</style>
