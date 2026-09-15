<script setup>
/**
 * 对话窗口：就知识库提问，答案带引用溯源；答不出来时如实说明并沉淀成知识缺口。
 *
 * 交互契约（与后端 /ask 一致）：
 * - status=answered     → 正文 + 引用卡片（点开看原文）+ 检索依据 + 后续追问
 * - status=insufficient → 模型自己承认依据不足（同样算缺口，不硬答）
 * - status=no_hits      → 根本没检到依据，**后端没调模型**，提示去上传
 * - status=failed       → 契约违例/模型异常，明确报错，不给"看起来像答案"的文本
 * 对话历史只在前端内存里（后端不存会话），刷新即清空——列表页/多轮上下文见文档已知边界。
 */
import { nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatLineRound, Document, Delete, Promotion } from '@element-plus/icons-vue'
import { api, ApiError } from '../api'

const question = ref('')
const messages = ref([])          // {role, text, payload?, error?}
const sending = ref(false)
const topK = ref(6)
const listRef = ref(null)
const drawer = ref({ open: false, path: '', loading: false, content: '', exists: true })

const EXAMPLES = [
  '知识库里有哪几种部署模式？',
  '应急指挥平台的验收标准是什么？',
  '项目复盘的模板包含哪些内容？',
]

async function scrollToBottom() {
  await nextTick()
  const el = listRef.value?.wrapRef
  if (el) el.scrollTop = el.scrollHeight
}

async function ask(text) {
  const q = (text ?? question.value).trim()
  if (!q || sending.value) return
  messages.value.push({ role: 'user', text: q })
  question.value = ''
  sending.value = true
  await scrollToBottom()
  try {
    const payload = await api.ask(q, topK.value)
    messages.value.push({
      role: 'assistant',
      text: payload.answer || payload.error || '（本次没有返回内容）',
      payload,
    })
  } catch (err) {
    const message = err instanceof ApiError ? err.message : '提问失败，请稍后重试。'
    messages.value.push({ role: 'assistant', text: message, error: true })
    ElMessage.error(message)
  } finally {
    sending.value = false
    await scrollToBottom()
  }
}

function clearChat() {
  messages.value = []
  ElMessage.success('已清空当前对话（历史只存在本页内存中）')
}

async function openEntry(path) {
  drawer.value = { open: true, path, loading: true, content: '', exists: true }
  try {
    const res = await api.entryContent(path)
    drawer.value.content = res.content || ''
    drawer.value.exists = res.exists
  } catch (err) {
    drawer.value.content = err instanceof ApiError ? err.message : '读取失败'
    drawer.value.exists = false
  } finally {
    drawer.value.loading = false
  }
}

function statusMeta(payload) {
  if (!payload) return null
  if (payload.status === 'answered') return { type: 'success', text: '已依据检索结果作答' }
  if (payload.status === 'insufficient') return { type: 'warning', text: '依据不足：模型如实说明未作答（已记为知识缺口）' }
  if (payload.status === 'no_hits') return { type: 'info', text: '知识库暂无相关内容：本次未调用模型（已记为知识缺口）' }
  return { type: 'error', text: `作答失败：${payload.error || '未知原因'}` }
}

function channelText(channels) {
  if (!channels) return ''
  return `grep ${channels.grep ?? 0} · 向量 ${channels.vector ?? 0}`
}
</script>

