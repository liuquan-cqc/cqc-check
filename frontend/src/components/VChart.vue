<template>
  <!-- 通用 ECharts 容器：按需注册图表类型，自动处理 resize 与销毁 -->
  <div ref="chartRef" class="v-chart" :style="{ height }"></div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, PieChart, LineChart, GraphChart, TreeChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  GraphicComponent
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

// 按需注册：后续页面需要其他图表类型时在此补充
echarts.use([
  BarChart,
  PieChart,
  LineChart,
  GraphChart,
  TreeChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  GraphicComponent,
  CanvasRenderer
])

const props = withDefaults(
  defineProps<{
    /** ECharts 配置项 */
    option: EChartsCoreOption
    /** 容器高度 */
    height?: string
  }>(),
  { height: '320px' }
)
const emit = defineEmits<{
  nodeClick: [data: Record<string, any>]
}>()

const chartRef = ref<HTMLElement>()
let chart: echarts.ECharts | null = null
let resizeObserver: ResizeObserver | null = null

/** 渲染 / 更新图表 */
function render() {
  if (!chartRef.value) return
  if (!chart) {
    chart = echarts.init(chartRef.value)
    chart.on('click', (params) => {
      if (params.data && typeof params.data === 'object') emit('nodeClick', params.data as Record<string, any>)
    })
  }
  chart.setOption(props.option, true)
}

onMounted(() => {
  render()
  // 容器尺寸变化（窗口缩放、侧边栏折叠）时自适应
  if (chartRef.value) {
    resizeObserver = new ResizeObserver(() => chart?.resize())
    resizeObserver.observe(chartRef.value)
  }
})

watch(
  () => props.option,
  () => render(),
  { deep: true }
)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<style scoped>
.v-chart {
  width: 100%;
}
</style>
