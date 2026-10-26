<script setup lang="ts">
/**
 * 监控大屏（博泰实习项目的 Web 监控前端复现）。
 *
 * 数据流：
 *   - 快照类数据（机组最新状态/告警）：每 3 秒轮询一次（生产环境可用 WebSocket）
 *   - 曲线类数据（历史趋势）：每 15 秒刷新一次（后端已做分桶降采样）
 * 图表全部通过 <EChart> 组件渲染，本文件只负责"取数 + 拼 ECharts option"。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, type AlarmRow, type DashboardGroup, type Device, type HistoryPoint, type QualityRow } from '@/api'
import EChart from '@/components/EChart.vue'

// ---------- 状态 ----------
const devices = ref<Device[]>([])
const groups = ref<DashboardGroup[]>([])
const quality = ref<QualityRow[]>([])
const alarms = ref<AlarmRow[]>([])
const collectOk = ref<number | null>(null) // 近1小时采集成功率(%)
const groupCount = ref(10)
const selectedGroup = ref(0)
const colorHistory = ref<HistoryPoint[]>([])
const readingHistory = ref<HistoryPoint[]>([])
const selectedZone = ref(1) // 压力曲线看哪个模块（1~4）

let timerSnapshot: number | undefined
let timerChart: number | undefined

// ---------- 取数 ----------
async function loadSnapshot() {
  try {
    groups.value = await api.dashboardLatest()
    alarms.value = await api.alarms(30)
  } catch (e) {
    console.error('snapshot failed', e)
  }
}

async function loadCharts() {
  try {
    // 只看最近 5 分钟：step_s=5（一条数据一个桶），x 轴带秒
    const [colors, readings, q, cs] = await Promise.all([
      api.historyColors(selectedGroup.value, 5, 5),
      api.historyReadings(selectedGroup.value, 5, 5),
      api.qualityReport(1),
      api.collectStatus(1),
    ])
    colorHistory.value = colors
    readingHistory.value = readings
    quality.value = q
    // 采集成功率 = ok 批次 / 总批次
    const total = cs.reduce((s, r) => s + r.batches, 0)
    const ok = cs.find((r) => r.status === 'ok')?.batches ?? 0
    collectOk.value = total ? Math.round((ok / total) * 1000) / 10 : null
  } catch (e) {
    console.error('charts failed', e)
  }
}

async function loadDevices() {
  devices.value = await api.devices()
  const plc = devices.value.find((d) => d.type === 'plc')
  if (plc) groupCount.value = plc.group_count
}

onMounted(async () => {
  await loadDevices()
  await loadSnapshot()
  await loadCharts()
  timerSnapshot = window.setInterval(loadSnapshot, 3000)
  timerChart = window.setInterval(loadCharts, 5000) // 曲线 5 秒刷新一次（与采集周期同步）
})
onBeforeUnmount(() => {
  // 页面切换时清掉定时器，避免后台继续请求（内存泄漏的常见来源）
  if (timerSnapshot) clearInterval(timerSnapshot)
  if (timerChart) clearInterval(timerChart)
})

// ---------- 顶部统计卡片（computed：依赖变了自动重算） ----------
const avgDeltaE = computed(() => {
  if (!quality.value.length) return '-'
  const s = quality.value.reduce((sum, r) => sum + r.delta_e_avg * r.samples, 0)
  const n = quality.value.reduce((sum, r) => sum + r.samples, 0)
  return n ? (s / n).toFixed(2) : '-'
})
const outOfTol = computed(() => groups.value.filter((g) => g.in_tolerance === false).length)
const speed = computed(() => groups.value[0]?.speed_mmin?.toFixed(0) ?? '-')

// ---------- 图表配置 ----------
const deltaEOption = computed(() => ({
  // 布局口径：标题占第一行，图例下移到第二行，绘图区再往下——避免重叠
  title: { text: `ΔE 趋势（${selectedGroup.value + 1}号机组，近5分钟）`, left: 'center', top: 0, textStyle: { fontSize: 13 } },
  tooltip: { trigger: 'axis' },
  legend: { top: 30 },
  grid: { top: 66, left: 45, right: 45, bottom: 30 },
  xAxis: { type: 'category', data: colorHistory.value.map((r) => String(r.bucket).slice(11, 19)) },
  yAxis: [
    { type: 'value', name: 'ΔE', min: 0 },
    { type: 'value', name: '合格率', max: 1, axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%` } },
  ],
  series: [
    { name: 'ΔE均值', type: 'line', data: colorHistory.value.map((r) => r.delta_e), smooth: true, showSymbol: false },
    { name: 'ΔE最大', type: 'line', data: colorHistory.value.map((r) => r.delta_e_max), lineStyle: { type: 'dashed' }, showSymbol: false },
    { name: '合格率', type: 'line', yAxisIndex: 1, data: colorHistory.value.map((r) => r.tol_rate), showSymbol: false },
  ],
}))

const pressureOption = computed(() => {
  const z = selectedZone.value
  return {
    title: { text: `压力曲线（${selectedGroup.value + 1}号机组 · ${z}号模块，近5分钟）`, left: 'center', top: 0, textStyle: { fontSize: 13 } },
    tooltip: { trigger: 'axis' },
    legend: { top: 30 },
    grid: { top: 66, left: 50, right: 30, bottom: 30 },
    xAxis: { type: 'category', data: readingHistory.value.map((r) => String(r.bucket).slice(11, 19)) },
    yAxis: { type: 'value', name: 'kN/m', min: -5, max: 5 },
    series: [
      { name: '实际值', type: 'line', data: readingHistory.value.map((r) => r[`p_actual_${z}`]), showSymbol: false },
      { name: '初始值（基准）', type: 'line', data: readingHistory.value.map((r) => r[`p_init_${z}`]), lineStyle: { type: 'dashed' }, showSymbol: false },
    ],
  }
})
</script>

<template>
  <div>
    <!-- 顶部统计卡片 -->
    <el-row :gutter="12">
      <el-col :span="6"><el-card shadow="never"><el-statistic title="当前车速" :value="speed" suffix=" m/min" /></el-card></el-col>
      <el-col :span="6"><el-card shadow="never"><el-statistic title="平均 ΔE（近1h）" :value="avgDeltaE" /></el-card></el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <el-statistic title="超差机组（实时）" :value="outOfTol" :value-style="{ color: outOfTol ? '#f56c6c' : '#67c23a' }" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <el-statistic title="采集成功率（近1h）" :value="collectOk === null ? '-' : collectOk" suffix="%" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 曲线区 -->
    <el-row :gutter="12" class="mt">
      <el-col :span="12">
        <el-card shadow="never">
          <div class="toolbar">
            <span>机组：</span>
            <el-select v-model="selectedGroup" style="width: 110px" @change="loadCharts">
              <el-option v-for="g in groupCount" :key="g" :value="g - 1" :label="`${g}号机组`" />
            </el-select>
          </div>
          <EChart :option="deltaEOption" height="280px" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <div class="toolbar">
            <span>压力模块：</span>
            <el-select v-model="selectedZone" style="width: 110px">
              <el-option v-for="z in 4" :key="z" :value="z" :label="`${z}号模块`" />
            </el-select>
          </div>
          <EChart :option="pressureOption" height="280px" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 机组实时状态表 -->
    <el-card shadow="never" class="mt" header="机组实时状态（3秒刷新）">
      <el-table :data="groups" size="small" stripe>
        <el-table-column label="机组" width="90">
          <template #default="{ row }">{{ row.group_index + 1 }}号</template>
        </el-table-column>
        <el-table-column label="色差 ΔE" width="110">
          <template #default="{ row }">
            <el-tag :type="row.in_tolerance === false ? 'danger' : 'success'" size="small">
              {{ row.delta_e ?? '-' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="密度差 ΔD" width="100">
          <template #default="{ row }">{{ row.delta_d ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="实际压力 kN/m" min-width="180">
          <template #default="{ row }">{{ (row.p_actual ?? []).join(' / ') }}</template>
        </el-table-column>
        <el-table-column label="初始压力 kN/m" min-width="180">
          <template #default="{ row }">{{ (row.p_init ?? []).join(' / ') }}</template>
        </el-table-column>
        <el-table-column label="墨辊温度 ℃" width="110">
          <template #default="{ row }">{{ row.roller_temp ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="ts" label="更新时间" width="170" />
      </el-table>
    </el-card>

    <!-- 告警 -->
    <el-card shadow="never" class="mt" header="最新告警">
      <el-table :data="alarms" size="small" stripe>
        <el-table-column prop="ts" label="时间" width="180" />
        <el-table-column label="级别" width="80">
          <template #default="{ row }">
            <el-tag :type="row.level === 'warn' ? 'warning' : 'info'" size="small">{{ row.level }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="内容" min-width="300" />
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.mt { margin-top: 12px; }
.toolbar { margin-bottom: 4px; display: flex; align-items: center; gap: 8px; }
</style>
