/**
 * 后端 API 统一封装：前端页面只调用这里的函数，不直接写 fetch。
 *
 * 【零基础说明】
 * - 所有请求走相对路径 /api/*，由 vite 开发代理转发到 FastAPI(8000 端口)，
 *   生产环境由 nginx 做同样转发——前端代码不需要区分环境。
 * - streamChat() 是本项目的重点：SSE（服务器推送流）的浏览器端解析。
 *   EventSource 组件只支持 GET，我们的对话是 POST，所以用 fetch + 
 *   ReadableStream 手动解析（后端每条消息格式固定为 "data: {json}\n\n"）。
 */

// ============ 类型定义（与后端返回的 JSON 字段一一对应） ============

export interface Device {
  id: number
  code: string
  name: string
  type: 'plc' | 'spectro'
  host: string
  port: number
  group_count: number
}

export interface DashboardGroup {
  group_index: number
  ts: string | null
  speed_mmin: number | null
  temperature: number | null
  humidity: number | null
  roller_temp: number | null
  p_actual: number[] | null
  /** 初始压力：人工调试的固定基准（印刷结束后用它复位） */
  p_init: number[] | null
  p_set: number[] | null
  delta_e: number | null
  delta_d: number | null
  in_tolerance: boolean | null
}

export interface HistoryPoint {
  bucket: string
  [key: string]: string | number
}

export interface QualityRow {
  group_index: number
  samples: number
  delta_e_avg: number
  delta_e_max: number
  delta_d_avg: number
  tol_rate: number
}

export interface AlarmRow {
  id: number
  ts: string
  level: 'info' | 'warn' | 'error'
  group_index: number | null
  code: string
  message: string
}

export interface ActiveModel {
  name: string
  version: string
  algo: string
  r2: number | null
  mae: number | null
  train_rows: number
  metrics: Record<string, number> | null
  created_at: string
}

export interface AdjustResult {
  adjusted: Array<{
    group_index: number
    delta_e?: number
    old_setpoints?: number[]
    new_setpoints: number[]
    written: boolean
  }>
  message?: string
  tolerance: number
  dry_run: boolean
  speed_mmin?: number
  model_version?: string
}

export interface ResetResult {
  reset: Array<{
    group_index: number
    old_setpoints: number[]
    initial: number[]
    written: boolean
  }>
}

export interface SessionRow {
  id: number
  title: string
  created_at: string
}

/** 历史消息（meta 结构与存储时一致：sql/chart/citations） */
export interface HistoryMessage {
  role: 'user' | 'assistant'
  content: string
  meta: {
    sql?: { sql: string; ok: boolean; attempts: number; error?: string | null; columns?: string[]; rows?: Array<Array<string | number | null>>; note?: string }
    chart?: Record<string, unknown>
    citations?: Array<{ doc: string; section: string }>
  } | null
  created_at: string
}

/** SSE 每帧事件（后端 routes_chat.py 定义，字段按 type 区分，所以都是可选的） */
export interface ChatEvent {
  type: 'session' | 'node' | 'status' | 'citations' | 'sql' | 'table' | 'chart' | 'answer_delta' | 'answer' | 'done'
  session_id?: number
  name?: string
  text?: string
  items?: Array<{ doc: string; section: string }>
  sql?: string
  ok?: boolean
  attempts?: number
  error?: string | null
  columns?: string[]
  rows?: Array<Array<string | number | null>>
  note?: string
  option?: Record<string, unknown>
}

// ============ 普通REST接口 ============

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url)
  if (!resp.ok) throw new Error(`GET ${url} -> ${resp.status}`)
  return resp.json() as Promise<T>
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!resp.ok) {
    const detail = await resp.text()
    throw new Error(`POST ${url} -> ${resp.status}: ${detail.slice(0, 200)}`)
  }
  return resp.json() as Promise<T>
}

export const api = {
  health: () => get<{ status: string; db: boolean }>('/api/health'),
  devices: () => get<Device[]>('/api/devices'),
  dashboardLatest: () => get<DashboardGroup[]>('/api/dashboard/latest'),
  historyReadings: (groupIndex: number, minutes = 120, stepS = 60) =>
    get<HistoryPoint[]>(`/api/history/readings?group_index=${groupIndex}&minutes=${minutes}&step_s=${stepS}`),
  historyColors: (groupIndex: number, minutes = 120, stepS = 60) =>
    get<HistoryPoint[]>(`/api/history/colors?group_index=${groupIndex}&minutes=${minutes}&step_s=${stepS}`),
  qualityReport: (hours = 1) => get<QualityRow[]>(`/api/quality/report?hours=${hours}`),
  alarms: (limit = 50) => get<AlarmRow[]>(`/api/alarms?limit=${limit}`),
  collectStatus: (hours = 1) =>
    get<Array<{ status: string; batches: number; avg_ms: number }>>(`/api/collect/status?hours=${hours}`),
  activeModel: () => get<ActiveModel>('/api/model/active'),
  trainModel: (limit = 100000) =>
    post<{ version: string; r2: number; mae: number; train_rows: number }>('/api/model/train', { limit }),
  adjust: (payload: { group_indices?: number[] | null; tolerance: number; dry_run: boolean }) =>
    post<AdjustResult>('/api/control/adjust', payload),
  /** 压力复位：把设定值写回初始压力（group_indices 空=全部机组） */
  reset: (payload: { group_indices?: number[] | null; operator?: string }) =>
    post<ResetResult>('/api/control/reset', payload),
  // ---- 智能体长期记忆：会话列表与历史消息 ----
  chatSessions: () => get<SessionRow[]>('/api/chat/sessions'),
  chatHistory: (sessionId: number) =>
    get<HistoryMessage[]>(`/api/chat/history?session_id=${sessionId}`),
}

// ============ 智能体对话（SSE 流式） ============

/**
 * 发送一条对话消息，后端通过 SSE 持续推送事件，每收到一帧回调一次 onEvent。
 * 返回 session_id（多轮对话时传回去）。
 *
 * 解析原理：HTTP 响应体是一个持续的文本流，格式为若干 "data: {json}\n\n" 段；
 * 我们边读边按 "\n\n" 切帧，取出 data: 后面的 JSON 交给回调。
 */
export async function streamChat(
  message: string,
  sessionId: number | null,
  onEvent: (evt: ChatEvent) => void,
): Promise<number | null> {
  const resp = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId }),
  })
  if (!resp.ok || !resp.body) throw new Error(`chat stream -> ${resp.status}`)

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let sessionIdOut: number | null = null

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // 循环切帧：保证一次 read 里带了多帧（或半帧）时都能正确解析
    let sep: number
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const line = frame.split('\n').find((l) => l.startsWith('data: '))
      if (!line) continue
      const evt = JSON.parse(line.slice(6)) as ChatEvent
      if (evt.type === 'session' && evt.session_id) sessionIdOut = evt.session_id
      onEvent(evt)
    }
  }
  return sessionIdOut
}
