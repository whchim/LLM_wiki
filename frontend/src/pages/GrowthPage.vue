<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api, ApiError } from '../api'

const loading = ref(false)
const stats = ref({})
const missed = ref([])
const reportKind = ref('growth')
const report = ref({ exists: false, content: '', name: '' })
const reportLoading = ref(false)

async function load() {
  loading.value = true
  try {
    const [s, m] = await Promise.all([api.searchStats(), api.searchMissed(20)])
    stats.value = s || {}
    missed.value = m.items || []
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载看板数据失败')
  } finally {
    loading.value = false
  }
}

async function loadReport() {
  reportLoading.value = true
  try {
    report.value = await api.reports(reportKind.value)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载周报失败')
  } finally {
    reportLoading.value = false
  }
}

const missRate = () => {
  const r = Number(stats.value.miss_rate || 0)
  return `${(r * 100).toFixed(0)}%`
}

onMounted(async () => { await load(); await loadReport() })
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>自增长看板</h1>
        <p class="lede">搜索未命中即知识缺口——缺口 Top 20 驱动补文档，形成闭环。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-row :gutter="12" class="stat-row">
      <el-col :span="8">
        <el-card shadow="never"><div class="stat-label">总搜索次数</div><div class="stat-value">{{ stats.total || 0 }}</div></el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never"><div class="stat-label">未命中次数</div><div class="stat-value">{{ stats.miss_count || 0 }}</div></el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never"><div class="stat-label">未命中率</div><div class="stat-value">{{ missRate() }}</div></el-card>
      </el-col>
    </el-row>

    <h3 class="section">搜索未命中 Top 20（知识缺口）</h3>
    <el-empty v-if="!loading && !missed.length" description="暂无知识缺口记录" :image-size="80">
      <p class="muted">用户搜索都有结果时，这里为空。</p>
    </el-empty>
    <el-table v-else :data="missed" v-loading="loading" stripe>
      <el-table-column prop="query" label="缺口查询" min-width="200" />
      <el-table-column prop="cnt" label="搜索次数" width="110" />
      <el-table-column prop="last_seen" label="最近出现" min-width="180" />
    </el-table>

    <h3 class="section">周报</h3>
    <el-radio-group v-model="reportKind" class="report-switch" @change="loadReport">
      <el-radio-button value="growth">自增长周报</el-radio-button>
      <el-radio-button value="health">健康周报</el-radio-button>
    </el-radio-group>
    <el-button size="small" :loading="reportLoading" @click="loadReport">重新读取</el-button>

    <el-card shadow="never" class="report-card" v-loading="reportLoading">
      <template v-if="report.exists">
        <div class="report-name">{{ report.name }}</div>
        <pre class="report-body">{{ report.content }}</pre>
      </template>
      <el-empty v-else description="尚无周报" :image-size="70">
        <p class="muted">
          周报由 Claude Code 在宿主机生成：自增长执行 <code>/process-growth</code>，
          健康巡检执行 <code>/health-check</code>（见 workflows/）。
        </p>
      </el-empty>
    </el-card>
  </div>
</template>

<style scoped>
.stat-row { margin-bottom: 22px; }
.stat-label { font-size: 12px; color: var(--c-text-muted); }
.stat-value { margin-top: 6px; font-size: 30px; font-weight: 700; color: var(--c-text-strong); }
.section { margin: 22px 0 12px; font-size: 15px; color: var(--c-text); }
.report-switch { margin-right: 12px; }
.report-card { margin-top: 14px; }
.report-name { margin-bottom: 10px; font-size: 12px; color: var(--c-text-dim); }
.report-body {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12.5px; line-height: 1.75; color: var(--c-text);
}
.muted { color: var(--c-text-muted); font-size: 12px; line-height: 1.8; }
</style>
