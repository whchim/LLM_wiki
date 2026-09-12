<script setup>
import { computed, onMounted, ref } from 'vue'
import { ArrowRight, Upload } from '@element-plus/icons-vue'
import { api, ApiError, SESSION_STATUS_LABELS } from '../api'

const props = defineProps({
  auth: { type: Object, required: true },
  isReviewer: { type: Boolean, default: false },
})
const emit = defineEmits(['go'])

const loading = ref(false)
const sessions = ref([])
const proposals = ref([])
const entriesTotal = ref(0)
const myCounts = ref({})

async function load() {
  loading.value = true
  try {
    const tasks = [api.mine(), api.myEntries(), api.entries({ limit: 1 })]
    if (props.isReviewer) tasks.push(api.proposals())
    const [s, my, all, prop] = await Promise.all(tasks)
    sessions.value = s || []
    myCounts.value = my?.counts || {}
    entriesTotal.value = all?.total || 0
    proposals.value = prop || []
  } catch (err) {
    // 总览是只读聚合，单点失败不打断整页
  } finally {
    loading.value = false
  }
}

const openSessions = computed(() => sessions.value.filter((s) => s.status === 'open').length)
const publishedMine = computed(() => myCounts.value['已发布'] || 0)
const pendingMine = computed(() => (myCounts.value['待审核'] || 0) + (myCounts.value['已编译'] || 0))
const today = new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })

onMounted(load)
</script>

<template>
  <div class="page" v-loading="loading">
    <header class="page-head">
      <div>
        <div class="eyebrow">{{ today }}</div>
        <h1>你好，{{ props.auth.display_name || props.auth.username }}。</h1>
        <p class="lede">先看事实与缺口，再决定今天补哪一块知识。</p>
      </div>
      <el-button type="primary" :icon="Upload" @click="emit('go', 'upload')">上传文档</el-button>
    </header>

    <el-row :gutter="12" class="stat-row">
      <el-col :xs="12" :md="6">
        <el-card shadow="never" class="stat-card clickable" @click="emit('go', 'my-kb')">
          <div class="stat-label">我提交的内容</div>
          <div class="stat-value">{{ publishedMine + pendingMine }}</div>
          <div class="stat-sub">已发布 {{ publishedMine }} · 流转中 {{ pendingMine }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :md="6">
        <el-card shadow="never" class="stat-card clickable" @click="emit('go', 'browse')">
          <div class="stat-label">企业知识库条目</div>
          <div class="stat-value">{{ entriesTotal }}</div>
          <div class="stat-sub">全部编译产物</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :md="6">
        <el-card shadow="never" class="stat-card clickable" @click="emit('go', 'sales')">
          <div class="stat-label">待澄清会话</div>
          <div class="stat-value">{{ openSessions }}</div>
          <div class="stat-sub">需要销售补充事实</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :md="6">
        <el-card v-if="props.isReviewer" shadow="never" class="stat-card clickable" @click="emit('go', 'customer')">
          <div class="stat-label">待负责人确认</div>
          <div class="stat-value">{{ proposals.length }}</div>
          <div class="stat-sub">状态不会自动跳转</div>
        </el-card>
        <el-card v-else shadow="never" class="stat-card">
          <div class="stat-label">事实边界</div>
          <div class="stat-value">100%</div>
          <div class="stat-sub">事实需人工确认</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="18">
      <el-col :xs="24" :lg="14">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <strong>最近澄清会话</strong>
              <el-button size="small" text type="primary" @click="emit('go', 'sales')">
                查看全部 <el-icon><ArrowRight /></el-icon>
              </el-button>
            </div>
          </template>
          <el-empty v-if="!sessions.length" description="还没有会话" :image-size="80">
            <el-button size="small" type="primary" @click="emit('go', 'sales')">提交第一条纪要</el-button>
          </el-empty>
          <el-table v-else :data="sessions.slice(0, 6)" size="small" @row-click="() => emit('go', 'sales')">
            <el-table-column prop="customer_id" label="客户" min-width="140" />
            <el-table-column label="状态" width="120">
              <template #default="{ row }">{{ SESSION_STATUS_LABELS[row.status] || row.status }}</template>
            </el-table-column>
            <el-table-column prop="session_id" label="会话" min-width="180" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="10">
        <el-card shadow="never" class="principle">
          <div class="quote-mark">“</div>
          <p>成熟的 Agent，不是能做最多事情的那个，而是在明确的边界里，能稳定完成正确事情的那个。</p>
          <span class="muted">— 产品原则 / 01</span>
        </el-card>
        <el-card shadow="never" class="quick">
          <template #header><strong>快速入口</strong></template>
          <div class="quick-list">
            <el-button text @click="emit('go', 'my-kb')">我的知识库（流转阶段）</el-button>
            <el-button text @click="emit('go', 'browse')">全部条目浏览</el-button>
            <el-button text @click="emit('go', 'upload')">上传文档</el-button>
            <el-button v-if="props.isReviewer" text @click="emit('go', 'review')">审核管理</el-button>
            <el-button v-if="props.isReviewer" text @click="emit('go', 'growth')">自增长看板</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.eyebrow { font-size: 12px; color: #7890ae; }
.stat-row { margin: 26px 0 18px; }
.stat-card { min-height: 124px; }
.stat-card.clickable { cursor: pointer; transition: border-color .15s; }
.stat-card.clickable:hover { border-color: var(--el-color-primary); }
.stat-label { font-size: 12px; color: #91a5c0; }
.stat-value { margin: 10px 0 6px; font-size: 32px; font-weight: 700; color: #eaf4ff; letter-spacing: -.02em; }
.stat-sub { font-size: 11px; color: #7890ae; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.principle { margin-bottom: 18px; }
.quote-mark { font-size: 40px; line-height: 1; color: var(--el-color-primary); opacity: .5; }
.principle p { margin: 6px 0 12px; font-size: 13.5px; line-height: 1.9; color: #cddcef; }
.quick-list { display: flex; flex-direction: column; align-items: flex-start; }
.quick-list .el-button { justify-content: flex-start; margin: 0; }
.muted { color: #91a5c0; font-size: 12px; }
:deep(.el-table__row) { cursor: pointer; }
</style>
