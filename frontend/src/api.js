// LLM Wiki 工作台 API 客户端（统一入口）
// 开发服务器通过 Vite 代理转发 /api，避免 localhost 与 127.0.0.1 的跨域差异。
const API_BASE = import.meta.env.VITE_API_BASE || '/api'

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

/** 401 时清会话并通知 App（只通知一次，避免并发请求刷屏）——见 App.vue 的监听。 */
let sessionExpiredNotified = false
function handleUnauthorized(path) {
  if (path.startsWith('/auth/login')) return          // 登录接口的 401 = 账号密码错，不是会话过期
  localStorage.removeItem('llmwiki_token')
  localStorage.removeItem('llmwiki_auth')
  if (sessionExpiredNotified) return
  sessionExpiredNotified = true
  window.dispatchEvent(new CustomEvent('llmwiki:unauthorized'))
}

export async function request(path, options = {}) {
  const token = localStorage.getItem('llmwiki_token')
  const isForm = options.body instanceof FormData
  const headers = { ...(options.body && !isForm ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  } catch (error) {
    throw new ApiError(0, '无法连接 API 服务，请确认 FastAPI 已启动。')
  }
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401) handleUnauthorized(path)
    const detail = payload.detail
    const message = response.status === 401 && !path.startsWith('/auth/login')
      ? '登录已过期，请重新登录。'
      : (typeof detail === 'object'
        ? detail.errors?.join('；') || detail.message || JSON.stringify(detail)
        : detail || '请求失败')
    throw new ApiError(response.status, message)
  }
  return payload
}

const qs = (params) => {
  const usable = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '')
  return usable.length ? '?' + new URLSearchParams(usable).toString() : ''
}

