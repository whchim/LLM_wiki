<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api, ApiError, STATE_LABELS } from '../api'

const props = defineProps({ isReviewer: { type: Boolean, default: false } })

const loading = ref(false)
const proposals = ref([])
const customers = ref([])
const selected = ref(null)      // 待确认建议
const customer = ref(null)      // 当前查看的客户
const mode = ref('')            // '' | 'proposal' | 'customer'
const events = ref([])
const eventsLoading = ref(false)

async function load() {
  loading.value = true
  try {
    proposals.value = await api.proposals()
    if (selected.value) {
      const still = proposals.value.find((p) => p.proposal_id === selected.value.proposal_id)
      if (!still) { selected.value = null; if (mode.value === 'proposal') mode.value = '' }
    }
    try {
      customers.value = await api.customers()
      if (customer.value) {
        const fresh = customers.value.find((c) => c.customer_id === customer.value.customer_id)
        if (fresh) customer.value = fresh
      }
    } catch (err) {
      customers.value = []      // 总览不可用时不阻断待确认建议
    }
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载待确认建议失败')
  } finally {
    loading.value = false
  }
}

async function loadEvents(customerId) {
  events.value = []
  eventsLoading.value = true
  try {
    events.value = await api.events(customerId)
  } catch (err) {
    // 时间线不可用不阻断决策（证据引用仍可看）
  } finally {
    eventsLoading.value = false
  }
}

async function open(row) {
  mode.value = 'proposal'
  selected.value = { ...row }
  await loadEvents(row.customer_id)
}

/** 查看客户当前阶段：确认动作的"下文"就在这里 */
async function openCustomer(row) {
  mode.value = 'customer'
  customer.value = { ...row }
  await loadEvents(row.customer_id)
}

async function decide(decision) {
  if (!selected.value) return
  const customerId = selected.value.customer_id
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
    const res = await api.decide(selected.value.proposal_id, { decision, reason })
    const finalCustomer = res.customer_id || customerId
    selected.value = null
    await load()
    if (decision === 'rejected') {
      ElMessage.info('已驳回：客户阶段保持不变（该建议不会再出现在待确认列表）')
      mode.value = ''
      customer.value = null
      return
    }
    const updated = customers.value.find((c) => c.customer_id === finalCustomer)
    ElMessage.success(`已记录：客户 ${finalCustomer} 现在是「${stateLabel(updated?.state || res.state)}」`)
    await openCustomer(updated || { customer_id: finalCustomer, state: res.state })
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '操作失败')
  }
}

const confidence = (row) => Math.round(Number(row?.confidence || 0) * 100)
const stateLabel = (s) => STATE_LABELS[s] || s || '未确认'
const fmtTime = (v) => (v ? String(v).replace('T', ' ').slice(0, 16) : '—')

/** 风险标识与建议类型用中文展示（后端契约里的枚举值不直接给用户看） */
const RISK_LABELS = {
  low_confidence: '置信度偏低', conflicting_signals: '存在冲突信号',
  missing_strong_evidence: '缺少强证据', customer_identity_uncertain: '客户身份不确定',
  sensitive_content_detected: '疑似敏感内容',
}
const riskText = (row) => (row?.risk_flags || []).map((f) => RISK_LABELS[f] || f).join('、')
const decisionLabel = (row) => ({ propose: '建议推进', needs_review: '需人工判断', reject: '建议驳回' }[row?.decision] || row?.decision || '')
const decisionTag = (row) => ({ propose: 'success', needs_review: 'warning', reject: 'danger' }[row?.decision] || 'info')

const evidenceCount = computed(() => (selected.value?.evidence_refs || []).length)

