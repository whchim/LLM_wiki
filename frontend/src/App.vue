<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  DataAnalysis, DataBoard, Document, FolderOpened, Refresh, Search,
  Setting, UploadFilled, UserFilled, ChatDotRound, Checked, SwitchButton,
} from '@element-plus/icons-vue'
import { api, ApiError, ROLE_LABELS } from './api'

import LoginPage from './pages/LoginPage.vue'
import OverviewPage from './pages/OverviewPage.vue'
import MyKbPage from './pages/MyKbPage.vue'
import BrowsePage from './pages/BrowsePage.vue'
import UploadPage from './pages/UploadPage.vue'
import ReviewPage from './pages/ReviewPage.vue'
import GrowthPage from './pages/GrowthPage.vue'
import ObservabilityPage from './pages/ObservabilityPage.vue'
import SalesPage from './pages/SalesPage.vue'
import CustomerStatePage from './pages/CustomerStatePage.vue'

const auth = ref(JSON.parse(localStorage.getItem('llmwiki_auth') || 'null'))
const view = ref('overview')
const loading = ref(false)

const isReviewer = computed(() => ['admin', 'reviewer'].includes(auth.value?.role))
const isAdmin = computed(() => auth.value?.role === 'admin')
const roleLabel = computed(() => ROLE_LABELS[auth.value?.role] || '访客')

// 导航结构：知识域为主（项目根基），销售域为落地应用
const NAV = [
  { key: 'overview', label: '工作总览', icon: DataBoard, group: '知识库' },
  { key: 'my-kb', label: '我的知识库', icon: FolderOpened, group: '知识库' },
  { key: 'browse', label: '全部条目', icon: Document, group: '知识库' },
  { key: 'upload', label: '上传文档', icon: UploadFilled, group: '知识库' },
  { key: 'review', label: '审核管理', icon: Checked, group: '知识库', reviewerOnly: true },
  { key: 'growth', label: '自增长看板', icon: DataAnalysis, group: '洞察', reviewerOnly: true },
  { key: 'observability', label: '可观测性', icon: Setting, group: '洞察', reviewerOnly: true },
  { key: 'sales', label: '销售澄清', icon: ChatDotRound, group: '销售应用' },
  { key: 'customer', label: '客户状态', icon: UserFilled, group: '销售应用', reviewerOnly: true },
]
const visibleNav = computed(() => NAV.filter((n) => !n.reviewerOnly || isReviewer.value))
const groups = computed(() => [...new Set(visibleNav.value.map((n) => n.group))])

function setAuth(result, username) {
  auth.value = {
    token: result.access_token, username,
    role: result.role, display_name: result.display_name,
  }
  localStorage.setItem('llmwiki_token', result.access_token)
  localStorage.setItem('llmwiki_auth', JSON.stringify(auth.value))
}

function logout() {
  localStorage.removeItem('llmwiki_token')
  localStorage.removeItem('llmwiki_auth')
  auth.value = null
  view.value = 'overview'
}

/** 全局搜索：命中则跳到条目浏览并带上查询词 */
const searchQuery = ref('')
function doSearch() {
  if (!searchQuery.value.trim()) return
  view.value = 'browse'
}

function notifyError(err) {
  const message = err instanceof ApiError ? err.message : '操作失败，请稍后重试。'
  ElMessage.error(message)
}

const rebuildLoading = ref(false)
async function rebuildIndex() {
  rebuildLoading.value = true
  try {
    const res = await api.rebuildIndex()
    ElMessage.success(`索引已重建：${res.entries} 条`)
  } catch (err) {
    notifyError(err)
  } finally {
    rebuildLoading.value = false
  }
}

onMounted(() => { if (auth.value) view.value = 'overview' })

defineExpose({ notifyError })
</script>

