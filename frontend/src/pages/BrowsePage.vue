<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { api, ApiError, ENTRY_TYPE_LABELS, ENTRY_STATUS_LABELS } from '../api'

const props = defineProps({ initialQuery: { type: String, default: '' } })

const loading = ref(false)
const error = ref('')
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const typeFilter = ref('')
const statusFilter = ref('')
const keyword = ref(props.initialQuery || '')

const drawer = ref({ open: false, path: '', content: '', exists: false, loading: false })

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.entries({
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
      type_: typeFilter.value || undefined,
      status: statusFilter.value || undefined,
    })
    rows.value = res.items || []
    total.value = res.total || 0
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

// 后端 /entries 无关键词过滤，前端按标题/路径本地筛（总量小，够用）
const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return rows.value
  return rows.value.filter((r) =>
    (r.title || '').toLowerCase().includes(k) || (r.path || '').toLowerCase().includes(k))
})

const typeOptions = computed(() =>
  Object.entries(ENTRY_TYPE_LABELS).map(([value, label]) => ({ value, label })))
const statusOptions = computed(() =>
  Object.entries(ENTRY_STATUS_LABELS).map(([value, label]) => ({ value, label })))

const statusTag = (s) => ({
  active: 'success', pending: 'warning', draft: 'info', stale: 'warning', deprecated: 'info',
}[s] || 'info')

async function openPreview(row) {
  drawer.value = { open: true, path: row.path, content: '', exists: false, loading: true }
  try {
    const res = await api.entryContent(row.path)
    drawer.value = { open: true, path: row.path, content: res.content || '', exists: res.exists, loading: false }
  } catch (err) {
    drawer.value.loading = false
    ElMessage.error(err instanceof ApiError ? err.message : '预览失败')
  }
}

watch([typeFilter, statusFilter], () => { page.value = 1; load() })
watch(page, load)
watch(() => props.initialQuery, (v) => { if (v) keyword.value = v })
onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>全部知识条目</h1>
        <p class="lede">企业知识库的编译产物（不分作者）；点任意行可预览 Markdown 正文。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mb" />

    <div class="filter-bar">
      <el-input v-model="keyword" placeholder="按标题或路径筛选" :prefix-icon="Search" clearable style="width: 240px" />
      <el-select v-model="typeFilter" placeholder="全部类型" clearable style="width: 140px">
        <el-option v-for="o in typeOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 140px">
        <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <span class="muted">共 {{ total }} 条</span>
    </div>

    <el-empty v-if="!loading && !total" description="知识库为空">
      <p class="muted">上传文档并完成编译审核后，条目会出现在这里。</p>
    </el-empty>

    <template v-else>
      <el-table :data="filtered" v-loading="loading" stripe style="width: 100%" @row-click="openPreview">
        <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
        <el-table-column label="类型" width="90">
          <template #default="{ row }">{{ ENTRY_TYPE_LABELS[row.type] || row.type }}</template>
        </el-table-column>
        <el-table-column label="部门" width="100">
          <template #default="{ row }">{{ row.department || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)" size="small">{{ ENTRY_STATUS_LABELS[row.status] || row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="version" label="版本" width="80" />
        <el-table-column prop="updated_at" label="更新" width="110" />
        <el-table-column prop="path" label="路径" min-width="220" show-overflow-tooltip />
      </el-table>

      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        class="pager"
      />
    </template>

    <el-drawer v-model="drawer.open" :title="drawer.path" size="52%" direction="rtl">
      <div v-loading="drawer.loading" class="preview">
        <el-alert v-if="!drawer.exists && !drawer.loading" type="info" show-icon :closable="false"
                  title="文件不存在（索引与文件可能不一致，可尝试重建索引）" />
        <pre v-else>{{ drawer.content }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.mb { margin-bottom: 16px; }
.filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.muted { color: var(--c-text-muted); font-size: 12px; }
.pager { margin-top: 16px; justify-content: flex-end; }
.preview pre {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12.5px; line-height: 1.75; color: var(--c-text);
}
:deep(.el-table__row) { cursor: pointer; }
</style>
