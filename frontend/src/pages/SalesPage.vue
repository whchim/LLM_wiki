<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, Loading, Refresh, Upload } from '@element-plus/icons-vue'
import { api, ApiError, SESSION_STATUS_LABELS, STATE_LABELS } from '../api'
const props = defineProps({
  isReviewer: { type: Boolean, default: false },
  isAdmin: { type: Boolean, default: false },
})
const loading = ref(false)
const showArchived = ref(false)

// ---- Agent 进行中反馈 ----
// 澄清 Agent 单次调用通常 10-40 秒（服务端 60 秒超时），必须让用户看到"在跑"而不是"卡死"。
// agentBusy 同时充当防重复点击的第一道闸（后端另有"未答问题不推进"+乐观锁兜底）。
const agentBusy = ref('')
const elapsed = ref(0)
let busyTimer = null

function stopTimer() {
  if (busyTimer) { clearInterval(busyTimer); busyTimer = null }
}
function startBusy(label) {
  agentBusy.value = label
  elapsed.value = 0
  stopTimer()
  busyTimer = setInterval(() => { elapsed.value += 1 }, 1000)
}
function stopBusy() {
  stopTimer()
  agentBusy.value = ''
  elapsed.value = 0
}
onUnmounted(stopTimer)

const sessions = ref([])
const selected = ref(null)
const drafts = ref({})
const fileInput = ref(null)
const importedFile = ref(null)
const fileError = ref('')
/** 同一份纪要重复提交的提示（含原会话与"仍要新建"入口） */
const duplicateHint = ref(null)

const form = ref({
  customer_id: '', customer_alias: null, idempotency_key: '', occurred_at: nowLocal(),
  source_type: 'meeting_note', source_ref: null, content: '',
})

// ---- 客户简称（别名）绑定 ----
const aliases = ref([])
const addAliasDialog = ref(false)
const aliasDraft = ref({ alias: '', customer_id: '' })

/** 选了简称后锁定代号字段并回填解析结果，避免"选了简称还带旧代号"的混淆 */
const customerIdLocked = computed(() => {
  if (!form.value.customer_alias) return ''
  const hit = aliases.value.find((a) => a.alias === form.value.customer_alias)
  return hit ? `（由简称「${form.value.customer_alias}」决定：${hit.customer_id}）` : ''
})

async function loadAliases() {
  try {
    aliases.value = await api.aliases()
  } catch (err) {
    // 别名是可选便利功能，加载失败不阻断提交（仍可直接填代号）
    aliases.value = []
  }
}

function onAliasChange(value) {
  const hit = aliases.value.find((a) => a.alias === value)
  form.value.customer_id = hit ? hit.customer_id : ''
}

async function createAlias() {
  const alias = aliasDraft.value.alias.trim()
  const customerId = aliasDraft.value.customer_id.trim()
  if (!alias) {
    ElMessage.warning('请填写简称')
    return
  }
  const existing = aliases.value.find((a) => a.alias === alias)
  if (!customerId) {
    ElMessage.warning('请填写该简称对应的脱敏代号（或用「生成代号」）')
    return
  }
  try {
    const created = await api.createAlias(alias, customerId)
    await loadAliases()
    form.value.customer_alias = created.alias
    onAliasChange(created.alias)
    addAliasDialog.value = false
    aliasDraft.value = { alias: '', customer_id: '' }
    ElMessage.success(`已登记简称「${created.alias}」`)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '登记失败')
  }
}

