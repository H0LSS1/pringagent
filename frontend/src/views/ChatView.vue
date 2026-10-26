<script setup lang="ts">
/**
 * 工艺智能体对话页（SSE 流式 + 长期会话记忆）。
 *
 * 【两个关键机制】
 * 1. 长期记忆：会话存在 MySQL（按 session_id）。组件挂载时先查会话列表，
 *    自动恢复最近一次会话并从后端拉取完整历史（含 SQL/表格/图表/引用，
 *    都在消息的 meta 里，可完整还原）；localStorage 同时记住上次用的会话。
 *    切页面回来/刷新浏览器 => 对话还在。"新对话"按钮 => 清空本地状态从头开始。
 * 2. 流式输出：后端以 answer_delta 事件逐段推送回答文本，前端收到就追加渲染
 *    （打字机效果）；收到 answer 全量事件时直接覆盖（丢帧兜底）。
 */
import { nextTick, onMounted, ref } from 'vue'
import { api, streamChat, type ChatEvent, type HistoryMessage } from '@/api'
import EChart from '@/components/EChart.vue'

interface AssistantExtras {
  citations?: Array<{ doc: string; section: string }>
  sql?: { sql: string; ok: boolean; attempts: number; error?: string | null }
  table?: { columns: string[]; rows: Array<Array<string | number | null>>; note?: string }
  chart?: Record<string, unknown>
}

interface ChatMsg {
  role: 'user' | 'assistant'
  text: string
  streaming?: boolean
  extras?: AssistantExtras
}

const WELCOME: ChatMsg = {
  role: 'assistant',
  text: '你好，我是印刷工艺智能体。可以问我印刷标准知识（如"色差公差是多少"），'
    + '也可以让我查产线数据（如"查询1号机组最近1小时的ΔE趋势"）。',
}

const input = ref('')
const streaming = ref(false)
const sessionId = ref<number | null>(null)
const sessions = ref<Array<{ id: number; title: string; created_at: string }>>([])
const messages = ref<ChatMsg[]>([])
const listEl = ref<HTMLDivElement>()

const SESSION_KEY = 'printagent_chat_session'

function scrollBottom() {
  // nextTick：等 DOM 更新完再滚，才能滚到底
  nextTick(() => listEl.value?.scrollTo({ top: listEl.value.scrollHeight, behavior: 'smooth' }))
}

// ---------- 历史还原 ----------
function extrasFromMeta(meta: HistoryMessage['meta']): AssistantExtras | undefined {
  if (!meta) return undefined
  const extras: AssistantExtras = {}
  if (meta.citations?.length) extras.citations = meta.citations
  if (meta.sql?.sql) {
    extras.sql = { sql: meta.sql.sql, ok: meta.sql.ok, attempts: meta.sql.attempts, error: meta.sql.error }
    if (meta.sql.columns?.length) {
      extras.table = { columns: meta.sql.columns, rows: meta.sql.rows ?? [], note: meta.sql.note }
    }
  }
  if (meta.chart) extras.chart = meta.chart
  return Object.keys(extras).length ? extras : undefined
}

async function loadHistory(sid: number) {
  const history = await api.chatHistory(sid)
  messages.value = history.map((m: HistoryMessage) => ({
    role: m.role,
    text: m.content,
    extras: m.role === 'assistant' ? extrasFromMeta(m.meta) : undefined,
  }))
  if (!messages.value.length) messages.value.push({ ...WELCOME })
  scrollBottom()
}

async function switchSession(sid: number) {
  if (streaming.value) return
  sessionId.value = sid
  localStorage.setItem(SESSION_KEY, String(sid))
  try {
    await loadHistory(sid)
  } catch (e) {
    ElMessageError((e as Error).message)
  }
}

function newChat() {
  if (streaming.value) return
  sessionId.value = null
  localStorage.removeItem(SESSION_KEY)
  messages.value = [{ ...WELCOME }]
}