<template>
  <div class="ask">
    <div class="head">
      <div>
        <h2><el-icon><ChatLineRound /></el-icon> 对话窗口</h2>
        <p class="muted">
          答案只依据知识库里「已发布」的条目生成，每条结论都能点开原文核对；
          检索不到就如实说"没有"，不猜、不用模型自己的知识凑数。
        </p>
      </div>
      <div class="head-actions">
        <el-select v-model="topK" size="small" style="width: 132px">
          <el-option :value="3" label="依据 3 条" />
          <el-option :value="6" label="依据 6 条" />
          <el-option :value="10" label="依据 10 条" />
        </el-select>
        <el-button :icon="Delete" size="small" :disabled="!messages.length" @click="clearChat">清空对话</el-button>
      </div>
    </div>

    <el-scrollbar ref="listRef" class="stream">
      <el-empty v-if="!messages.length" description="还没有对话，可以试着问：">
        <div class="examples">
          <el-tag
            v-for="ex in EXAMPLES"
            :key="ex"
            class="example"
            effect="plain"
            @click="ask(ex)"
          >{{ ex }}</el-tag>
        </div>
      </el-empty>

      <div v-for="(msg, i) in messages" :key="i" :class="['msg', msg.role]">
        <div v-if="msg.role === 'user'" class="bubble user-bubble">{{ msg.text }}</div>

        <div v-else class="bubble bot-bubble">
          <el-alert
            v-if="msg.error"
            type="error" show-icon :closable="false" :title="msg.text"
          />
          <template v-else>
            <div class="answer">{{ msg.text }}</div>

            <div v-if="msg.payload.citations?.length" class="block">
              <div class="block-title">引用来源（{{ msg.payload.citations.length }}）</div>
              <div
                v-for="(c, ci) in msg.payload.citations"
                :key="ci"
                class="citation"
                @click="openEntry(c.path)"
              >
                <el-icon><Document /></el-icon>
                <div class="citation-body">
                  <div class="citation-path">{{ c.path }}</div>
                  <div v-if="c.excerpt" class="citation-quote">…{{ c.excerpt }}…</div>
                  <div v-if="c.note" class="citation-note">支撑结论：{{ c.note }}</div>
                </div>
              </div>
            </div>

            <el-alert
              v-if="statusMeta(msg.payload)"
              class="block"
              :type="statusMeta(msg.payload).type"
              :title="statusMeta(msg.payload).text"
              show-icon
              :closable="false"
            />

            <el-collapse v-if="msg.payload.retrieved?.length" class="block">
              <el-collapse-item :title="`检索依据（${msg.payload.retrieved.length} 条 · ${channelText(msg.payload.channels)}）`">
                <div v-for="r in msg.payload.retrieved" :key="r.path" class="retrieved">
                  <span class="retrieved-title">{{ r.title || r.path }}</span>
                  <span class="muted">{{ r.path }} · 得分 {{ r.score }}</span>
                </div>
              </el-collapse-item>
            </el-collapse>

            <div v-if="msg.payload.followups?.length" class="block followups">
              <span class="muted">可能还想问：</span>
              <el-tag
                v-for="f in msg.payload.followups"
                :key="f"
                class="example"
                effect="plain"
                @click="ask(f)"
              >{{ f }}</el-tag>
            </div>

            <div class="meta muted">
              {{ msg.payload.model || '未调用模型' }} ·
              用时 {{ msg.payload.latency_ms }}ms ·
              token {{ msg.payload.usage?.input_tokens }} / {{ msg.payload.usage?.output_tokens }} ·
              trace {{ (msg.payload.trace_id || '').slice(0, 8) }}
            </div>
          </template>
        </div>
      </div>

      <div v-if="sending" class="msg assistant">
        <div class="bubble bot-bubble muted">正在检索知识库并生成答案…</div>
      </div>
    </el-scrollbar>

    <div class="composer">
      <el-input
        v-model="question"
        type="textarea"
        :rows="2"
        resize="none"
        maxlength="300"
        show-word-limit
        placeholder="问一个知识库里的问题（Enter 发送，Shift+Enter 换行）"
        @keydown.enter.exact.prevent="ask()"
      />
      <el-button type="primary" :icon="Promotion" :loading="sending" @click="ask()">发送</el-button>
    </div>

    <el-drawer v-model="drawer.open" :title="drawer.path" size="50%" direction="rtl">
      <div v-loading="drawer.loading" class="preview">
        <el-alert
          v-if="!drawer.exists && !drawer.loading"
          type="info" show-icon :closable="false"
          title="文件不存在（索引与文件可能不一致，可尝试重建索引）"
        />
        <pre v-else>{{ drawer.content }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.ask { display: flex; flex-direction: column; height: calc(100vh - 150px); }
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.head h2 { display: flex; align-items: center; gap: 8px; margin: 0 0 6px; font-size: 17px; color: var(--c-text-strong); }
.head-actions { display: flex; align-items: center; gap: 8px; }
.muted { color: var(--c-text-muted); font-size: 12px; }
.head p { margin: 0; max-width: 780px; line-height: 1.7; }

.stream { flex: 1; min-height: 0; margin: 16px 0; padding-right: 6px; }
.examples { display: flex; flex-direction: column; gap: 8px; align-items: center; }
.example { cursor: pointer; }

.msg { display: flex; margin-bottom: 14px; }
.msg.user { justify-content: flex-end; }
.bubble { max-width: 78%; padding: 12px 14px; border-radius: 10px; font-size: 13px; line-height: 1.8; }
.user-bubble { background: var(--el-color-primary); color: #fff; white-space: pre-wrap; word-break: break-word; }
.bot-bubble { width: 100%; background: var(--c-hover-bg); border: 1px solid var(--el-border-color); }
.answer { white-space: pre-wrap; word-break: break-word; color: var(--c-text); }

.block { margin-top: 12px; }
.block-title { margin-bottom: 6px; font-size: 12px; font-weight: 600; color: var(--c-text-strong); }
.citation {
  display: flex; gap: 8px; align-items: flex-start;
  margin-bottom: 8px; padding: 9px 11px;
  border: 1px solid var(--el-border-color); border-radius: 8px; cursor: pointer;
  background: var(--c-sidebar);
}
.citation:hover { border-color: var(--el-color-primary); }
.citation-body { min-width: 0; }
.citation-path { font-size: 12px; color: var(--c-brand-ink); word-break: break-all; }
.citation-quote { margin-top: 4px; font-size: 12px; color: var(--c-text-muted); line-height: 1.7; }
.citation-note { margin-top: 4px; font-size: 11px; color: var(--c-text-dim); }
.retrieved { display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; font-size: 12px; }
.retrieved-title { color: var(--c-text); }
.followups { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.meta { margin-top: 10px; font-size: 11px; }

.composer { display: flex; gap: 10px; align-items: flex-end; }
.preview pre {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12.5px; line-height: 1.75; color: var(--c-text);
}
</style>
