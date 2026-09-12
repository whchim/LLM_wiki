<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, UploadFilled } from '@element-plus/icons-vue'
import { api, ApiError } from '../api'

const emit = defineEmits(['go'])

const CATEGORIES = ['个人_notes', '会议', '经验', '项目']

const files = ref([])
const category = ref('个人_notes')
const uploading = ref(false)
const errors = ref([])
const tasks = ref([])
const tasksLoading = ref(false)

async function loadTasks() {
  tasksLoading.value = true
  try {
    tasks.value = await api.listTasks(50)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '获取任务列表失败')
  } finally {
    tasksLoading.value = false
  }
}

// el-upload 手动模式：只收集文件，由「上传」按钮统一提交，便于选择分类
function onChange(file, fileList) { files.value = fileList.map((f) => f.raw).filter(Boolean) }
function onRemove(file, fileList) { files.value = fileList.map((f) => f.raw).filter(Boolean) }

async function submit() {
  if (!files.value.length) {
    ElMessage.warning('请先选择文件')
    return
  }
  uploading.value = true
  errors.value = []
  try {
    const fd = new FormData()
    files.value.forEach((f) => fd.append('files', f, f.name))
    fd.append('category', category.value)
    const res = await api.upload(fd)
    errors.value = res.errors || []
    if (res.ok) {
      ElMessage.success(`${res.ok} 个文件已加入编译队列${errors.value.length ? `（${errors.value.length} 个失败）` : ''}`)
      files.value = []
      await loadTasks()
    }
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '上传失败')
  } finally {
    uploading.value = false
  }
}

async function retry(row) {
  try {
    await api.retryTask(row.id)
    ElMessage.success('已重新加入编译队列')
    await loadTasks()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '重试失败')
  }
}

const failedCount = computed(() => tasks.value.filter((t) => t.status === 'failed').length)
const statusTag = (s) => ({
  done: 'success', pending: 'info', processing: 'primary', failed: 'danger', cached: 'success',
}[s] || 'info')

onMounted(loadTasks)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>上传文档</h1>
        <p class="lede">落盘 RAW/ 并写入触发队列；Claude Code（宿主机 watcher）消费后自动编译。</p>
      </div>
      <el-button :icon="Refresh" :loading="tasksLoading" @click="loadTasks">刷新任务</el-button>
    </header>

    <el-card shadow="never" class="upload-card">
      <el-upload
        drag
        multiple
        :auto-upload="false"
        :file-list="[]"
        accept=".md,.txt,.pdf,.docx"
        :on-change="onChange"
        :on-remove="onRemove"
      >
        <el-icon class="upload-icon"><UploadFilled /></el-icon>
        <div class="upload-text">把文档拖到这里，或<em>点击选择</em></div>
        <template #tip>
          <div class="upload-tip">支持 .md / .txt / .pdf / .docx，单文件 ≤10MB。文件会以“不覆盖”方式落盘，同名文件将被拒绝。</div>
        </template>
      </el-upload>

      <div class="submit-bar">
        <span class="muted">来源分类</span>
        <el-select v-model="category" style="width: 150px">
          <el-option v-for="c in CATEGORIES" :key="c" :label="c" :value="c" />
        </el-select>
        <el-button type="primary" :loading="uploading" :disabled="!files.length" @click="submit">
          上传并加入编译队列{{ files.length ? `（${files.length}）` : '' }}
        </el-button>
      </div>

      <el-alert
        v-for="(msg, i) in errors" :key="i" :title="msg" type="error" show-icon
        :closable="false" class="err-item"
      />
    </el-card>

    <el-card shadow="never" class="task-card">
      <template #header>
        <div class="card-head">
          <strong>编译任务状态</strong>
          <el-tag v-if="failedCount" type="danger" size="small" effect="dark">{{ failedCount }} 个失败</el-tag>
        </div>
      </template>

      <el-empty v-if="!tasksLoading && !tasks.length" description="暂无编译任务" :image-size="80">
        <p class="muted">上传文件后，宿主机 watcher 会消费触发队列并自动编译。</p>
      </el-empty>

      <el-table v-else :data="tasks" v-loading="tasksLoading" stripe size="small">
        <el-table-column prop="id" label="任务" width="70" />
        <el-table-column prop="raw_path" label="文件" min-width="220" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="错误" min-width="180">
          <template #default="{ row }">
            <span v-if="row.error_msg" class="danger">{{ row.error_msg }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="完成时间" width="170">
          <template #default="{ row }">{{ row.completed_at || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button v-if="row.status === 'failed'" size="small" type="primary" text @click="retry(row)">重试</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.upload-card { margin-bottom: 20px; }
.upload-icon { font-size: 46px; color: var(--el-color-primary); }
.upload-text { margin-top: 8px; color: #b7c5d9; font-size: 14px; }
.upload-text em { color: var(--el-color-primary); font-style: normal; }
.upload-tip { margin-top: 8px; color: #7890ae; font-size: 12px; line-height: 1.7; }
.submit-bar { display: flex; align-items: center; gap: 12px; margin-top: 16px; }
.err-item { margin-top: 10px; }
.card-head { display: flex; align-items: center; gap: 10px; }
.muted { color: #91a5c0; font-size: 12px; }
.danger { color: var(--el-color-danger); }
</style>