onMounted(async () => {
  try {
    sessions.value = await api.chatSessions()
    // 优先恢复上次用的会话；不存在（被清库/换浏览器）则退回最新会话
    const stored = Number(localStorage.getItem(SESSION_KEY))
    const known = sessions.value.find((s) => s.id === stored)
    const target = known ? stored : sessions.value[0]?.id ?? null
    if (target) {
      sessionId.value = target
      localStorage.setItem(SESSION_KEY, String(target))
      await loadHistory(target)
      return
    }
  } catch (e) {
    console.error('restore session failed', e)
  }
  messages.value = [{ ...WELCOME }]
})

// ---------- 发送与流式渲染 ----------
function tableRows(msg: ChatMsg): Array<Record<string, unknown>> {
  // el-table 需要对象数组：把 ["ts", "delta_e"] + [[v1, v2]] 拼成 {ts: v1, delta_e: v2}
  const t = msg.extras?.table
  if (!t) return []
  return t.rows.map((row) => Object.fromEntries(t.columns.map((c, i) => [c, row[i]])))
}

async function send() {
  const question = input.value.trim()
  if (!question || streaming.value) return
  input.value = ''
  streaming.value = true

  messages.value.push({ role: 'user', text: question })
  // 占位的 assistant 消息：SSE 事件逐段往里填
  const reply: ChatMsg = { role: 'assistant', text: '思考中…', streaming: true, extras: {} }
  messages.value.push(reply)
  scrollBottom()

  const onEvent = (evt: ChatEvent) => {
    switch (evt.type) {
      case 'status':
        // 还没有正文时，用状态文字占位；开始输出正文后状态会被覆盖
        if (!reply.text || reply.text.startsWith('⏳')) reply.text = `⏳ ${evt.text ?? ''}`
        break
      case 'citations':
        reply.extras = { ...reply.extras, citations: evt.items }
        break
      case 'sql':
        reply.extras = { ...reply.extras, sql: { sql: evt.sql ?? '', ok: !!evt.ok, attempts: evt.attempts ?? 1, error: evt.error } }
        break
      case 'table':
        reply.extras = { ...reply.extras, table: { columns: evt.columns ?? [], rows: evt.rows ?? [], note: evt.note } }
        break
      case 'chart':
        reply.extras = { ...reply.extras, chart: evt.option }
        break
      case 'answer_delta':
        // 流式核心：每帧追加一小段文本（首次追加时清掉"思考中"占位）
        if (reply.text.startsWith('⏳') || reply.text === '思考中…') reply.text = ''
        reply.text += evt.text ?? ''
        scrollBottom()
        break
      case 'answer':
        reply.text = evt.text ?? reply.text // 全量兜底覆盖
        reply.streaming = false
        break
      case 'done':
        reply.streaming = false
        break
      default:
        break
    }
  }

  try {
    sessionId.value = await streamChat(question, sessionId.value, onEvent)
    if (sessionId.value) localStorage.setItem(SESSION_KEY, String(sessionId.value))
    // 会话列表刷新（新会话/新标题）
    sessions.value = await api.chatSessions().catch(() => sessions.value)
  } catch (e) {
    reply.text = `请求失败：${(e as Error).message}`
  } finally {
    reply.streaming = false
    streaming.value = false
    scrollBottom()
  }
}

// 简单错误提示（避免为了一处 import 引入整个 ElMessage）
function ElMessageError(msg: string) {
  console.error(msg)
  messages.value.push({ role: 'assistant', text: `历史加载失败：${msg}` })
}
</script>

