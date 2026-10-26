<script setup lang="ts">
/**
 * 压力管理页：模型管理 + 闭环调节 + 压力对比图。
 *
 * 三个功能块：
 * 1. 模型卡片：显示当前激活的压力预测模型（版本/R²/MAE），可一键重新训练
 * 2. 闭环调节：设置 ΔE 公差 → 点击"自动调节" → 后端实时读 PLC/分光光度计、
 *    预测应恢复的初始压力并写回设定寄存器（dry_run 模式只预测不写，联调安全）
 * 3. 压力对比柱状图：当前机组的 实际/标准/设定 四模块对比，直观看到偏差
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type ActiveModel, type AdjustResult, type DashboardGroup, type ResetResult } from '@/api'
import EChart from '@/components/EChart.vue'

// ---------- 状态 ----------
const groups = ref<DashboardGroup[]>([])
const selectedGroup = ref(0)
const model = ref<ActiveModel | null>(null)
const training = ref(false)
const adjusting = ref(false)
const resetting = ref(false)
const tolerance = ref(1.5)
const dryRun = ref(false)
const adjustResult = ref<AdjustResult | null>(null)
const resetResult = ref<ResetResult | null>(null)

let timer: number | undefined

async function loadSnapshot() {
  groups.value = await api.dashboardLatest()
}
async function loadModel() {
  try {
    model.value = await api.activeModel()
  } catch {
    model.value = null // 尚未训练过模型：页面显示"未训练"
  }
}

onMounted(async () => {
  await Promise.all([loadSnapshot(), loadModel()])
  timer = window.setInterval(loadSnapshot, 5000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

const current = computed(() => groups.value.find((g) => g.group_index === selectedGroup.value))

// ---------- 动作 ----------
async function train() {
  training.value = true
  try {
    const res = await api.trainModel(100000)
    ElMessage.success(`模型 ${res.version} 训练完成：R²=${res.r2.toFixed(4)}, MAE=${res.mae.toFixed(4)}`)
    await loadModel()
  } catch (e) {
    ElMessage.error(`训练失败：${(e as Error).message}`)
  } finally {
    training.value = false
  }
}

async function adjust() {
  adjusting.value = true
  adjustResult.value = null
  try {
    // group_indices 不传 => 后端自动选 ΔE 超差机组
    adjustResult.value = await api.adjust({ tolerance: tolerance.value, dry_run: dryRun.value })
    if (adjustResult.value.message) {
      ElMessage.info(adjustResult.value.message)
    } else {
      ElMessage.success(`已调节 ${adjustResult.value.adjusted.length} 个机组${dryRun.value ? '（演练，未下发）' : ''}`)
    }
    await loadSnapshot()
  } catch (e) {
    ElMessage.error(`调节失败：${(e as Error).message}`)
  } finally {
    adjusting.value = false
  }
}

/** 压力复位：把设定值写回初始压力（印刷结束恢复初始状态）。group_indices=null 表示全部机组 */
async function resetPressure(groupIndices: number[] | null) {
  const label = groupIndices ? `${groupIndices.map((g) => g + 1).join('、')}号机组` : '全部机组'
  try {
    await ElMessageBox.confirm(`确定把 ${label} 的压力重置为初始值？`, '压力复位', { type: 'warning' })
  } catch {
    return // 用户点了取消
  }
  resetting.value = true
  resetResult.value = null
  try {
    resetResult.value = await api.reset({ group_indices: groupIndices })
    ElMessage.success(`已重置 ${resetResult.value.reset.length} 个机组`)
    await loadSnapshot()
  } catch (e) {
    ElMessage.error(`复位失败：${(e as Error).message}`)
  } finally {
    resetting.value = false
  }
}

// ---------- 对比图：初始值（人工基准） vs 实际值（含模型调节效果） ----------
const compareOption = computed(() => {
  const c = current.value
  const zones = ['1号', '2号', '3号', '4号']
  return {
    title: {
      text: c ? `${c.group_index + 1}号机组 压力对比（实时）` : '加载中...',
      left: 'center', top: 0, textStyle: { fontSize: 13 },
    },
    tooltip: { trigger: 'axis' },
    legend: { top: 30 },
    grid: { top: 66, left: 50, right: 30, bottom: 30 },
    xAxis: { type: 'category', data: zones },
    yAxis: { type: 'value', name: 'kN/m', min: -5, max: 5 },
    series: [
      { name: '实际值', type: 'bar', data: c?.p_actual ?? [], itemStyle: { color: '#409eff' } },
      { name: '初始值（基准）', type: 'bar', data: c?.p_init ?? [], itemStyle: { color: '#67c23a' } },
    ],
  }
})
</script>

