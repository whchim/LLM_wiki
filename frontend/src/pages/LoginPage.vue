<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { api, ApiError } from '../api'

const emit = defineEmits(['authenticated'])

const form = ref({ username: 'admin', password: 'admin123' })
const loading = ref(false)
const error = ref('')

async function submit() {
  if (!form.value.username.trim() || !form.value.password) {
    error.value = '请输入用户名和密码。'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = await api.login(form.value.username.trim(), form.value.password)
    emit('authenticated', res, form.value.username.trim())
    ElMessage.success('已登录工作台')
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '登录失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-shell">
    <div class="auth-art">
      <div class="auth-mark">LLM WIKI / CONSOLE</div>
      <h1>让知识在入库时<br /><em>被理解一次</em>，<br />而不是每次查询重来。</h1>
      <p>编译式知识平台：文档入库即编译为结构化 Markdown，经六维度审核与人工放行进入企业知识库；之上落地销售客户状态 Agent。</p>
      <div class="auth-foot">文件为权威源 · 数据库为可重建缓存 · 事实需人工确认</div>
    </div>

    <el-card class="login-card" shadow="never">
      <div class="eyebrow">LLM WIKI / KNOWLEDGE OPS</div>
      <h2>进入工作台</h2>
      <p class="muted">用你的组织账号继续</p>

      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input v-model="form.username" :prefix-icon="User" autocomplete="username" size="large" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            :prefix-icon="Lock"
            type="password"
            show-password
            autocomplete="current-password"
            size="large"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="login-error" />
        <el-button type="primary" size="large" class="login-btn" :loading="loading" @click="submit">
          登录工作台
        </el-button>
      </el-form>

      <div class="login-note">默认账号 admin / admin123（生产环境请改）</div>
      <div class="login-note">所有状态变化均保留审计事件</div>
    </el-card>
  </div>
</template>

<style scoped>
.auth-shell {
  display: grid;
  grid-template-columns: 1.15fr .85fr;
  gap: 40px;
  align-items: center;
  min-height: 100vh;
  padding: 0 6vw;
  background: radial-gradient(circle at 18% 30%, rgba(64, 158, 255, .1), transparent 55%), var(--c-bg);
}

.auth-art h1 {
  margin: 20px 0 18px;
  font-size: clamp(30px, 3.2vw, 46px);
  line-height: 1.22;
  letter-spacing: -.03em;
  color: var(--c-text-strong);
}
.auth-art h1 em { color: var(--el-color-primary); font-style: normal; }
.auth-art p { max-width: 520px; color: var(--c-text-muted); font-size: 14px; line-height: 1.8; }
.auth-mark {
  display: inline-block; padding: 7px 12px; border-radius: 20px;
  background: rgba(64, 158, 255, .12); color: var(--el-color-primary);
  font-size: 11px; font-weight: 700; letter-spacing: .14em;
}
.auth-foot { margin-top: 26px; color: var(--c-text-faint); font-size: 12px; }

.login-card { max-width: 420px; background: var(--c-panel-bg); border-color: var(--el-border-color); }
.eyebrow { font-size: 11px; letter-spacing: .14em; color: var(--c-text-faint); }
.login-card h2 { margin: 12px 0 6px; font-size: 26px; color: var(--c-text-strong); }
.muted { margin-bottom: 18px; color: var(--c-text-muted); font-size: 13px; }
.login-error { margin-bottom: 14px; }
.login-btn { width: 100%; }
.login-note { margin-top: 14px; color: var(--c-text-faint); font-size: 12px; }

@media (max-width: 900px) {
  .auth-shell { grid-template-columns: 1fr; padding: 40px 20px; }
  .auth-art { display: none; }
}
</style>