async function removeAlias(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除简称「${row.alias}」与代号 ${row.customer_id} 的绑定？删除后该简称不能再用于提交。`,
      '删除别名绑定', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
  } catch {
    return // 用户取消
  }
  try {
    await api.deleteAlias(row.alias, row.customer_id)
    if (form.value.customer_alias === row.alias) {
      form.value.customer_alias = null
      form.value.customer_id = ''
    }
    await loadAliases()
    ElMessage.success('已删除绑定')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  }
}

function nowLocal() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
const uid = () => globalThis.crypto?.randomUUID?.() || `k-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`

/**
 * 生成客户代号：`cust-YYYYMMDD-xxxx`。
 * 刻意**不从客户中文名派生**（拼音/缩写同样会泄露客户身份），只给中性代号。
 * ⚠️ 同一客户必须长期复用同一个代号，否则状态机（按 customer_id 聚合）会把一次跟进
 * 拆成多个客户，历史就断了——所以生成后要记下来。
 */
function generateCustomerId() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
  form.value.customer_id = `cust-${stamp}-${uid().replace(/-/g, '').slice(0, 4)}`
}

function guessCustomerId(filename) {
  const base = filename.replace(/\.[^.]+$/, '').replace(/^\d{4}[-_]?\d{2}[-_]?\d{2}[-_\s]*/, '').trim()
  return base.replace(/[^\w\u4e00-\u9fa5-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40)
}

async function load() {
  loading.value = true
  try {
    sessions.value = await api.mine(showArchived.value)
    if (selected.value) {
      const still = sessions.value.find((s) => s.session_id === selected.value.session_id)
      if (still) selected.value = await api.session(selected.value.session_id)
      else selected.value = null
    }
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载会话失败')
  } finally {
    loading.value = false
  }
}

async function openSession(row) {
  if (agentBusy.value) return   // 进行中不重复触发（防重复推进）
  if (row.deleted_at) {
    ElMessage.info('该会话已归档（仅管理员可查看），如需打开请先点「恢复」')
    return
  }
  startBusy('澄清 Agent 正在分析这条纪要，生成追问…')
  try {
    // advance=true：会话为 open 时让后端跑一轮 agent，首次打开即可看到追问
    selected.value = await api.session(row.session_id, true)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载会话详情失败')
  } finally {
    stopBusy()
  }
}

/** 转人工后是否还能重开：还有 Agent 运行预算就允许（max_rounds 轮追问 + 1 次收尾判定） */
const canReopen = computed(() => {
  const s = selected.value
  return !!s && (s.round_count || 0) <= (s.max_rounds || 2)
})

async function doResolve(payload) {
  try {
    const res = await api.resolveSession(selected.value.session_id, payload)
    selected.value = { ...selected.value, ...res.session }
    ElMessage.success(payload.decision === 'closed' ? '会话已关闭' : '会话已重开，可继续推进')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '处置失败')
  }
}

/** 补充事实后继续：可选补一句脱敏事实；留空即仅重开重试（瞬时故障场景） */
async function reopenSession() {
  const res = await ElMessageBox.prompt(
    '可补充一句已脱敏的事实（留空＝仅重试一轮）', '补充事实后继续',
    { confirmButtonText: '重开并继续', cancelButtonText: '取消', inputPlaceholder: '例如：客户已确认下周二演示' },
  ).catch(() => ({ value: null }))
  if (res.value === null) return
  await doResolve({ decision: 'reopened', answer_text_redacted: res.value || null })
}

/** 关闭会话：原因必填，落库并进审计——关闭无原因等于没闭环 */
async function closeSession() {
  const res = await ElMessageBox.prompt(
    '关闭原因（必填，记入会话与审计）', '关闭澄清会话',
    {
      confirmButtonText: '关闭会话', cancelButtonText: '取消',
      inputValidator: (v) => ((v || '').trim() ? true : '请填写关闭原因'),
    },
  ).catch(() => ({ value: null }))
  if (res.value === null) return
  await doResolve({ decision: 'closed', reason: res.value })
}

/** 事实已足够（ready_for_proposal）→ 生成待确认状态建议，交负责人在「客户状态」确认 */
async function generateProposal() {
  if (agentBusy.value) return
  startBusy('正在根据已确认事实生成状态建议…')
  try {
    const res = await api.generateProposal(selected.value.session_id)
    if (!res.generated) {
      ElMessage.warning(`未能生成建议：${res.reason || '证据不足'}`)
    } else if (res.reused) {
      const state = stateName(res.proposal?.proposed_state)
      ElMessage.info(`该洽谈已有待确认建议（${state}），未重复创建`)
    } else {
      // 判定方式用业务语言说清楚：规则能判就规则判（零成本），判不出才请模型复核
      const how = (res.used || '').startsWith('llm') ? '模型复核判定' : '规则判定'
      ElMessage.success(`已生成待确认建议：${stateName(res.proposed_state)}（${how}）—— 请负责人在「客户状态」确认`)
      if (res.llm_error) {
        ElMessage.warning(`模型复核没有得出结论（${res.llm_error}），本次结论来自规则判定，请负责人留意`)
      }
    }
    selected.value = await api.session(selected.value.session_id)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '生成状态建议失败')
  } finally {
    stopBusy()
  }
}

/** 从本地 .md/.txt 导入正文——不自动提交，销售需过目删改后再提交（门禁在正文上） */
async function readFile(event) {
  const file = event.target.files?.[0]
  if (!file) return
  fileError.value = ''
  const ok = ['.md', '.markdown', '.txt'].some((ext) => file.name.toLowerCase().endsWith(ext))
  if (!ok) {
    fileError.value = '仅支持 .md / .markdown / .txt'
    event.target.value = ''
    return
  }
  if (file.size > 1024 * 1024) {
    fileError.value = `文件 ${(file.size / 1024 / 1024).toFixed(1)}MB 超过 1MB 上限`
    event.target.value = ''
    return
  }
  const text = await file.text()
  if (text.trim().length < 10) {
    fileError.value = `「${file.name}」没有可提交的正文（不足 10 字）`
    event.target.value = ''
    return
  }
  form.value.content = text
  form.value.source_type = 'meeting_note'
  form.value.source_ref = file.name
  form.value.occurred_at = nowLocal()
  form.value.idempotency_key = `file-${uid()}`
  if (!form.value.customer_id.trim()) form.value.customer_id = guessCustomerId(file.name)
  importedFile.value = { name: file.name, size: file.size }
  event.target.value = ''
}

function clearFile() {
  importedFile.value = null
  form.value.content = ''
  fileError.value = ''
}

async function submit(forceNew = false) {
  if (!form.value.customer_id.trim() || !form.value.content.trim()) {
    ElMessage.warning('请填写客户标识与纪要正文')
    return
  }
  if (!form.value.idempotency_key.trim()) form.value.idempotency_key = `ui-${uid()}`
  loading.value = true
  duplicateHint.value = null
  let openAfter = null
  startBusy('正在提交纪要并创建澄清会话…')
  try {
    const res = await api.intake({
      ...form.value, occurred_at: new Date(form.value.occurred_at).toISOString(), force_new: forceNew,
    })
    if (res?.duplicate) {
      // 同一份正文已经提交过：后端复用原洽谈，不新建会话——必须说清楚"为什么没有新会话"
      const d = res.duplicate
      duplicateHint.value = {
        sessionId: d.session_id, submittedAt: fmtTime(d.submitted_at), sourceRef: d.source_ref,
      }
      ElMessage.warning(`这份纪要已于 ${fmtTime(d.submitted_at)} 提交过（内容完全相同），已为你打开原会话`)
      await load()
      openAfter = d.session_id || null
    } else {
      ElMessage.success('纪要已通过门禁并创建澄清会话')
      duplicateHint.value = null
      form.value.content = ''
      form.value.idempotency_key = ''
      importedFile.value = null
      await load()
    }
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '提交失败')
  } finally {
    loading.value = false
    stopBusy()
  }
  // 打开会话要放在 stopBusy 之后：openSession 在"有任务进行中"时会直接返回（防重复推进）
  if (openAfter) await openSession({ session_id: openAfter })
}

/** 重复提交提示里的"仍要新建"：显式 force_new 再提交一次（保留表单内容，不让人重填） */
async function submitAnyway() {
  await submit(true)
}

async function sendAnswer(turn, question) {
  const value = (drafts.value[question.id] || '').trim()
  if (!value || agentBusy.value) return
  startBusy('正在根据你的回答生成下一轮判断…')
  try {
    const res = await api.answer(selected.value.session_id, {
      turn_id: turn.turn_id, question_id: question.id, answer_text_redacted: value,
    })
    drafts.value[question.id] = ''
    // 回答后后端已自动推进一轮：可能产生新追问，也可能转人工
    const adv = res?.advance
    if (adv?.advanced && adv.status === 'needs_clarification') ElMessage.success('回答已保存，Agent 提出了新的追问')
    else if (adv?.advanced) ElMessage.info('回答已保存；本会话已转人工审核')
    else ElMessage.success('回答已追加保存')
    selected.value = await api.session(selected.value.session_id)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '保存回答失败')
  } finally {
    stopBusy()
  }
}

/** 归档（软删除）会话——**仅管理员**；客户状态事件不受影响（事实不可删，只能追加更正） */
async function archiveSession() {
  if (!selected.value || agentBusy.value) return
  try {
    await ElMessageBox.confirm(
      `确定归档「${selected.value.customer_id}」的这条澄清会话？\n\n`
      + '归档后：该会话与其状态建议不再出现在列表里；客户已确认的阶段与状态事件**不受影响**（事实记录只追加、不删除）。可由管理员随时恢复。',
      '归档会话记录', { type: 'warning', confirmButtonText: '确认归档', cancelButtonText: '取消' })
  } catch (err) {
    return
  }
  startBusy('正在归档会话…')
  try {
    const res = await api.deleteSession(selected.value.session_id)
    ElMessage.success(res.already_deleted
      ? '该会话此前已归档'
      : `已归档（同时归档状态建议 ${res.proposals_archived} 条）`)
    selected.value = null
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '归档失败')
  } finally {
    stopBusy()
  }
}

/** 恢复已归档会话——仅管理员 */
async function restoreArchived(row) {
  try {
    const res = await api.restoreSession(row.session_id)
    ElMessage.success(res.restored ? '已恢复该会话' : '该会话未被归档')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '恢复失败')
  }
}

const statusTag = (s) => ({
  open: 'warning', needs_human_review: 'danger', ready_for_proposal: 'success',
  completed: 'info', cancelled: 'info',
}[s] || 'info')

/** 轮次展示：round_count 含"收尾判定"那一轮，不能直接当追问轮数显示（否则出现 3/2 这种怪值） */
function roundLabel(s) {
  if (!s) return ''
  const max = s.max_rounds || 2
  const asked = Math.min(s.round_count || 0, max)
  const concluded = (s.round_count || 0) > max
  return `${asked}/${max} 轮追问${concluded ? ' · 收尾已完成' : ''}`
}

const confidence = (row) => Math.round(Number(row?.confidence || 0) * 100)
const stateName = (s) => STATE_LABELS[s] || s || '未确认'

/** 时间展示：列表要能区分同一客户的多次洽谈，时间必须可见 */
function fmtTime(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 同一客户第几次洽谈：一个客户多次洽谈是正常的（新洽谈=新会话），列表必须说清楚而不是看着像重复 */
const customerRounds = computed(() => {
  const byCustomer = {}
  ;[...sessions.value]
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at))
    .forEach((s) => {
      byCustomer[s.customer_id] = byCustomer[s.customer_id] || { total: 0, index: {} }
      byCustomer[s.customer_id].total += 1
      byCustomer[s.customer_id].index[s.session_id] = byCustomer[s.customer_id].total
    })
  return byCustomer
})
function customerRoundLabel(row) {
  const hit = customerRounds.value[row.customer_id]
  if (!hit || hit.total < 2) return ''
  return `第 ${hit.index[row.session_id]} 次洽谈`
}

/** 会话状态中文：cancelled 需结合处置信息区分"人工处置后关闭"与"作废"，不能一律叫"已取消" */
function sessionStatusLabel(s) {
  if (!s) return ''
  if (s.status === 'cancelled') return s.resolution_note ? '人工已关闭' : '已取消'
  return SESSION_STATUS_LABELS[s.status] || s.status
}

/** 最新一轮里尚未回答的问题（与后端判据同构：回答按轮作用域比对） */
function pendingQuestions(s) {
  const turns = s?.turns || []
  if (!turns.length) return []
  const latest = turns[turns.length - 1]
  if (latest.status !== 'needs_clarification') return []
  const answered = new Set((s.answers || [])
    .filter((a) => a.turn_id === latest.turn_id)
    .map((a) => a.question_id))
  return (latest.agent_output?.questions || []).filter((q) => !answered.has(q.id))
}

/** "下一步该做什么"：避免用户只看到"没有按钮"却不知道原因（button 只在条件满足时出现） */
const nextStep = computed(() => {
  const s = selected.value
  if (!s) return null
  const pending = pendingQuestions(s)
  // 已生成建议：交给负责人了，明确告知"球在谁手上"，避免看起来毫无变化
  if (s.pending_proposal) {
    const p = s.pending_proposal
    const tail = p.decision === 'needs_review' ? '（需人工判断）' : ''
    return { type: 'success',
             text: `已交负责人：待确认建议「${stateName(p.proposed_state)}」${tail}，等待负责人在「客户状态」确认` }
  }
  if (s.status === 'open' && pending.length) {
    return { type: 'warning',
             text: `下一步：请先回答第 ${s.round_count} 轮的 ${pending.length} 个追问，答完会自动进入判断` }
  }
  if (s.status === 'open') {
    return { type: 'info', text: '本轮问题已答完：点「刷新」或重新打开会话即可让 Agent 继续判断' }
  }
  if (s.status === 'ready_for_proposal') {
    return { type: 'success', text: '事实已足够：点下方「生成状态建议」，交负责人在「客户状态」确认' }
  }
  if (s.status === 'needs_human_review') {
    return { type: 'warning',
             text: '已转人工：点「生成建议交负责人」把案例交给负责人判断；或由审核者补充事实后重开' }
  }
  if (s.status === 'cancelled') {
    return { type: 'info', text: `会话已由人工关闭：${s.resolution_note || '—'}（${s.resolved_by || '—'}）` }
  }
  return null
})

/** 一轮里的追问（结构：turn.agent_output.questions） */
const questionsOf = (turn) => (turn?.agent_output?.questions) || []
/** 某问题是否已答（从会话的 answers 里按 question_id 找） */
const answerOf = (questionId) => {
  const hit = (selected.value?.answers || []).find((a) => a.question_id === questionId)
  return hit?.answer_text_redacted || ''
}
/** 无追问时的说明文案 */
function turnNote(turn) {
  const out = turn?.agent_output || {}
  if (turn?.status === 'human_review' || turn?.status === 'needs_human_review') {
    return `已转人工审核：${(out.error || []).join('；') || '模型或契约未通过'}`
  }
  if (out.stop_reason === 'ready_for_proposal') return '事实已足够，可进入状态建议（需负责人确认）'
  if (out.stop_reason === 'insufficient_evidence') return '证据不足，未能提出可执行的追问'
  return `本轮无追问（stop_reason=${out.stop_reason || '未知'}）`
}

const openCount = computed(() => sessions.value.filter((s) => s.status === 'open').length)

onMounted(() => { load(); loadAliases() })
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>销售澄清</h1>
        <p class="lede">把一次洽谈交给 Agent，销售只回答真正会改变判断的问题。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-row :gutter="18">
      <el-col :xs="24" :lg="10">
        <el-card shadow="never">
          <template #header><strong>提交洽谈纪要</strong></template>
          <el-form label-position="top">
            <el-form-item>
              <template #label>
                <div class="label-row">
                  <span>客户（可选内部简称）</span>
                  <el-button size="small" text type="primary" @click="addAliasDialog = true">登记简称</el-button>
                </div>
              </template>
              <el-select
                v-model="form.customer_alias"
                clearable filterable allow-create default-first-option
                placeholder="选择已登记的简称；没有就留空，改用右侧代号"
                @change="onAliasChange"
              >
                <el-option v-for="a in aliases" :key="a.alias + a.customer_id"
                           :label="a.alias" :value="a.alias">
                  <span>{{ a.alias }}</span>
                  <span class="alias-cid">{{ a.customer_id }}</span>
                </el-option>
              </el-select>
              <div class="field-hint">
                简称只是选择入口，系统内部仍存脱敏代号；同一客户可登记多个叫法。
              </div>
            </el-form-item>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item>
                  <template #label>
                    <div class="label-row">
                      <span>客户脱敏标识{customerIdLocked}</span>
                      <el-button size="small" text type="primary" :disabled="!!form.customer_alias"
                                 @click="generateCustomerId">生成代号</el-button>
                    </div>
                  </template>
                  <el-input v-model="form.customer_id" :disabled="!!form.customer_alias"
                            :placeholder="form.customer_alias ? '由所选简称决定' : 'cust-20260912-a3f7'" />
                  <div class="field-hint">
                    用代号，不要填客户真实名称：同一客户长期复用同一个代号（状态按它聚合），
                    只能用字母、数字和 <code>- _ . :</code>，首字符须为字母或数字。
                  </div>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="幂等标识">
                  <el-input v-model="form.idempotency_key" placeholder="留空则自动生成" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="洽谈时间">
                  <el-input v-model="form.occurred_at" type="datetime-local" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="记录来源">
                  <el-select v-model="form.source_type">
                    <el-option label="会后纪要" value="meeting_note" />
                    <el-option label="会议转写" value="transcript" />
                    <el-option label="聊天摘要" value="chat_summary" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item>
              <template #label>
                <div class="label-row">
                  <span>纪要正文</span>
                  <el-button size="small" text type="primary" :icon="Upload" @click="fileInput.click()">
                    从文件导入
                  </el-button>
                </div>
              </template>
              <input ref="fileInput" type="file" accept=".md,.markdown,.txt" class="hidden-file" @change="readFile" />
              <el-input v-model="form.content" type="textarea" :rows="8"
                        placeholder="从本地 .md / .txt 导入，或直接粘贴。只提交已脱敏的中文洽谈事实。" />
            </el-form-item>
          </el-form>

          <el-alert v-if="importedFile" type="success" :closable="false" show-icon class="mb">
            <template #title>
              <span class="file-chip">
                <el-icon><Document /></el-icon>
                {{ importedFile.name }} · {{ (importedFile.size / 1024).toFixed(1) }}KB 已载入正文，请过目后提交
                <el-button size="small" text @click="clearFile">移除</el-button>
              </span>
            </template>
          </el-alert>
          <el-alert v-if="fileError" type="error" :title="fileError" show-icon :closable="false" class="mb" />

          <el-alert type="info" show-icon :closable="false" class="mb"
                    title="提交前自动检查敏感信息、Prompt injection 与数字隔离；精确数值不会进入 Agent 上下文。" />

          <el-button type="primary" :loading="loading" class="full" @click="submit()">提交并进入澄清</el-button>

          <el-alert v-if="duplicateHint" type="warning" show-icon :closable="false" class="mb">
            <template #title>
              这份纪要已于 {{ duplicateHint.submittedAt }} 提交过，内容完全相同——已为你打开原会话，没有重复新建
            </template>
            <div class="dup-body">
              <span v-if="duplicateHint.sourceRef" class="muted">来源：{{ duplicateHint.sourceRef }}</span>
              <span class="muted">原会话：{{ duplicateHint.sessionId }}</span>
              <span class="muted">如果这确实是一次新的洽谈，点右边按钮；否则不用再操作。</span>
              <el-button size="small" text type="primary" :disabled="loading" @click="submitAnyway">
                这确实是一次新洽谈，仍要新建
              </el-button>
            </div>
          </el-alert>

          <el-collapse class="alias-manage">
            <el-collapse-item :title="`已登记的客户简称（${aliases.length}）`" name="alias">
              <el-empty v-if="!aliases.length" description="还没有登记简称" :image-size="60">
                <p class="muted">简称让销售不用记代号；系统内部仍只存代号。</p>
              </el-empty>
              <el-table v-else :data="aliases" size="small">
                <el-table-column prop="alias" label="内部简称" min-width="100" />
                <el-table-column prop="customer_id" label="脱敏代号" min-width="140" show-overflow-tooltip />
                <el-table-column prop="created_by" label="登记人" width="90" />
                <el-table-column label="操作" width="80">
                  <template #default="{ row }">
                    <el-button size="small" text type="danger" @click="removeAlias(row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <p class="muted alias-note">
                同一简称只能绑定一个客户——绑到别的客户会被拒绝并提示冲突，避免把两个客户合并成一个。
              </p>
            </el-collapse-item>
          </el-collapse>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="14">
        <el-card shadow="never" class="session-card">
          <template #header>
            <div class="card-head">
              <strong>我的澄清会话</strong>
              <el-tag v-if="openCount" type="warning" size="small">待澄清 {{ openCount }}</el-tag>
              <el-checkbox v-if="props.isAdmin" v-model="showArchived" size="small"
                           class="archived-toggle" @change="load">显示已归档</el-checkbox>
            </div>
          </template>

          <el-empty v-if="!loading && !sessions.length" description="还没有会话" :image-size="80" />

          <el-table v-else v-loading="loading" :data="sessions" highlight-current-row size="small" @row-click="openSession">
            <el-table-column label="客户" min-width="140">
              <template #default="{ row }">
                <div>{{ row.customer_id }}</div>
                <el-tag v-if="customerRoundLabel(row)" size="small" type="info" effect="plain">
                  {{ customerRoundLabel(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="纪要摘要" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.content_preview">{{ row.content_preview }}</span>
                <span v-else class="muted">（无正文摘要）</span>
                <div v-if="row.source_ref" class="muted src-line">来源：{{ row.source_ref }}</div>
              </template>
            </el-table-column>
            <el-table-column label="提交时间" width="130">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag v-if="row.deleted_at" type="info" size="small">已归档</el-tag>
                <el-tag v-else :type="statusTag(row.status)" size="small">
                  {{ sessionStatusLabel(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="轮次" width="140">
              <template #default="{ row }">{{ roundLabel(row) }}</template>
            </el-table-column>
            <el-table-column v-if="showArchived && props.isAdmin" label="操作" width="80">
              <template #default="{ row }">
                <el-button v-if="row.deleted_at" size="small" text type="primary"
                           @click.stop="restoreArchived(row)">恢复</el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
          <p class="muted list-note">
            同一客户可以有多条记录：一次洽谈 = 一个会话（上表按最近更新排序，可看「第 N 次洽谈」）。
            同一份纪要重复提交会被自动拦下并复用原会话；确实要再建一次洽谈，用提交提示里的「确实要再建一次洽谈」。
          </p>
        </el-card>

        <el-card v-if="selected" shadow="never" class="detail-card">
          <template #header>
            <div class="card-head">
              <strong>{{ selected.customer_id }}</strong>
              <el-tag :type="statusTag(selected.status)" size="small">
                {{ sessionStatusLabel(selected) }}
              </el-tag>
              <span class="muted">{{ roundLabel(selected) }}</span>
              <span class="muted">{{ selected.session_id }}</span>
              <div class="spacer" />
              <el-button v-if="props.isAdmin" size="small" type="danger" text
                         :disabled="!!agentBusy" @click="archiveSession">归档记录</el-button>
            </div>
          </template>

          <!-- 下一步提示：按钮只在条件满足时出现，这里明确告诉用户当前缺什么 -->
          <el-alert v-if="nextStep" :type="nextStep.type" show-icon :closable="false"
                    :title="nextStep.text" class="next-step" />

          <!-- Agent 进行中：明确告知"在跑"并显示已等待秒数，避免被误认为卡死 -->
          <div v-if="agentBusy" class="agent-busy">
            <el-icon class="is-loading"><Loading /></el-icon>
            <span>{{ agentBusy }}</span>
            <span class="muted">已等待 {{ elapsed }} 秒 · 通常 10-40 秒；服务端 60 秒超时会明确报错</span>
          </div>

          <div v-for="turn in selected.turns || []" :key="turn.turn_id" class="turn">
            <div class="turn-head">
              第 {{ turn.turn_no }} 轮
              <el-tag size="small" :type="turn.status === 'needs_clarification' ? 'warning' : 'info'" class="turn-tag">
                {{ SESSION_STATUS_LABELS[turn.status] || turn.status }}
              </el-tag>
              <span v-if="turn.input_tokens" class="muted">tokens {{ turn.input_tokens }}/{{ turn.output_tokens }} · {{ turn.latency_ms }}ms</span>
            </div>
            <!-- 结构：turn.agent_output.questions（由澄清 Agent 写入，见 SA-13） -->
            <div v-for="q in questionsOf(turn)" :key="q.id" class="question">
              <p class="q-text">
                {{ q.question }}
                <span class="q-type">{{ q.answer_type }}</span>
              </p>
              <div v-if="answerOf(q.id)" class="answered">已答：{{ answerOf(q.id) }}</div>
              <div v-else class="answer-row">
                <el-input v-model="drafts[q.id]" size="small" placeholder="用一句话补充事实（已脱敏）"
                          :disabled="!!agentBusy" />
                <el-button size="small" type="primary" :disabled="!!agentBusy"
                           @click="sendAnswer(turn, q)">提交回答</el-button>
              </div>
            </div>
            <p v-if="!questionsOf(turn).length" class="muted turn-note">{{ turnNote(turn) }}</p>
          </div>

          <!-- 事实已足够：生成待确认状态建议（最后一公里），交负责人在「客户状态」确认 -->
          <div v-if="selected.status === 'ready_for_proposal' && !selected.pending_proposal" class="resolve-row">
            <el-alert type="success" show-icon :closable="false"
                      title="澄清已完成：可生成待确认状态建议（建议不会自动改客户事实，需负责人在「客户状态」确认）" />
            <div class="resolve-actions">
              <el-button type="primary" :disabled="!!agentBusy" @click="generateProposal">生成状态建议</el-button>
            </div>
          </div>

          <!-- 人工处置闭环：转人工后必须给负责人一个可判断的对象，否则"转人工"是死胡同 -->
          <div v-if="selected.status === 'needs_human_review'" class="resolve-row">
            <el-alert type="warning" show-icon :closable="false"
                      title="本会话已转人工：可生成待确认建议交负责人判断，或由审核者补充事实后重开，或关闭本次澄清记录" />
            <el-alert v-if="selected.pending_proposal" type="success" show-icon :closable="false"
                      :title="`已交负责人：建议「${stateName(selected.pending_proposal.proposed_state)}」待确认（去「客户状态」查看）`" />
            <div class="resolve-actions">
              <el-button v-if="!selected.pending_proposal" type="success" :disabled="!!agentBusy"
                         @click="generateProposal">生成建议交负责人</el-button>
              <el-button type="primary" plain :disabled="!props.isReviewer || !canReopen || !!agentBusy"
                         @click="reopenSession">补充事实后继续</el-button>
              <el-button type="danger" plain :disabled="!props.isReviewer || !!agentBusy"
                         @click="closeSession">关闭会话</el-button>
            </div>
            <p v-if="!canReopen" class="muted resolve-hint">
              追问与自动判定额度已用尽（{{ selected.max_rounds }}/{{ selected.max_rounds }} 轮）：
              点「生成建议交负责人」把案例交给负责人判断，或「关闭会话」结束本次澄清记录（关闭不会产生给负责人的对象）。
            </p>
            <p v-else-if="!props.isReviewer" class="muted resolve-hint">
              补充事实、关闭会话需审核者/管理员；「生成建议交负责人」你自己即可操作。
            </p>
            <p v-else class="muted resolve-hint">重开后可补答待答问题；答完自动执行收尾判定。</p>
          </div>
          <div v-else-if="selected.status === 'cancelled' && selected.resolution_note" class="resolve-row">
            <el-alert type="info" show-icon :closable="false"
                      :title="`已关闭：${selected.resolution_note}（${selected.resolved_by || '—'}）`" />
          </div>

          <el-empty v-if="!(selected.turns || []).length" description="尚无澄清轮次" :image-size="70">
            <p class="muted">打开会话时后端会自动让澄清 Agent 跑一轮；若模型未配置或失败，会作为「转人工审核」轮次记录在此。</p>
          </el-empty>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="addAliasDialog" title="登记客户简称" width="460px">
      <el-form label-position="top">
        <el-form-item label="内部简称">
          <el-input v-model="aliasDraft.alias" placeholder="例如：某某项目 / 某某科技"
                    maxlength="40" show-word-limit />
        </el-form-item>
        <el-form-item label="对应脱敏代号">
          <el-input v-model="aliasDraft.customer_id" placeholder="cust-20260912-a3f7" />
          <div class="field-hint">
            简称只是选择入口，系统内部仍存这个代号；不能填客户真实名称或手机号。
            不确定就点「生成代号」。
          </div>
        </el-form-item>
        <el-button size="small" text type="primary" @click="() => { if (!aliasDraft.customer_id) { generateCustomerId(); aliasDraft.customer_id = form.customer_id } }">
          生成一个代号填入
        </el-button>
      </el-form>
      <template #footer>
        <el-button @click="addAliasDialog = false">取消</el-button>
        <el-button type="primary" @click="createAlias">登记</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mb { margin-bottom: 12px; }
.full { width: 100%; }
.label-row { display: flex; align-items: center; justify-content: space-between; width: 100%; }
.field-hint { margin-top: 6px; color: var(--c-text-dim); font-size: 12px; line-height: 1.7; }
.field-hint code { padding: 1px 4px; border-radius: 4px; background: var(--c-neutral-badge-bg); font-size: 11px; }
.alias-manage { margin-top: 16px; }
.alias-note { margin: 8px 0 0; line-height: 1.7; }
.alias-cid { float: right; color: var(--c-text-faint); font-size: 11px; }
.hidden-file { display: none; }
.file-chip { display: inline-flex; align-items: center; gap: 6px; }
.card-head { display: flex; align-items: center; gap: 10px; }
.spacer { flex: 1; }
.archived-toggle { margin-left: auto; }
.card-head .muted { margin-left: auto; }
.session-card { margin-bottom: 18px; }
.turn { margin-bottom: 18px; }
.turn-head { font-size: 12px; color: var(--c-text-dim); margin-bottom: 8px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.turn-tag { flex: none; }
.turn-note { margin: 6px 0 0; }
.q-type { margin-left: 6px; font-size: 11px; color: var(--c-text-faint); }
.question { padding: 10px 12px; border: 1px solid var(--el-border-color); border-radius: 8px; margin-bottom: 8px; }
.q-text { margin: 0 0 8px; font-size: 13px; color: var(--c-text); }
.answered { font-size: 12px; color: var(--el-color-success); }
.answer-row { display: flex; gap: 8px; }
.muted { color: var(--c-text-muted); font-size: 12px; }
.resolve-row { margin-top: 14px; }
.resolve-hint { margin: 8px 0 0; }
.next-step { margin-bottom: 12px; }
.resolve-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
.agent-busy {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 10px 12px; margin-bottom: 12px; font-size: 13px;
  border: 1px solid var(--el-color-primary-light-7);
  background: var(--el-color-primary-light-9);
  border-radius: 8px;
}
:deep(.el-table__row) { cursor: pointer; }
.src-line { margin-top: 2px; }
.list-note { margin: 10px 0 0; line-height: 1.6; }
.dup-body { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 6px; }
</style>