<template>
  <div>
    <el-row :gutter="12">
      <!-- 模型管理 -->
      <el-col :span="8">
        <el-card shadow="never" header="压力预测模型">
          <div class="fill">
            <div>
              <template v-if="model">
                <el-descriptions :column="1" size="small" border>
                  <el-descriptions-item label="版本">{{ model.version }}</el-descriptions-item>
                  <el-descriptions-item label="算法">{{ model.algo }}</el-descriptions-item>
                  <el-descriptions-item label="R²">{{ model.r2 }}</el-descriptions-item>
                  <el-descriptions-item label="MAE">{{ model.mae }} kN/m</el-descriptions-item>
                  <el-descriptions-item label="训练样本">{{ model.train_rows }}</el-descriptions-item>
                </el-descriptions>
                <div class="feature-note">
                  特征：车速 / 温度 / 湿度 / 机组号 / 模块号 / ΔE / ΔD →
                  输出 4 个压力模块的目标压力
                </div>
              </template>
              <el-empty v-else description="尚未训练模型" :image-size="60" />
            </div>
            <el-button type="primary" class="full" :loading="training" @click="train">
              {{ training ? '训练中...' : '重新训练（10万样本）' }}
            </el-button>
          </div>
        </el-card>
      </el-col>

      <!-- 闭环调节 -->
      <el-col :span="8">
        <el-card shadow="never" header="压力闭环调节（模型 → PLC）">
          <div class="fill">
            <div>
              <el-form label-position="left" label-width="80px" size="small">
                <el-form-item label="ΔE 公差">
                  <el-input-number v-model="tolerance" :min="0.5" :max="5" :step="0.1" style="width: 100%" />
                </el-form-item>
                <el-form-item label="演练模式">
                  <div class="inline">
                    <el-switch v-model="dryRun" />
                    <span class="hint">开 = 只预测不写 PLC</span>
                  </div>
                </el-form-item>
              </el-form>
              <div class="hint block-note">不指定机组时，自动选择 ΔE 超差的机组进行调节</div>
            </div>
            <div>
              <el-button type="warning" class="full" :loading="adjusting" @click="adjust">
                {{ dryRun ? '演练：预测超差机组' : '自动调节超差机组' }}
              </el-button>
              <el-alert v-if="adjustResult?.message" :title="adjustResult.message" type="info" :closable="false" class="mt8" />
              <div v-for="a of adjustResult?.adjusted ?? []" :key="a.group_index" class="adjust-row">
                <el-tag size="small">{{ a.group_index + 1 }}号机组</el-tag>
                <span class="hint">ΔE={{ a.delta_e?.toFixed(2) }} 设定 {{ a.old_setpoints?.join('/') }} → <b>{{ a.new_setpoints.join('/') }}</b></span>
                <el-tag :type="a.written ? 'success' : 'info'" size="small">{{ a.written ? '已下发' : '未下发' }}</el-tag>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 初始值 & 复位 -->
      <el-col :span="8">
        <el-card shadow="never" header="初始压力与复位">
          <div class="toolbar">
            <span>查看机组：</span>
            <el-select v-model="selectedGroup" style="width: 110px">
              <el-option v-for="g in groups" :key="g.group_index" :value="g.group_index" :label="`${g.group_index + 1}号机组`" />
            </el-select>
          </div>
          <EChart :option="compareOption" height="260px" />
          <div class="btn-row">
            <el-button type="success" :loading="resetting" @click="resetPressure([selectedGroup])">
              重置当前机组
            </el-button>
            <el-button type="success" plain :loading="resetting" @click="resetPressure(null)">
              重置全部机组
            </el-button>
          </div>
          <div v-for="r of resetResult?.reset ?? []" :key="r.group_index" class="adjust-row">
            <el-tag size="small">{{ r.group_index + 1 }}号机组</el-tag>
            <span class="hint">{{ r.old_setpoints.join('/') }} → <b>{{ r.initial.join('/') }}</b></span>
            <el-tag :type="r.written ? 'success' : 'danger'" size="small">{{ r.written ? '已复位' : '失败' }}</el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 全部机组一览（同监控页的精简版） -->
    <el-card shadow="never" class="mt" header="全部机组（5秒刷新）">
      <el-table :data="groups" size="small" stripe>
        <el-table-column label="机组" width="80">
          <template #default="{ row }">{{ row.group_index + 1 }}号</template>
        </el-table-column>
        <el-table-column label="ΔE" width="90">
          <template #default="{ row }">
            <el-tag :type="row.in_tolerance === false ? 'danger' : 'success'" size="small">{{ row.delta_e ?? '-' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column v-for="z in 4" :key="z" :label="`${z}号模块 实际/初始`" min-width="150">
          <template #default="{ row }">
            {{ row.p_actual?.[z - 1] ?? '-' }} / {{ row.p_init?.[z - 1] ?? '-' }}
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.mt { margin-top: 12px; }
.mt8 { margin-top: 8px; }
.toolbar { margin-bottom: 4px; display: flex; align-items: center; gap: 8px; }
.hint { color: #909399; font-size: 12px; margin: 0 6px; }
.inline { display: flex; align-items: center; }
.adjust-row { display: flex; align-items: center; gap: 6px; margin-top: 6px; flex-wrap: wrap; }

/* 按钮统一全宽/等分，视觉上与卡片内容对齐 */
.full { width: 100%; }
.btn-row { display: flex; gap: 8px; margin-top: 8px; }
.btn-row .el-button { flex: 1; }

/* 卡片内容上下分布：顶部信息、底部动作按钮，消除底部大片空白 */
.fill {
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 12px;
}
.feature-note {
  margin-top: 8px; font-size: 12px; color: #909399; line-height: 1.6;
}
.block-note { margin-top: -6px; }

/* 三张卡片等高：列变 flex 容器，卡片撑满列高并纵向排布内容 */
.el-col { display: flex; }
.el-col > :deep(.el-card) {
  flex: 1;
  width: 100%;
  display: flex;
  flex-direction: column;
}
.el-col > :deep(.el-card) > :deep(.el-card__body) { flex: 1; }
</style>
