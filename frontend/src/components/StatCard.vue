<template>
  <!-- 统计卡片：图标 + 数值 + 标题，支持副信息插槽 -->
  <div class="ccc-card stat-card">
    <div class="stat-icon" :style="{ backgroundColor: color + '1a', color }">
      <el-icon :size="24"><component :is="icon" /></el-icon>
    </div>
    <div class="stat-body">
      <div class="stat-title">{{ title }}</div>
      <div class="stat-value">{{ displayValue }}</div>
      <div v-if="$slots.extra" class="stat-extra"><slot name="extra" /></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, type Component } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 卡片标题 */
    title: string
    /** 数值（数字自动千分位格式化） */
    value: number | string
    /** 图标组件（element-plus 图标） */
    icon: Component
    /** 主题色，默认主色蓝 */
    color?: string
  }>(),
  { color: '#2563eb' }
)

const displayValue = computed(() =>
  typeof props.value === 'number' ? props.value.toLocaleString('zh-CN') : props.value
)
</script>

<style scoped>
.stat-card {
  display: flex;
  align-items: center;
  gap: 16px;
}

.stat-icon {
  width: 52px;
  height: 52px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-body {
  min-width: 0;
}

.stat-title {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}

.stat-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.2;
}

.stat-extra {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
