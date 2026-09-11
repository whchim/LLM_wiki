<script setup>
import { computed, onMounted, ref } from 'vue'
import { ArrowUpRight, Check, ChevronRight, CircleAlert, Clock3, FileText, LayoutDashboard, LogOut, MessageSquareMore, RefreshCw, ShieldCheck, Sparkles, UserRound } from 'lucide-vue-next'
import { api, ApiError } from './api'

const auth = ref(JSON.parse(localStorage.getItem('llmwiki_auth') || 'null'))
const view = ref('overview')
const loading = ref(false)
const error = ref('')
const notice = ref('')
const sessions = ref([])
const proposals = ref([])
const selectedSession = ref(null)
const selectedProposal = ref(null)
const answerDrafts = ref({})
const loginForm = ref({ username: 'admin', password: 'admin123' })
const intakeForm = ref({ customer_id: '', idempotency_key: '', occurred_at: new Date().toISOString().slice(0, 16), source_type: 'meeting_note', content: '' })

const isReviewer = computed(() => ['admin', 'reviewer'].includes(auth.value?.role))
const roleLabel = computed(() => ({ admin: '管理员', reviewer: '审核者', user: '销售' }[auth.value?.role] || '访客'))
const statusLabel = (status) => ({ open: '等待澄清', needs_human_review: '转人工审核', ready_for_proposal: '可生成建议', completed: '已完成', cancelled: '已取消' }[status] || status)
const stateLabel = (state) => ({ new_lead: '新线索', contacted: '已接触', need_confirmed: '需求已确认', solution_eval: '方案评估', commercial_negotiation: '商务谈判', won: '已赢单', lost_or_paused: '流失 / 暂停', expired: '已过期' }[state] || state || '未确认')

async function run(task) {
  loading.value = true; error.value = ''; notice.value = ''
  try { await task() } catch (err) { error.value = err instanceof ApiError ? err.message : '操作失败，请稍后重试。' } finally { loading.value = false }
}

async function loadData() {
  await run(async () => {
    sessions.value = await api.mine()
    if (isReviewer.value) proposals.value = await api.proposals()
    // 首次加载只选中第一条会话，不强制切换页面，避免登录后跳过总览。
    if (!selectedSession.value && sessions.value[0]) selectedSession.value = await api.session(sessions.value[0].session_id)
  })
}

async function login() {
  await run(async () => {
    const result = await api.login(loginForm.value.username, loginForm.value.password)
    auth.value = { token: result.access_token, username: loginForm.value.username, role: result.role, display_name: result.display_name }
    localStorage.setItem('llmwiki_token', result.access_token); localStorage.setItem('llmwiki_auth', JSON.stringify(auth.value)); view.value = 'overview'; await loadData()
  })
}

function logout() { localStorage.removeItem('llmwiki_token'); localStorage.removeItem('llmwiki_auth'); auth.value = null; selectedSession.value = null }

async function submitIntake() {
  await run(async () => {
    await api.intake({ ...intakeForm.value, occurred_at: new Date(intakeForm.value.occurred_at).toISOString() })
    notice.value = '纪要已通过门禁并创建澄清会话。'; intakeForm.value.content = ''; await loadData(); view.value = 'sales'
  })
}

async function openSession(id) { selectedSession.value = await api.session(id); view.value = 'sales' }
async function sendAnswer(turn, question) {
  const value = (answerDrafts.value[question.id] || '').trim(); if (!value) return
  await run(async () => { await api.answer(selectedSession.value.session_id, { turn_id: turn.turn_id, question_id: question.id, answer_text_redacted: value }); notice.value = '回答已追加保存。'; answerDrafts.value[question.id] = ''; await openSession(selectedSession.value.session_id); await loadData() })
}
async function openProposal(proposal) {
  selectedProposal.value = { ...proposal, events: [] }
  view.value = 'review'
  try { selectedProposal.value.events = await api.events(proposal.customer_id) } catch (_) { /* 时间线不可用时仍保留证据引用 */ }
}
async function decide(decision) {
  if (!selectedProposal.value) return
  await run(async () => { await api.decide(selectedProposal.value.proposal_id, { decision, reason: decision === 'approved' ? '负责人确认' : '负责人在工作台提交决定' }); notice.value = '负责人决定已记录。'; selectedProposal.value = null; await loadData() })
}
function navigate(next) { view.value = next; if (next === 'review' && isReviewer.value) loadData() }

