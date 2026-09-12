<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, Refresh, Upload } from '@element-plus/icons-vue'
import { api, ApiError, SESSION_STATUS_LABELS } from '../api'
const loading = ref(false)
const sessions = ref([])
const selected = ref(null)
const drafts = ref({})
const fileInput = ref(null)
const importedFile = ref(null)
const fileError = ref('')

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
    sessions.value = await api.mine()
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
  try {
    // advance=true：会话为 open 时让后端跑一轮 agent，首次打开即可看到追问
    selected.value = await api.session(row.session_id, true)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载会话详情失败')
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

async function submit() {
  if (!form.value.customer_id.trim() || !form.value.content.trim()) {
    ElMessage.warning('请填写客户标识与纪要正文')
    return
  }
  if (!form.value.idempotency_key.trim()) form.value.idempotency_key = `ui-${uid()}`
  loading.value = true
  try {
    await api.intake({ ...form.value, occurred_at: new Date(form.value.occurred_at).toISOString() })
    ElMessage.success('纪要已通过门禁并创建澄清会话')
    form.value.content = ''
    form.value.idempotency_key = ''
    importedFile.value = null
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '提交失败')
  } finally {
    loading.value = false
  }
}

async function sendAnswer(turn, question) {
  const value = (drafts.value[question.id] || '').trim()
  if (!value) return
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
  }
}

const statusTag = (s) => ({
  open: 'warning', needs_human_review: 'danger', ready_for_proposal: 'success',
  completed: 'info', cancelled: 'info',
}[s] || 'info')

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

          <el-button type="primary" :loading="loading" class="full" @click="submit">提交并进入澄清</el-button>

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
            </div>
          </template>

          <el-empty v-if="!loading && !sessions.length" description="还没有会话" :image-size="80" />

          <el-table v-else :data="sessions" highlight-current-row size="small" @row-click="openSession">
            <el-table-column prop="customer_id" label="客户" min-width="140" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag :type="statusTag(row.status)" size="small">
                  {{ SESSION_STATUS_LABELS[row.status] || row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="round_count" label="轮次" width="70" />
            <el-table-column prop="session_id" label="会话 ID" min-width="180" show-overflow-tooltip />
          </el-table>
        </el-card>

        <el-card v-if="selected" shadow="never" class="detail-card">
          <template #header>
            <div class="card-head">
              <strong>{{ selected.customer_id }}</strong>
              <el-tag :type="statusTag(selected.status)" size="small">
                {{ SESSION_STATUS_LABELS[selected.status] || selected.status }}
              </el-tag>
              <span class="muted">{{ selected.session_id }}</span>
            </div>
          </template>

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
                <el-input v-model="drafts[q.id]" size="small" placeholder="用一句话补充事实（已脱敏）" />
                <el-button size="small" type="primary" @click="sendAnswer(turn, q)">提交回答</el-button>
              </div>
            </div>
            <p v-if="!questionsOf(turn).length" class="muted turn-note">{{ turnNote(turn) }}</p>
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
:deep(.el-table__row) { cursor: pointer; }
</style>
