<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, View } from '@element-plus/icons-vue'
import { api, ApiError } from '../api'

const props = defineProps({ isReviewer: { type: Boolean, default: false } })

const loading = ref(false)
const pending = ref([])
const rejected = ref([])
const preview = ref({ open: false, path: '', content: '', loading: false })

const scoreField = (rec, key) => parseScores(rec)?.scores?.[key]

function parseScores(rec) {
  const s = rec?.ai_scores
  if (!s) return null
  if (typeof s === 'object') return s
  try { return JSON.parse(s) } catch { return null }
}

function concerns(rec) {
  const s = parseScores(rec)
  const list = s?.concerns
  return Array.isArray(list) ? list : []
}

const stats = computed(() => ({
  pending: pending.value.length,
  rejected: rejected.value.length,
  aiDone: pending.value.filter((r) => parseScores(r)).length,
}))

async function load() {
  loading.value = true
  try {
    const [p, r] = await Promise.all([api.reviewsPending(), api.reviewsRejected()])
    pending.value = p || []
    rejected.value = r || []
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载审核列表失败')
  } finally {
    loading.value = false
  }
}

async function approve(row) {
  try {
    const res = await api.approve(row.id)
    ElMessage.success(`已通过：${res.target_path || ''}`)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '操作失败')
  }
}

async function reject(row) {
  try {
    const { value } = await ElMessageBox.prompt('驳回原因（必填，将写入审核记录）', '驳回条目', {
      confirmButtonText: '确认驳回', cancelButtonText: '取消',
      inputValidator: (v) => (v && v.trim() ? true : '驳回原因不能为空'),
    })
    await api.reject(row.id, value.trim())
    ElMessage.success('已驳回')
    await load()
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err instanceof ApiError ? err.message : '操作失败')
  }
}

async function retryAi(row) {
  try {
    await api.retryAi(row.id)
    ElMessage.success('已加入 AI 审核队列')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '操作失败')
  }
}

async function resubmit(row) {
  try {
    await api.resubmit(row.id)
    ElMessage.success('已重新提交 AI 审核')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '操作失败')
  }
}

async function openPreview(row) {
  preview.value = { open: true, path: row.nexus_path, content: '', loading: true }
  try {
    const res = await api.entryContent(row.nexus_path)
    preview.value = { open: true, path: row.nexus_path, content: res.exists ? res.content : '（文件不存在，请用 Obsidian 打开 vault/ 查看）', loading: false }
  } catch (err) {
    preview.value.loading = false
    preview.value.content = `预览失败：${err instanceof ApiError ? err.message : '未知错误'}`
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>审核管理</h1>
        <p class="lede">AI 六维度评分 + 人工放行；通过后条目移入 NEXUS/ 并对全员可见。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-alert v-if="!props.isReviewer" type="warning" show-icon :closable="false"
              title="审核操作仅管理员 / 审核者可用（当前仅可查看列表）" class="mb" />

    <el-row :gutter="12" class="stat-row">
      <el-col :span="8"><el-card shadow="never"><div class="stat-label">待审核</div><div class="stat-value">{{ stats.pending }}</div></el-card></el-col>
      <el-col :span="8"><el-card shadow="never"><div class="stat-label">已完成 AI 评分</div><div class="stat-value">{{ stats.aiDone }}</div></el-card></el-col>
      <el-col :span="8"><el-card shadow="never"><div class="stat-label">已驳回（可重提）</div><div class="stat-value">{{ stats.rejected }}</div></el-card></el-col>
    </el-row>

    <h3 class="section">待审核（{{ pending.length }}）</h3>
    <el-empty v-if="!loading && !pending.length" description="暂无待审核条目" :image-size="80" />

    <el-card v-for="row in pending" :key="row.id" shadow="never" class="review-card">
      <template #header>
        <div class="card-head">
          <strong>{{ row.title || row.nexus_path.split('/').pop() }}</strong>
          <el-tag :type="row.ai_verdict === 'approved' ? 'success' : row.ai_verdict === 'rejected' ? 'danger' : 'info'" size="small">
            AI：{{ row.ai_verdict || '未完成' }}
          </el-tag>
          <el-tag v-if="row.ai_scores_valid === false" type="warning" size="small">契约校验未过</el-tag>
          <div class="spacer" />
          <el-button size="small" :icon="View" @click="openPreview(row)">预览正文</el-button>
        </div>
      </template>

      <template v-if="parseScores(row)">
        <el-descriptions :column="5" size="small" border>
          <el-descriptions-item label="完整性">{{ scoreField(row, 'completeness') ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="去重">{{ scoreField(row, 'dedup') ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="质量">{{ scoreField(row, 'quality') ?? '—' }}/5</el-descriptions-item>
          <el-descriptions-item label="敏感信息">{{ scoreField(row, 'sensitive') ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="合规">{{ scoreField(row, 'compliance') ?? '—' }}</el-descriptions-item>
        </el-descriptions>
        <p class="muted score-meta">
          职务归属：{{ parseScores(row).department || '—' }}
          <template v-if="parseScores(row).summary">｜摘要：{{ parseScores(row).summary }}</template>
        </p>
        <el-alert v-if="concerns(row).length" type="warning" show-icon :closable="false"
                  :title="'关注项：' + concerns(row).join('；')" class="mb" />
      </template>
      <el-alert v-else type="info" show-icon :closable="false"
                title="AI 审核未完成或失败——可人工审核，或点「重试 AI 审核」" class="mb" />

      <div class="actions">
        <el-button type="primary" :disabled="!props.isReviewer" @click="approve(row)">通过</el-button>
        <el-button type="danger" plain :disabled="!props.isReviewer" @click="reject(row)">驳回</el-button>
        <el-button :disabled="!props.isReviewer" @click="retryAi(row)">重试 AI 审核</el-button>
      </div>
    </el-card>

    <template v-if="rejected.length">
      <h3 class="section">已驳回（{{ rejected.length }}）</h3>
      <el-card v-for="row in rejected" :key="row.id" shadow="never" class="review-card">
        <div class="card-head">
          <strong>{{ row.title || row.nexus_path.split('/').pop() }}</strong>
          <span class="danger">驳回原因：{{ row.reject_reason || '—' }}</span>
          <div class="spacer" />
          <el-button size="small" :icon="View" @click="openPreview(row)">预览正文</el-button>
          <el-button size="small" type="primary" :disabled="!props.isReviewer" @click="resubmit(row)">重新提交审核</el-button>
        </div>
      </el-card>
    </template>

    <el-drawer v-model="preview.open" :title="preview.path" size="52%" direction="rtl">
      <div v-loading="preview.loading" class="preview"><pre>{{ preview.content }}</pre></div>
    </el-drawer>
  </div>
</template>

<style scoped>
.mb { margin-bottom: 14px; }
.stat-row { margin-bottom: 22px; }
.stat-label { font-size: 12px; color: var(--c-text-muted); }
.stat-value { margin-top: 6px; font-size: 26px; font-weight: 700; color: var(--c-text-strong); }
.section { margin: 22px 0 12px; font-size: 15px; color: var(--c-text); }
.review-card { margin-bottom: 14px; }
.card-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.spacer { flex: 1; }
.actions { display: flex; gap: 10px; margin-top: 14px; }
.score-meta { margin: 10px 0 0; }
.muted { color: var(--c-text-muted); font-size: 12px; }
.danger { color: var(--el-color-danger); font-size: 12px; }
.preview pre {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12.5px; line-height: 1.75; color: var(--c-text);
}
</style>
