import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles.css'
import './core/theme'
import App from './App.vue'

// 主题类由 src/core/theme.js 统一管理（含 index.html 内联脚本的首屏防闪）
createApp(App).use(ElementPlus, { locale: zhCn }).mount('#app')
