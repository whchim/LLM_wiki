<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api, ApiError, STATE_LABELS } from '../api'

const props = defineProps({ isReviewer: { type: Boolean, default: false } })

const loading = ref(false)
const proposals = ref([])
const selected = ref(null)
const events = ref([])
const eventsLoading = ref(false)

async function load() {
  loading.value = true
  try {
    proposals.value = await api.proposals()
    if (selected.value) {
      const still = proposals.value.find((p) => p.proposal_id === selected.value.proposal_id)
      if (!still) { selected.value = null; events.value = [] }
    }
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载待确认建议失败')
  } finally {
    loading.value = false
  }
}

async function open(row) {
  selected.value = { ...row }
  events.value = []
  eventsLoading.value = true
  try {
    events.value = await api.events(row.customer_id)
  } catch (err) {
    // 时间线不可用不阻断决策（证据引用仍可看）
  } finally {
    eventsLoading.value = false
  }
}

async function decide(decision) {
  if (!selected.value) return
  let reason = decision === 'approved' ? '负责人确认' : '负责人驳回'
  if (decision === 'rejected') {
    try {
      const { value } = await ElMessageBox.prompt('驳回原因（将记入状态事件）', '驳回状态建议', {
        confirmButtonText: '确认驳回', cancelButtonText: '取消',
        inputValidator: (v) => (v && v.trim() ? true : '驳回原因不能为空'),
      })
      reason = value.trim()
    } catch (err) {
      return
    }
  }
  try {
    await api.decide(selected.value.proposal_id, { decision, reason })
    ElMessage.success('负责人决定已记录')
    selected.value = null
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '操作失败')
  }
}

const confidence = (row) => Math.round(Number(row?.confidence || 0) * 100)
const stateLabel = (s) => STATE_LABELS[s] || s || '未确认'

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>客户状态</h1>
        <p class="lede">首屏只展示建议状态、风险和下一步；确认动作写入不可变状态事件。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-alert v-if="!props.isReviewer" type="warning" show-icon :closable="false"
              title="确认状态需负责人 / 管理员权限（当前仅可查看）" class="mb" />

    <el-row :gutter="18">
      <el-col :xs="24" :lg="9">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <strong>待确认建议</strong>
              <el-tag size="small">{{ proposals.length }}</el-tag>
            </div>
          </template>
          <el-empty v-if="!loading && !proposals.length" description="暂无待确认建议" :image-size="80">
            <p class="muted">澄清完成后进入状态建议；模型端口未接入时保持为空。</p>
          </el-empty>
          <el-table v-else :data="proposals" highlight-current-row size="small" @row-click="open">
            <el-table-column label="建议状态" width="110">
              <template #default="{ row }">
                <el-tag type="primary" size="small" effect="dark">{{ stateLabel(row.proposed_state) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="customer_id" label="客户" min-width="120" />
            <el-table-column label="置信度" width="90">
              <template #default="{ row }">{{ confidence(row) }}%</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="15">
        <el-card v-if="selected" shadow="never">
          <template #header>
            <div class="card-head">
              <strong>{{ stateLabel(selected.proposed_state) }}</strong>
              <span class="muted">{{ selected.customer_id }} · {{ selected.proposal_id }}</span>
              <div class="spacer" />
              <el-progress type="circle" :width="54" :percentage="confidence(selected)" />
            </div>
          </template>

          <el-descriptions :column="1" border size="small" class="brief">
            <el-descriptions-item label="判断摘要">{{ selected.reasoning_summary || '—' }}</el-descriptions-item>
            <el-descriptions-item label="下一步动作">{{ selected.next_action || '—' }}</el-descriptions-item>
            <el-descriptions-item label="当前状态">{{ stateLabel(selected.current_state) }}</el-descriptions-item>
          </el-descriptions>

          <el-alert v-if="(selected.risk_flags || []).length" type="warning" show-icon :closable="false"
                    :title="'风险提示：' + selected.risk_flags.join('、')" class="mb" />

          <el-collapse class="blocks">
            <el-collapse-item title="证据引用" name="evidence">
              <pre class="json">{{ JSON.stringify(selected.evidence_refs || [], null, 2) }}</pre>
            </el-collapse-item>
            <el-collapse-item :title="`状态事件历史（${events.length}）`" name="events">
              <div v-loading="eventsLoading">
                <el-timeline v-if="events.length">
                  <el-timeline-item v-for="ev in events" :key="ev.event_id || ev.id"
                                    :timestamp="ev.effective_at || ev.created_at" placement="top">
                    <strong>{{ stateLabel(ev.state) }}</strong>
                    <span class="muted"> · {{ ev.created_by || '—' }}</span>
                    <div v-if="ev.reason" class="muted">{{ ev.reason }}</div>
                  </el-timeline-item>
                </el-timeline>
                <p v-else class="muted">暂无事件记录。</p>
              </div>
            </el-collapse-item>
          </el-collapse>

          <div class="actions">
            <el-button type="primary" :disabled="!props.isReviewer" @click="decide('approved')">确认状态</el-button>
            <el-button type="danger" plain :disabled="!props.isReviewer" @click="decide('rejected')">驳回建议</el-button>
          </div>
        </el-card>

        <el-empty v-else description="从左侧选择一条待确认建议" :image-size="90" />
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.mb { margin-bottom: 14px; }
.card-head { display: flex; align-items: center; gap: 10px; }
.spacer { flex: 1; }
.brief { margin-bottom: 14px; }
.blocks { margin-bottom: 16px; }
.json {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; color: #d7e1ee;
}
.actions { display: flex; gap: 10px; }
.muted { color: #91a5c0; font-size: 12px; }
:deep(.el-table__row) { cursor: pointer; }
</style>
