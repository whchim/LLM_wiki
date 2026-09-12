<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { api, ApiError, STAGE_ORDER, STAGE_HINT } from '../api'

const emit = defineEmits(['go'])

const loading = ref(false)
const error = ref('')
const items = ref([])
const counts = ref({})
const stageFilter = ref('全部')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.myEntries()
    items.value = res.items || []
    counts.value = res.counts || {}
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

const shown = computed(() =>
  stageFilter.value === '全部' ? items.value : items.value.filter((it) => it.stage === stageFilter.value))

const stageOptions = computed(() => ['全部', ...STAGE_ORDER.filter((s) => counts.value[s])])

const stageTagType = (stage) => ({
  已发布: 'success', 待审核: 'warning', 已编译: 'info',
  编译中: 'primary', 编译失败: 'danger', 已驳回: 'danger', 已上传: 'info',
}[stage] || 'info')

const actionable = computed(() => shown.value.filter((it) => it.stage === '编译失败' || it.stage === '已驳回'))

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>我的知识库</h1>
        <p class="lede">从个人沉淀到企业共享：看得到自己提交的每一条现在到哪一步。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mb" />

    <el-row :gutter="12" class="stage-row">
      <el-col v-for="stage in STAGE_ORDER" :key="stage" :xs="12" :sm="8" :md="6" :lg="3">
        <el-card
          shadow="hover"
          :class="['stage-card', { dim: !counts[stage], picked: stageFilter === stage }]"
          @click="stageFilter = stageFilter === stage ? '全部' : stage"
        >
          <div class="stage-name">{{ stage }}</div>
          <div class="stage-count">{{ counts[stage] || 0 }}</div>
        </el-card>
      </el-col>
    </el-row>
    <p v-if="stageFilter !== '全部'" class="hint">{{ STAGE_HINT[stageFilter] }}（再次点击可取消筛选）</p>

    <el-empty v-if="!loading && !items.length" description="你还没有提交过任何文档">
      <el-button type="primary" @click="emit('go', 'upload')">去上传文档</el-button>
    </el-empty>

    <template v-else>
      <div class="filter-bar">
        <el-select v-model="stageFilter" size="small" style="width: 150px">
          <el-option v-for="s in stageOptions" :key="s" :label="s" :value="s" />
        </el-select>
        <span class="muted">共 {{ shown.length }} 条</span>
      </div>

      <el-table :data="shown" v-loading="loading" stripe style="width: 100%">
        <el-table-column prop="raw_name" label="名称" min-width="200" show-overflow-tooltip />
        <el-table-column label="阶段" width="105">
          <template #default="{ row }">
            <el-tag :type="stageTagType(row.stage)" size="small" effect="dark">{{ row.stage }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="编译" width="90">
          <template #default="{ row }">{{ row.compile_status || '—' }}</template>
        </el-table-column>
        <el-table-column label="审核" width="100">
          <template #default="{ row }">{{ row.review_decision || '—' }}</template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">{{ row.entry_type || '—' }}</template>
        </el-table-column>
        <el-table-column label="版本" width="80">
          <template #default="{ row }">{{ row.entry_version || '—' }}</template>
        </el-table-column>
        <el-table-column label="提交时间" width="170">
          <template #default="{ row }">{{ String(row.uploaded_at || '—').slice(0, 19) }}</template>
        </el-table-column>
        <el-table-column type="expand">
          <template #default="{ row }">
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="源文件">{{ row.raw_path }}</el-descriptions-item>
              <el-descriptions-item label="编译产物">{{ row.nexus_path || '—' }}</el-descriptions-item>
              <el-descriptions-item label="入库路径">{{ row.entry_path || '—' }}</el-descriptions-item>
              <el-descriptions-item v-if="row.compile_error" label="编译错误">
                <span class="danger">{{ row.compile_error }}</span>
              </el-descriptions-item>
              <el-descriptions-item v-if="row.reject_reason" label="驳回原因">
                <span class="danger">{{ row.reject_reason }}</span>
              </el-descriptions-item>
            </el-descriptions>
          </template>
        </el-table-column>
      </el-table>

      <el-card v-if="actionable.length" shadow="never" class="need-action">
        <template #header><strong>需要处理（{{ actionable.length }}）</strong></template>
        <div v-for="it in actionable" :key="it.raw_path" class="action-item">
          <el-tag :type="stageTagType(it.stage)" size="small" effect="dark">{{ it.stage }}</el-tag>
          <strong>{{ it.raw_name }}</strong>
          <span class="danger">{{ it.compile_error || it.reject_reason }}</span>
          <el-button v-if="it.stage === '编译失败'" size="small" @click="emit('go', 'upload')">去重试</el-button>
        </div>
      </el-card>
    </template>

    <el-collapse class="boundary">
      <el-collapse-item title="关于「我的」口径（已知边界）" name="1">
        <ul class="muted">
          <li>系统目前<strong>没有给知识条目记录归属人</strong>：<code>knowledge_entries</code> 无 owner/submitter 字段，<code>contributors</code> 表已建但尚未写入。</li>
          <li>因此本页「我的」= <strong>我上传过的</strong>（依据上传审计事件追溯），而不是「归我所有的」。</li>
          <li>条目一旦发布进入 <code>NEXUS/</code>，系统<strong>无法反查作者</strong>；支持按人过滤需给条目补归属字段（后续迭代）。</li>
          <li>审核结论按文件名关联，同名多次提交时取最近结论。</li>
        </ul>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<style scoped>
.mb { margin-bottom: 16px; }
.stage-row { margin-bottom: 6px; }
.stage-card { cursor: pointer; text-align: center; transition: opacity .15s; }
.stage-card.dim { opacity: .45; }
.stage-card.picked { border-color: var(--el-color-primary); }
.stage-name { font-size: 12px; color: #91a5c0; }
.stage-count { margin-top: 6px; font-size: 26px; font-weight: 700; color: #eaf4ff; }
.hint { margin: 6px 0 0; font-size: 12px; color: #7890ae; }
.filter-bar { display: flex; align-items: center; gap: 12px; margin: 18px 0 10px; }
.need-action { margin-top: 18px; }
.action-item { display: flex; align-items: center; gap: 10px; padding: 7px 0; font-size: 13px; }
.action-item .danger { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.danger { color: var(--el-color-danger); }
.boundary { margin-top: 24px; }
.boundary ul { margin: 0; padding-left: 20px; line-height: 1.9; font-size: 12px; }
.muted { color: #91a5c0; font-size: 12px; }
</style>
