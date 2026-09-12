<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api, ApiError } from '../api'

const loading = ref(false)
const data = ref(null)

async function load() {
  loading.value = true
  try {
    data.value = await api.observability()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载可观测性数据失败')
  } finally {
    loading.value = false
  }
}

const successRate = () => {
  const r = data.value?.search?.success_rate
  return r === null || r === undefined ? '—' : `${(r * 100).toFixed(0)}%`
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>可观测性</h1>
        <p class="lede">编译 / 检索 / 审核的埋点指标（trace_events），用于发现延迟与失败模式。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <div v-loading="loading">
      <el-row :gutter="12" class="stat-row">
        <el-col :span="6">
          <el-card shadow="never">
            <div class="stat-label">当日编译会话</div>
            <div class="stat-value">{{ data?.compile?.sessions ?? 0 }}</div>
            <div class="stat-sub">编译文件 {{ data?.compile?.files ?? 0 }} / 缓存命中 {{ data?.compile?.cached ?? 0 }}</div>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never">
            <div class="stat-label">检索成功数</div>
            <div class="stat-value">{{ data?.search?.ok ?? 0 }}</div>
            <div class="stat-sub">失败 {{ data?.search?.error ?? 0 }} / 总数 {{ data?.search?.total ?? 0 }}</div>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never">
            <div class="stat-label">编译平均延迟</div>
            <div class="stat-value">{{ data?.compile?.avg_ms ?? 0 }}<small> ms</small></div>
            <div class="stat-sub">当日 compile_session</div>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never">
            <div class="stat-label">检索成功率</div>
            <div class="stat-value">{{ successRate() }}</div>
            <div class="stat-sub">span_type = search</div>
          </el-card>
        </el-col>
      </el-row>

      <h3 class="section">平均响应延迟（按类型）</h3>
      <el-empty v-if="!loading && !(data?.latency || []).length" description="暂无 trace 数据" :image-size="80">
        <p class="muted">上传 / 检索 / 审核操作后这里才会有指标。</p>
      </el-empty>
      <el-table v-else :data="data?.latency || []" stripe>
        <el-table-column prop="label" label="类型" min-width="160" />
        <el-table-column prop="avg_ms" label="平均延迟 (ms)" width="150" />
        <el-table-column prop="count" label="次数" width="110" />
      </el-table>

      <h3 class="section">Top 失败模式</h3>
      <el-empty v-if="!loading && !(data?.top_errors || []).length" description="暂无失败记录" :image-size="80" />
      <el-table v-else :data="data?.top_errors || []" stripe>
        <el-table-column prop="label" label="类型" min-width="140" />
        <el-table-column prop="error" label="错误" min-width="240" show-overflow-tooltip />
        <el-table-column prop="count" label="次数" width="100" />
      </el-table>

      <p class="muted note">统计日期：{{ data?.date || '—' }}（当日编译按服务器日期零点起算）</p>
    </div>
  </div>
</template>

<style scoped>
.stat-row { margin-bottom: 20px; }
.stat-label { font-size: 12px; color: var(--c-text-muted); }
.stat-value { margin-top: 6px; font-size: 28px; font-weight: 700; color: var(--c-text-strong); }
.stat-value small { font-size: 13px; font-weight: 500; color: var(--c-text-muted); }
.stat-sub { margin-top: 6px; font-size: 11px; color: var(--c-text-dim); }
.section { margin: 22px 0 12px; font-size: 15px; color: var(--c-text); }
.note { margin-top: 20px; }
.muted { color: var(--c-text-muted); font-size: 12px; }
</style>