<template>
  <div class="chat-page">
    <!-- 会话工具条：历史会话切换 + 新对话 -->
    <div class="session-bar">
      <el-select
        :model-value="sessionId"
        placeholder="历史会话"
        size="small"
        style="width: 260px"
        @change="(sid: number) => switchSession(sid)"
      >
        <el-option v-for="s in sessions" :key="s.id" :value="s.id"
                   :label="`#${s.id} ${s.title}`" />
      </el-select>
      <el-button size="small" @click="newChat">＋ 新对话</el-button>
      <span class="hint">切换页面/刷新浏览器后对话自动恢复</span>
    </div>

    <div ref="listEl" class="msg-list">
      <div v-for="(msg, i) in messages" :key="i" class="msg" :class="msg.role">
        <div class="bubble">
          <span v-if="msg.streaming && !msg.text" class="thinking">思考中…</span>
          <span v-else class="text" style="white-space: pre-wrap">{{ msg.text }}</span>

          <!-- 知识引用（溯源卡片） -->
          <div v-if="msg.extras?.citations?.length" class="block">
            <el-divider style="margin: 8px 0" />
            <div class="label">📎 引用来源</div>
            <el-tag v-for="(c, ci) in msg.extras.citations" :key="ci" size="small" type="info" class="cite">
              {{ c.doc }} · {{ c.section }}
            </el-tag>
          </div>

          <!-- 生成的 SQL -->
          <div v-if="msg.extras?.sql" class="block">
            <el-collapse>
              <el-collapse-item :name="'sql'">
                <template #title>
                  <span class="label">
                    🗄️ SQL（执行{{ msg.extras.sql.ok ? '成功' : '失败' }}，尝试 {{ msg.extras.sql.attempts }} 次）
                  </span>
                </template>
                <pre class="sql">{{ msg.extras.sql.sql }}</pre>
                <div v-if="msg.extras.sql.error" class="error">{{ msg.extras.sql.error }}</div>
              </el-collapse-item>
            </el-collapse>
          </div>

          <!-- 查询结果表 -->
          <div v-if="msg.extras?.table?.rows?.length" class="block">
            <div class="label">📋 查询结果（{{ msg.extras.table.note }}）</div>
            <el-table :data="tableRows(msg)" size="small" max-height="240" border>
              <el-table-column
                v-for="col in msg.extras.table.columns" :key="col"
                :prop="col" :label="col" min-width="90" />
            </el-table>
          </div>

          <!-- ECharts 图表 -->
          <div v-if="msg.extras?.chart" class="block">
            <EChart :option="msg.extras.chart" height="260px" />
          </div>
        </div>
      </div>
    </div>

    <div class="input-bar">
      <el-input
        v-model="input"
        placeholder="试试：色差公差是多少？ / 查询2号机组最近1小时的ΔE趋势"
        :disabled="streaming"
        @keydown.enter="send"
      />
      <el-button type="primary" :disabled="streaming || !input.trim()" @click="send">发送</el-button>
    </div>
  </div>
</template>

<style scoped>
.chat-page { display: flex; flex-direction: column; height: calc(100vh - 130px); }
.session-bar {
  display: flex; align-items: center; gap: 8px;
  padding-bottom: 8px; border-bottom: 1px solid #e4e7ed;
}
.session-bar .hint { color: #909399; font-size: 12px; }
.msg-list { flex: 1; overflow-y: auto; padding: 8px 4px; }
.msg { display: flex; margin: 8px 0; }
.msg.user { justify-content: flex-end; }
.bubble {
  max-width: 78%; padding: 10px 12px; border-radius: 8px;
  background: #fff; border: 1px solid #e4e7ed;
}
.msg.user .bubble { background: #409eff; color: #fff; border: none; }
.thinking { color: #909399; }
.label { font-size: 12px; color: #909399; margin-bottom: 4px; }
.block { margin-top: 8px; }
.cite { margin-right: 6px; margin-bottom: 4px; }
.sql { font-family: Consolas, monospace; font-size: 12px; margin: 0; white-space: pre-wrap; }
.error { color: #f56c6c; font-size: 12px; margin-top: 4px; }
.input-bar { display: flex; gap: 8px; padding-top: 10px; border-top: 1px solid #e4e7ed; }
</style>
