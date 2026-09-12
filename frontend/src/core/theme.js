// 主题：影响 <html class="dark">（Element Plus dark css-vars 依赖它），并持久化到 localStorage。
// 首次访问跟随系统 prefers-color-scheme；用户一旦手动切换即以选择为准。
//
// 注意：首屏防闪由 index.html 的内联脚本负责（在 Vue 挂载前应用 class），
// 这里只负责运行期切换与持久化——两条路径读取同一组键名。
import { ref, computed, watchEffect } from 'vue'

const STORAGE_KEY = 'llmwiki_theme' // 'light' | 'dark'

function preferred() {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

const theme = ref(preferred())

// 单一副作用：主题变化 → 同步 <html> 类与 localStorage
watchEffect(() => {
  const root = document.documentElement
  root.classList.toggle('dark', theme.value === 'dark')
  localStorage.setItem(STORAGE_KEY, theme.value)
})

// 跟随系统（仅当用户尚未手动选择时）
window.matchMedia?.('(prefers-color-scheme: light)').addEventListener?.('change', (e) => {
  if (!localStorage.getItem(STORAGE_KEY)) theme.value = e.matches ? 'light' : 'dark'
})

export function useTheme() {
  const isDark = computed(() => theme.value === 'dark')
  return {
    theme,
    isDark,
    toggle: () => { theme.value = theme.value === 'dark' ? 'light' : 'dark' },
    set: (v) => { theme.value = v },
  }
}
