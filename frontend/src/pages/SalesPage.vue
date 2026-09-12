<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
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
  customer_id: '', idempotency_key: '', occurred_at: nowLocal(),
  source_type: 'meeting_note', source_ref: null, content: '',
})

function nowLocal() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
const uid = () => globalThis.crypto?.randomUUID?.() || `k-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`

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
    selected.value = await api.session(row.session_id)
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
    await api.answer(selected.value.session_id, {
      turn_id: turn.turn_id, question_id: question.id, answer_text_redacted: value,
    })
    drafts.value[question.id] = ''
    ElMessage.success('回答已追加保存')
    selected.value = await api.session(selected.value.session_id)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '保存回答失败')
  }
}

const statusTag = (s) => ({
  open: 'warning', needs_human_review: 'danger', ready_for_proposal: 'success',
  completed: 'info', cancelled: 'info',
}[s] || 'info')

const openCount = computed(() => sessions.value.filter((s) => s.status === 'open').length)

onMounted(load)
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
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="客户脱敏标识">
                  <el-input v-model="form.customer_id" placeholder="customer-demo-001" />
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
            <div class="turn-head">第 {{ turn.turn_no }} 轮</div>
            <div v-for="q in turn.questions || []" :key="q.id" class="question">
              <p class="q-text">{{ q.text || q.question }}</p>
              <div v-if="q.answer_text || q.answered" class="answered">
                已答：{{ q.answer_text || '（已记录）' }}
              </div>
              <div v-else class="answer-row">
                <el-input v-model="drafts[q.id]" size="small" placeholder="用一句话补充事实（已脱敏）" />
                <el-button size="small" type="primary" @click="sendAnswer(turn, q)">提交回答</el-button>
              </div>
            </div>
          </div>

          <el-empty v-if="!(selected.turns || []).length" description="该会话暂无澄清问题" :image-size="70">
            <p class="muted">问题由澄清 Agent 生成（模型端口尚未接入时会保持为空，见 SA-13）。</p>
          </el-empty>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.mb { margin-bottom: 12px; }
.full { width: 100%; }
.label-row { display: flex; align-items: center; justify-content: space-between; width: 100%; }
.hidden-file { display: none; }
.file-chip { display: inline-flex; align-items: center; gap: 6px; }
.card-head { display: flex; align-items: center; gap: 10px; }
.card-head .muted { margin-left: auto; }
.session-card { margin-bottom: 18px; }
.turn { margin-bottom: 18px; }
.turn-head { font-size: 12px; color: var(--c-text-dim); margin-bottom: 8px; }
.question { padding: 10px 12px; border: 1px solid var(--el-border-color); border-radius: 8px; margin-bottom: 8px; }
.q-text { margin: 0 0 8px; font-size: 13px; color: var(--c-text); }
.answered { font-size: 12px; color: var(--el-color-success); }
.answer-row { display: flex; gap: 8px; }
.muted { color: var(--c-text-muted); font-size: 12px; }
:deep(.el-table__row) { cursor: pointer; }
</style>