/** 证据来源说人话——技术键名（initial_note / question-1）不给运营人员看 */
function sourceLabel(source) {
  if (!source) return '原文片段'
  if (source === 'initial_note') return '洽谈纪要原文'
  if (/^question-\d+$/.test(String(source))) return '追问的回答'
  return String(source)
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>客户状态</h1>
        <p class="lede">左上是待你来确认的建议；左下可随时查每个客户现在到了哪一步（确认后的结果立即体现）。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-alert v-if="!props.isReviewer" type="warning" show-icon :closable="false"
              title="确认状态需负责人 / 管理员权限（当前仅可查看）" class="mb" />

    <el-row :gutter="18">
      <el-col :xs="24" :lg="9">
        <el-card shadow="never" class="mb">
          <template #header>
            <div class="card-head">
              <strong>待确认建议</strong>
              <el-tag size="small">{{ proposals.length }}</el-tag>
            </div>
          </template>
          <el-empty v-if="!loading && !proposals.length" description="暂无待确认建议" :image-size="80">
            <p class="muted">在「销售澄清」把会话推进到「可生成建议」或「已转人工」后，点「生成建议交负责人」，建议会出现在这里。</p>
          </el-empty>
          <el-table v-else :data="proposals" highlight-current-row size="small" @row-click="open">
            <el-table-column label="建议阶段" width="110">
              <template #default="{ row }">
                <el-tag type="primary" size="small" effect="dark">{{ stateLabel(row.proposed_state) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="100">
              <template #default="{ row }">
                <el-tag :type="decisionTag(row)" size="small">{{ decisionLabel(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="customer_id" label="客户" min-width="120" show-overflow-tooltip />
            <el-table-column label="置信度" width="90">
              <template #default="{ row }">{{ confidence(row) }}%</template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <strong>客户当前状态</strong>
              <el-tag size="small">{{ customers.length }}</el-tag>
            </div>
          </template>
          <el-empty v-if="!loading && !customers.length" description="暂无客户" :image-size="70" />
          <el-table v-else :data="customers" highlight-current-row size="small" @row-click="openCustomer">
            <el-table-column label="当前阶段" width="120">
              <template #default="{ row }">
                <el-tag v-if="row.state" type="success" size="small">{{ stateLabel(row.state) }}</el-tag>
                <el-tag v-else type="info" size="small">尚未确认</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="customer_id" label="客户" min-width="120" show-overflow-tooltip />
            <el-table-column label="待确认" width="80">
              <template #default="{ row }">
                <span v-if="row.pending_proposals" class="pending-badge">{{ row.pending_proposals }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="15">
        <!-- 待确认建议详情 -->
        <el-card v-if="mode === 'proposal' && selected" shadow="never">
          <template #header>
            <div class="card-head">
              <strong>{{ stateLabel(selected.proposed_state) }}</strong>
              <el-tag :type="decisionTag(selected)" size="small">{{ decisionLabel(selected) }}</el-tag>
              <span class="muted">{{ selected.customer_id }} · {{ selected.proposal_id }}</span>
              <div class="spacer" />
              <el-progress type="circle" :width="54" :percentage="confidence(selected)" />
            </div>
          </template>

          <el-descriptions :column="1" border size="small" class="brief">
            <el-descriptions-item label="判断摘要">{{ selected.reasoning_summary || '—' }}</el-descriptions-item>
            <el-descriptions-item label="下一步动作">{{ selected.next_action || '—' }}</el-descriptions-item>
            <el-descriptions-item label="当前阶段">{{ selected.current_state ? stateLabel(selected.current_state) : '尚未确认' }}</el-descriptions-item>
          </el-descriptions>

          <el-alert v-if="(selected.risk_flags || []).length" type="warning" show-icon :closable="false"
                    :title="'风险提示：' + riskText(selected)" class="mb" />

          <el-collapse class="blocks">
            <el-collapse-item :title="`证据引用（${evidenceCount}）`" name="evidence">
              <div v-if="evidenceCount" class="ev-list">
                <div v-for="(item, index) in selected.evidence_refs" :key="index" class="ev-item">
                  <div class="ev-head">
                    <span class="ev-index">{{ index + 1 }}</span>
                    <el-tag size="small" type="info" effect="plain">{{ sourceLabel(item.source) }}</el-tag>
                  </div>
                  <blockquote class="ev-quote">{{ item.quote || '（无原文片段）' }}</blockquote>
                  <p v-if="item.meaning" class="ev-meaning">这条说明：{{ item.meaning }}</p>
                </div>
              </div>
              <p v-else class="muted">没有可展示的证据引用。</p>

              <el-collapse class="raw-block">
                <el-collapse-item title="技术细节（开发/审计用）" name="raw">
                  <pre class="json">{{ JSON.stringify(selected.evidence_refs || [], null, 2) }}</pre>
                </el-collapse-item>
              </el-collapse>
            </el-collapse-item>
            <el-collapse-item :title="`状态事件历史（${events.length}）`" name="events">
              <div v-loading="eventsLoading">
                <el-timeline v-if="events.length">
                  <el-timeline-item v-for="ev in events" :key="ev.event_id || ev.id"
                                    :timestamp="fmtTime(ev.effective_at || ev.created_at)" placement="top">
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

        <!-- 客户当前阶段（确认后的结果落点） -->
        <el-card v-else-if="mode === 'customer' && customer" shadow="never">
          <template #header>
            <div class="card-head">
              <strong>{{ customer.customer_id }}</strong>
              <el-tag v-if="customer.state" type="success" size="small" effect="dark">{{ stateLabel(customer.state) }}</el-tag>
              <el-tag v-else type="info" size="small">尚未确认阶段</el-tag>
            </div>
          </template>

          <el-descriptions :column="1" border size="small" class="brief">
            <el-descriptions-item label="当前阶段">
              {{ customer.state ? stateLabel(customer.state) : '尚未确认（等待负责人确认第一条建议）' }}
            </el-descriptions-item>
            <el-descriptions-item label="生效时间">{{ fmtTime(customer.effective_at) }}</el-descriptions-item>
            <el-descriptions-item label="有效期至">
              {{ customer.valid_until ? fmtTime(customer.valid_until) : '不自动过期 / 尚无阶段' }}
            </el-descriptions-item>
            <el-descriptions-item label="待确认建议">{{ customer.pending_proposals || 0 }} 条</el-descriptions-item>
          </el-descriptions>

          <p class="muted tip">阶段只由负责人确认写入；这里显示的是由状态事件重建的投影。</p>

          <el-collapse class="blocks">
            <el-collapse-item :title="`状态事件历史（${events.length}）`" name="events">
              <div v-loading="eventsLoading">
                <el-timeline v-if="events.length">
                  <el-timeline-item v-for="ev in events" :key="ev.event_id || ev.id"
                                    :timestamp="fmtTime(ev.effective_at || ev.created_at)" placement="top">
                    <strong>{{ stateLabel(ev.state) }}</strong>
                    <span class="muted"> · {{ ev.created_by || '—' }}</span>
                    <div v-if="ev.reason" class="muted">{{ ev.reason }}</div>
                  </el-timeline-item>
                </el-timeline>
                <p v-else class="muted">该客户还没有状态事件（第一条建议确认后会出现）。</p>
              </div>
            </el-collapse-item>
          </el-collapse>

          <div class="actions">
            <el-button @click="mode = ''; customer = null">返回列表</el-button>
          </div>
        </el-card>

        <el-empty v-else description="从左侧选择一条待确认建议，或点一个客户查看当前阶段" :image-size="90" />
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
.tip { margin: 0 0 12px; }
.pending-badge {
  display: inline-block; min-width: 18px; padding: 0 6px; border-radius: 9px;
  background: var(--el-color-warning-light-8); color: var(--el-color-warning);
  font-size: 12px; text-align: center;
}
/* 证据引用：给运营人员看的版本——先原文片段，再一句中文说明，技术字段收进"技术细节" */
.ev-list { display: flex; flex-direction: column; gap: 12px; }
.ev-item { border: 1px solid var(--el-border-color); border-radius: 8px; padding: 10px 12px; }
.ev-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.ev-index {
  width: 20px; height: 20px; border-radius: 50%; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--el-color-primary-light-8); color: var(--el-color-primary);
  font-size: 12px;
}
.ev-quote {
  margin: 0 0 6px; padding: 6px 10px; font-size: 13px;
  border-left: 3px solid var(--el-color-primary-light-5);
  background: var(--el-fill-color-light); border-radius: 0 6px 6px 0;
  white-space: pre-wrap; word-break: break-word;
}
.ev-meaning { margin: 0; font-size: 12px; color: var(--c-text-muted); }
.raw-block { margin-top: 10px; }
.json {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; color: var(--c-text);
}
.actions { display: flex; gap: 10px; }
.muted { color: var(--c-text-muted); font-size: 12px; }
:deep(.el-table__row) { cursor: pointer; }
</style>
