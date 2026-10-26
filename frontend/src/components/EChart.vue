<script setup lang="ts">
/**
 * ECharts 通用包装组件：页面只管给 option（图表配置），其余全交给它。
 *
 * 【零基础说明】
 * - echarts.init(容器) 创建图表实例；setOption(option) 按配置渲染；
 *   容器尺寸变化时必须调 resize()，否则图会错位/截断——用 ResizeObserver 自动监听。
 * - watch 监听父组件传入的 option，变了就 setOption(opt, true) 全量刷新
 *   （第二个参数 true = 不合并旧配置，避免残留旧系列）。
 */
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    option: Record<string, unknown>
    height?: string
  }>(),
  { height: '300px' },
)

const el = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!el.value) return
  chart = echarts.init(el.value)
  chart.setOption(props.option)
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(el.value)
})

watch(
  () => props.option,
  (opt) => chart?.setOption(opt, true),
  { deep: true },
)

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="el" :style="{ height, width: '100%' }"></div>
</template>