onMounted(() => { if (auth.value) loadData() })
</script>

<template>
  <div v-if="!auth" class="auth-shell">
    <div class="auth-art"><div class="orbit orbit-a"></div><div class="orbit orbit-b"></div><div class="auth-mark"><Sparkles :size="20" /> SIGNAL / 01</div><h1>把模糊的<br /><em>销售现场</em>，变成<br />可确认的事实。</h1><p>Sales Signal Console 是一条有边界的 Agent 工作流：提取证据，追问缺口，交给负责人确认。</p><div class="auth-foot">受控工作流 · 证据优先 · 人工负责最终事实</div></div>
    <form class="login-card" @submit.prevent="login"><div class="eyebrow">LLM WIKI / SALES OPS</div><h2>进入工作台</h2><p class="muted">用你的组织账号继续</p><label>用户名<input v-model="loginForm.username" autocomplete="username" /></label><label>密码<input v-model="loginForm.password" type="password" autocomplete="current-password" /></label><button class="primary wide" :disabled="loading">{{ loading ? '正在验证…' : '登录工作台' }} <ArrowUpRight :size="17" /></button><p v-if="error" class="error-text">{{ error }}</p><div class="login-note"><ShieldCheck :size="16" /> 所有状态变化均保留审计事件</div></form>
  </div>
  <div v-else class="app-shell">
    <aside class="sidebar"><div class="brand"><div class="brand-icon"><Sparkles :size="17" /></div><div><strong>Signal</strong><span>sales ops console</span></div></div><div class="sidebar-label">工作台</div><button :class="['nav-item', { active: view === 'overview' }]" @click="navigate('overview')"><LayoutDashboard :size="17" /> 总览 <span>⌘ 1</span></button><button :class="['nav-item', { active: view === 'sales' }]" @click="navigate('sales')"><MessageSquareMore :size="17" /> 销售澄清 <span v-if="sessions.length">{{ sessions.length }}</span></button><button v-if="isReviewer" :class="['nav-item', { active: view === 'review' }]" @click="navigate('review')"><ShieldCheck :size="17" /> 负责人审核 <span v-if="proposals.length">{{ proposals.length }}</span></button><div class="sidebar-bottom"><div class="user-chip"><div class="avatar">{{ (auth.display_name || auth.username).slice(0, 1).toUpperCase() }}</div><div><strong>{{ auth.display_name || auth.username }}</strong><span>{{ roleLabel }}</span></div><button @click="logout" title="退出"><LogOut :size="15" /></button></div></div></aside>
    <main class="main"><header class="topbar"><div><span class="breadcrumb">SALES SIGNAL</span><span class="slash">/</span><span>{{ view === 'review' ? '负责人审核' : view === 'sales' ? '销售澄清' : '工作总览' }}</span></div><button class="refresh" @click="loadData"><RefreshCw :class="{ spinning: loading }" :size="16" /> 刷新数据</button></header><div v-if="error" class="flash error-flash"><CircleAlert :size="17" /> {{ error }}</div><div v-if="notice" class="flash success-flash"><Check :size="17" /> {{ notice }}</div>
      <section v-if="view === 'overview'" class="content"><div class="hero-row"><div><div class="eyebrow">{{ new Date().toLocaleDateString('zh-CN', { weekday: 'long', month: 'long', day: 'numeric' }) }}</div><h1>早上好，{{ auth.display_name || auth.username }}。</h1><p class="lede">今天先处理最接近业务结果的事实。</p></div><button class="primary" @click="navigate('sales')">提交一条纪要 <ArrowUpRight :size="17" /></button></div><div class="metric-grid"><div class="metric-card accent"><span>待澄清会话</span><strong>{{ sessions.filter(s => s.status === 'open').length }}</strong><small>需要销售补充事实</small></div><div class="metric-card"><span>我的会话</span><strong>{{ sessions.length }}</strong><small>所有追加记录可回放</small></div><div v-if="isReviewer" class="metric-card warning"><span>待负责人确认</span><strong>{{ proposals.length }}</strong><small>状态不会自动跳转</small></div><div class="metric-card dark"><span>安全边界</span><strong>100%</strong><small>事实需人工确认</small></div></div><div class="split-grid"><div class="panel"><div class="panel-head"><div><span class="eyebrow">RECENT SIGNALS</span><h3>最近澄清会话</h3></div><button class="link-btn" @click="navigate('sales')">查看全部 <ChevronRight :size="15" /></button></div><div v-if="sessions.length" class="signal-list"><button v-for="session in sessions.slice(0, 5)" :key="session.session_id" class="signal-row" @click="openSession(session.session_id)"><div class="signal-dot" :class="session.status"></div><div class="signal-main"><strong>{{ session.customer_id }}</strong><span>{{ session.session_id }}</span></div><div class="signal-status">{{ statusLabel(session.status) }}</div><ChevronRight :size="16" /></button></div><div v-else class="empty">提交第一条纪要，建立你的事实时间线。</div></div><div class="quote-card"><div class="quote-mark">“</div><p>成熟的 Agent，不是能做最多事情的那个，而是在明确的边界里，能稳定完成正确事情的那个。</p><span>— 产品原则 / 01</span></div></div></section>
      <section v-else-if="view === 'sales'" class="content"><div class="page-title"><div><div class="eyebrow">FIELD NOTES → VERIFIED FACTS</div><h1>销售澄清</h1><p class="lede">把一次洽谈交给 Agent，销售只回答真正会改变判断的问题。</p></div></div><div class="sales-grid"><div class="panel intake-panel"><div class="panel-head"><div><span class="eyebrow">NEW INTAKE</span><h3>提交洽谈纪要</h3></div><FileText :size="20" class="panel-icon" /></div><form @submit.prevent="submitIntake"><div class="field-row"><label>客户脱敏标识<input v-model="intakeForm.customer_id" required placeholder="customer-demo-001" /></label><label>幂等标识<input v-model="intakeForm.idempotency_key" required placeholder="同一份纪要重试时保持不变" /></label></div><div class="field-row"><label>洽谈时间<input v-model="intakeForm.occurred_at" type="datetime-local" required /></label><label>记录来源<select v-model="intakeForm.source_type"><option value="meeting_note">会后纪要</option><option value="transcript">会议转写</option><option value="chat_summary">聊天摘要</option></select></label></div><label>纪要正文<textarea v-model="intakeForm.content" required minlength="10" placeholder="只填写已脱敏的中文洽谈事实，不要粘贴手机号、邮箱、密钥或精确金额。"></textarea></label><div class="guardrail"><ShieldCheck :size="17" /><span>提交前自动检查敏感信息、Prompt injection 与数字隔离。精确数值不会进入 Agent 上下文。</span></div><button class="primary" :disabled="loading">提交并进入澄清 <ArrowUpRight :size="17" /></button></form></div><div class="panel session-panel"><div class="panel-head"><div><span class="eyebrow">MY SESSIONS</span><h3>我的澄清会话</h3></div><Clock3 :size="20" class="panel-icon" /></div><div class="session-picker" v-if="sessions.length"><button v-for="s in sessions" :key="s.session_id" :class="['session-item', { selected: selectedSession?.session_id === s.session_id }]" @click="openSession(s.session_id)"><div><strong>{{ s.customer_id }}</strong><span>{{ s.session_id }}</span></div><b>{{ statusLabel(s.status) }}</b></button></div><div v-else class="empty">还没有会话。</div><div v-if="selectedSession" class="session-detail"><div class="detail-head"><span>{{ selectedSession.conversation_id }}</span><b :class="['pill', selectedSession.status]">{{ statusLabel(selectedSession.status) }}</b></div><article v-for="turn in selectedSession.turns" :key="turn.turn_id" class="turn"><div class="turn-label">第 {{ turn.turn_no }} 轮 · {{ turn.question_count }} 个问题</div><div v-if="turn.agent_output?.claims?.length" class="claims"><div v-for="claim in turn.agent_output.claims" :key="claim.id" class="claim"><span class="claim-type">{{ claim.type }}</span><p>{{ claim.value }}</p><small>{{ claim.attribution }} · {{ claim.certainty }}</small></div></div><div v-for="question in turn.agent_output?.questions || []" :key="question.id" class="question"><div><span>需要确认</span><strong>{{ question.question }}</strong></div><div v-if="selectedSession.answers?.some(a => a.question_id === question.id)" class="answered">已追加回答</div><div v-else class="answer-box"><input v-model="answerDrafts[question.id]" placeholder="用一句话补充你确定的事实" @keyup.enter="sendAnswer(turn, question)" /><button @click="sendAnswer(turn, question)"><ArrowUpRight :size="16" /></button></div></div><div class="turn-meta">输入 {{ turn.input_tokens || 0 }} tokens · 输出 {{ turn.output_tokens || 0 }} tokens · {{ turn.latency_ms || 0 }} ms</div></article></div></div></div></section>
      <section v-else class="content"><div class="page-title"><div><div class="eyebrow">RESULTS BEFORE PROCESS</div><h1>负责人审核</h1><p class="lede">先看建议状态、风险和下一步；需要时展开证据与时间线。</p></div></div><div class="review-layout"><div class="proposal-list panel"><div class="panel-head"><div><span class="eyebrow">PENDING DECISIONS</span><h3>待确认建议</h3></div><span class="count-badge">{{ proposals.length }}</span></div><button v-for="proposal in proposals" :key="proposal.proposal_id" :class="['proposal-row', { selected: selectedProposal?.proposal_id === proposal.proposal_id }]" @click="openProposal(proposal)"><div class="proposal-state">{{ stateLabel(proposal.proposed_state) }}</div><div class="proposal-copy"><strong>{{ proposal.customer_id }}</strong><span>{{ proposal.reasoning_summary || '等待负责人查看证据' }}</span></div><div class="confidence">{{ Math.round(Number(proposal.confidence) * 100) }}%</div></button><div v-if="!proposals.length" class="empty">暂无待确认建议。</div></div><div class="proposal-detail panel" v-if="selectedProposal"><div class="detail-head"><div><span class="eyebrow">DECISION BRIEF</span><h2>{{ stateLabel(selectedProposal.proposed_state) }}</h2><p>{{ selectedProposal.customer_id }} · {{ selectedProposal.proposal_id }}</p></div><div class="confidence-ring">{{ Math.round(Number(selectedProposal.confidence) * 100) }}<small>%</small></div></div><div class="brief-summary"><span>判断摘要</span><p>{{ selectedProposal.reasoning_summary || '—' }}</p></div><div class="brief-summary"><span>下一步动作</span><p>{{ selectedProposal.next_action || '—' }}</p></div><div v-if="selectedProposal.risk_flags?.length" class="risk-box"><CircleAlert :size="17" /><div><strong>风险提示</strong><p>{{ selectedProposal.risk_flags.join('、') }}</p></div></div><details open><summary>证据引用 <ChevronRight :size="15" /></summary><pre>{{ JSON.stringify(selectedProposal.evidence_refs || [], null, 2) }}</pre></details><div class="decision-actions"><button class="primary" @click="decide('approved')"><Check :size="16" />确认状态</button><button class="secondary" @click="decide('rejected')">驳回</button></div></div><div v-else class="proposal-detail panel empty-detail"><Sparkles :size="30" /><h3>选择一条建议</h3><p>老板只看结果，但每个结果都能回到证据。</p></div></div></section>
    </main>
  </div>
</template>
