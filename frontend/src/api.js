// 开发服务器通过 Vite 代理转发 /api，避免 localhost 与 127.0.0.1 的跨域差异。
const API_BASE = import.meta.env.VITE_API_BASE || '/api'

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

export async function request(path, options = {}) {
  const token = localStorage.getItem('llmwiki_token')
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  } catch (error) {
    throw new ApiError(0, '无法连接 API 服务，请确认 FastAPI 已启动。')
  }
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload.detail
    const message = typeof detail === 'object' ? detail.errors?.join('；') || detail.message : detail || '请求失败'
    throw new ApiError(response.status, message)
  }
  return payload
}

export const api = {
  login: (username, password) => request('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  intake: (body) => request('/clarifications/intake', { method: 'POST', body: JSON.stringify(body) }),
  mine: () => request('/clarifications/mine'),
  sessions: () => request('/clarifications/sessions'),
  session: (id) => request(`/clarifications/sessions/${id}`),
  answer: (id, body) => request(`/clarifications/sessions/${id}/answers`, { method: 'POST', body: JSON.stringify(body) }),
  proposals: () => request('/customer-states/proposals/pending'),
  decide: (id, body) => request(`/customer-states/proposals/${id}/decision`, { method: 'POST', body: JSON.stringify(body) }),
  events: (customerId) => request(`/customer-states/${encodeURIComponent(customerId)}/events`),
}