<template>
  <LoginPage v-if="!auth" @authenticated="(res, username) => { setAuth(res, username); view = 'overview' }" />

  <el-container v-else class="app-shell">
    <el-aside width="232px" class="sidebar">
      <div class="brand">
        <div class="brand-icon"><Document /></div>
        <div>
          <strong>LLM Wiki</strong>
          <span>knowledge console</span>
        </div>
      </div>

      <el-input
        v-model="searchQuery"
        placeholder="搜索知识库"
        :prefix-icon="Search"
        clearable
        class="sidebar-search"
        @keyup.enter="doSearch"
      />

      <el-scrollbar class="nav-scroll">
        <template v-for="group in groups" :key="group">
          <div class="nav-group">{{ group }}</div>
          <div
            v-for="item in visibleNav.filter((n) => n.group === group)"
            :key="item.key"
            :class="['nav-item', { active: view === item.key }]"
            @click="view = item.key"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
          </div>
        </template>
      </el-scrollbar>

      <div class="sidebar-foot">
        <el-button
          v-if="isAdmin"
          :loading="rebuildLoading"
          :icon="Refresh"
          size="small"
          class="foot-btn"
          @click="rebuildIndex"
        >重建索引</el-button>
        <div class="user-chip">
          <div class="avatar">{{ (auth.display_name || auth.username).slice(0, 1).toUpperCase() }}</div>
          <div class="user-meta">
            <strong>{{ auth.display_name || auth.username }}</strong>
            <span>{{ roleLabel }}</span>
          </div>
          <el-tooltip content="退出登录" placement="top">
            <el-button :icon="SwitchButton" size="small" text @click="logout" />
          </el-tooltip>
        </div>
      </div>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <div class="crumbs">
          <span class="crumb-brand">LLM WIKI</span>
          <span class="slash">/</span>
          <span>{{ visibleNav.find((n) => n.key === view)?.label }}</span>
        </div>
      </el-header>

      <el-main class="main">
        <OverviewPage v-if="view === 'overview'" :auth="auth" :is-reviewer="isReviewer" @go="(v) => (view = v)" />
        <MyKbPage v-else-if="view === 'my-kb'" @go="(v) => (view = v)" />
        <BrowsePage v-else-if="view === 'browse'" :initial-query="searchQuery" />
        <UploadPage v-else-if="view === 'upload'" @go="(v) => (view = v)" />
        <ReviewPage v-else-if="view === 'review'" :is-reviewer="isReviewer" />
        <GrowthPage v-else-if="view === 'growth'" />
        <ObservabilityPage v-else-if="view === 'observability'" />
        <SalesPage v-else-if="view === 'sales'" />
        <CustomerStatePage v-else-if="view === 'customer'" :is-reviewer="isReviewer" />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-shell { min-height: 100vh; }

.sidebar {
  display: flex;
  flex-direction: column;
  padding: 20px 14px 14px;
  border-right: 1px solid var(--el-border-color);
  background: #0d1726;
}

.brand { display: flex; align-items: center; gap: 10px; padding: 0 8px 20px; }
.brand-icon {
  display: grid; place-items: center;
  width: 32px; height: 32px; border-radius: 9px;
  background: var(--el-color-primary); color: #062225;
  font-size: 18px;
}
.brand strong { display: block; font-size: 15px; color: #e7f5f4; }
.brand span { display: block; margin-top: 2px; font-size: 10px; letter-spacing: .08em; color: #7187a6; text-transform: uppercase; }

.sidebar-search { margin-bottom: 14px; }
:deep(.sidebar-search .el-input__wrapper) { background: rgba(7, 16, 29, .72); }

.nav-scroll { flex: 1; min-height: 0; }
.nav-group {
  padding: 14px 10px 6px;
  font-size: 10px; letter-spacing: .15em; text-transform: uppercase; color: #657b99;
}
.nav-item {
  display: flex; align-items: center; gap: 10px;
  margin: 2px 0; padding: 9px 11px;
  border-radius: 8px; cursor: pointer;
  color: #91a5c0; font-size: 13px;
  transition: background .15s, color .15s;
}
.nav-item:hover { color: #e7f5f4; background: rgba(64, 158, 255, .1); }
.nav-item.active { color: #e7f5f4; background: rgba(64, 158, 255, .16); box-shadow: inset 2px 0 0 var(--el-color-primary); }

.sidebar-foot { padding-top: 12px; border-top: 1px solid var(--el-border-color); }
.foot-btn { width: 100%; margin-bottom: 10px; }
.user-chip { display: flex; align-items: center; gap: 9px; padding: 4px; }
.avatar {
  display: grid; place-items: center;
  width: 30px; height: 30px; border-radius: 50%;
  background: var(--el-color-warning); color: #08212a;
  font-size: 12px; font-weight: 800;
}
.user-meta { flex: 1; min-width: 0; }
.user-meta strong { display: block; font-size: 12px; color: #d7e1ee; }
.user-meta span { display: block; margin-top: 2px; font-size: 11px; color: #7890ae; }

.topbar {
  display: flex; align-items: center;
  height: 62px;
  border-bottom: 1px solid var(--el-border-color);
}
.crumbs { font-size: 13px; color: #a7b8cd; }
.crumb-brand { color: var(--el-color-primary); font-size: 11px; font-weight: 700; letter-spacing: .15em; }
.slash { margin: 0 10px; color: #526984; }

.main { padding: 26px 30px 60px; }
</style>