export const api = {
  // ---- 认证 ----
  login: (username, password) => request('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),

  // ---- 知识层：条目与检索 ----
  entries: (params) => request(`/entries${qs(params)}`),
  myEntries: (limit = 200) => request(`/entries/mine${qs({ limit })}`),
  entryContent: (path) => request(`/entries/content${qs({ path })}`),
  search: (query, mode) => request(`/search${qs({ query, mode })}`),
  // 对话窗口：检索 + 生成带引用溯源的答案（无依据时如实说明并记为知识缺口）
  ask: (question, topK = 6) => request('/ask', {
    method: 'POST', body: JSON.stringify({ question, top_k: topK }),
  }),
  // 知识图谱：节点（条目 + 知识库保留文件）+ 边（related_to / [[wikilink]] / 链接）+ 待建页面
  graph: (opts = {}) => request(`/graph${qs({
    include_pending: opts.includePending ?? true,
    include_meta: opts.includeMeta ?? true,
    include_raw: opts.includeRaw ?? false,
  })}`),
  graphNeighbors: (path) => request(`/graph/neighbors${qs({ path })}`),
  searchStats: () => request('/search/stats'),
  searchMissed: (limit = 20) => request(`/search/missed${qs({ limit })}`),

  // ---- 知识层：上传与编译任务 ----
  listTasks: (limit = 50) => request(`/uploads/tasks${qs({ limit })}`),
  retryTask: (id) => request(`/uploads/tasks/${id}/retry`, { method: 'POST' }),
  upload: (formData) => request('/uploads', { method: 'POST', body: formData }),

  // ---- 知识层：审核 ----
  reviewsPending: () => request('/reviews/pending'),
  reviewsRejected: () => request('/reviews/rejected'),
  approve: (id) => request(`/reviews/${id}/approve`, { method: 'POST' }),
  reject: (id, reason) => request(`/reviews/${id}/reject`, { method: 'POST', body: JSON.stringify({ reason }) }),
  resubmit: (id) => request(`/reviews/${id}/resubmit`, { method: 'POST' }),
  retryAi: (id) => request(`/reviews/${id}/retry-ai`, { method: 'POST' }),

  // ---- 管理：可观测性与周报 ----
  observability: () => request('/admin/observability'),
  reports: (kind) => request(`/admin/reports${qs({ kind })}`),
  rebuildIndex: () => request('/admin/rebuild-index', { method: 'POST' }),
  backfillEmbeddings: (batch = 10) => request(`/admin/backfill-embeddings${qs({ batch })}`, { method: 'POST' }),

  // ---- 客户别名（中文简称 → 脱敏代号）----
  aliases: () => request('/customers/aliases'),
  createAlias: (alias, customerId) => request('/customers/aliases', {
    method: 'POST', body: JSON.stringify({ alias, customer_id: customerId }),
  }),
  deleteAlias: (alias, customerId) => request(`/customers/aliases${qs({ alias, customer_id: customerId })}`, { method: 'DELETE' }),

  // ---- 销售域：事实澄清 ----
  intake: (body) => request('/clarifications/intake', { method: 'POST', body: JSON.stringify(body) }),
  mine: (includeDeleted = false) => request(`/clarifications/mine${includeDeleted ? '?include_deleted=true' : ''}`),
  sessions: () => request('/clarifications/sessions'),
  // advance=true：会话为 open 时先推进一轮（调模型）再返回，工作台无需额外调用
  session: (id, advance = false) => request(`/clarifications/sessions/${id}${advance ? '?advance=true' : ''}`),
  advanceSession: (id) => request(`/clarifications/sessions/${id}/advance`, { method: 'POST' }),
  answer: (id, body) => request(`/clarifications/sessions/${id}/answers`, { method: 'POST', body: JSON.stringify(body) }),
  // 人工处置转人工会话：closed（关闭，带原因）/ reopened（补充事实后重开继续）
  resolveSession: (id, body) => request(`/clarifications/sessions/${id}/resolve`, { method: 'POST', body: JSON.stringify(body) }),
  // 把澄清结论转成待确认状态建议（确定性规则；建议不修改客户状态）
  generateProposal: (id) => request(`/clarifications/sessions/${id}/proposal`, { method: 'POST' }),
  // 归档（软删除）澄清会话与建议——仅管理员；恢复同样仅管理员
  deleteSession: (id) => request(`/clarifications/sessions/${id}`, { method: 'DELETE' }),
  restoreSession: (id) => request(`/clarifications/sessions/${id}/restore`, { method: 'POST' }),

  // ---- 销售域：客户状态 ----
  proposals: () => request('/customer-states/proposals/pending'),
  // 客户当前阶段总览（确认后的结果在这里可见）
  customers: () => request('/customer-states/customers'),
  decide: (id, body) => request(`/customer-states/proposals/${id}/decision`, { method: 'POST', body: JSON.stringify(body) }),
  events: (customerId) => request(`/customer-states/${encodeURIComponent(customerId)}/events`),
}

// 中文枚举映射（多处复用，避免各页重复定义）
export const STAGE_ORDER = ['已发布', '待审核', '已编译', '编译中', '编译失败', '已驳回', '已上传']
export const STAGE_LABELS = {
  已发布: '已发布', 待审核: '待审核', 已编译: '已编译', 编译中: '编译中',
  编译失败: '编译失败', 已驳回: '已驳回', 已上传: '已上传',
}
export const STAGE_HINT = {
  已发布: '已通过审核进入企业知识库，全员可检索',
  待审核: '编译产物等待 AI 六维度审核或人工放行',
  已编译: '编译已完成，等待进入审核队列',
  编译中: 'Claude Code 正在处理（触发队列 watcher）',
  编译失败: '编译中断，可在上传页重试',
  已驳回: '审核未通过，可修改后重新提交',
  已上传: '已落盘 RAW/，尚未开始编译',
}
export const STATE_LABELS = {
  new_lead: '新线索', contacted: '已接触', need_confirmed: '需求已确认',
  solution_eval: '方案评估', commercial_negotiation: '商务谈判',
  won: '已赢单', lost_or_paused: '流失/暂停', expired: '已过期',
}
export const SESSION_STATUS_LABELS = {
  open: '等待澄清', needs_human_review: '转人工审核', ready_for_proposal: '可生成建议',
  completed: '已完成', cancelled: '已取消',
  // 轮次（turn）状态词表与会话不同，展示时统一到同一套中文
  needs_clarification: '等待澄清', human_review: '转人工审核',
  insufficient_evidence: '证据不足',
}
export const ROLE_LABELS = { admin: '管理员', reviewer: '审核者', user: '普通用户' }
export const ENTRY_TYPE_LABELS = { concept: '概念', resource: '资源', research: '研究', glossary: '术语' }
export const ENTRY_STATUS_LABELS = { draft: '草稿', pending: '待审', active: '已发布', stale: '过期', deprecated: '废弃' }
