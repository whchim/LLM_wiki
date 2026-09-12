import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles.css'
import App from './App.vue'

// Element Plus 深色模式：<html> 上加 dark 类，theme-chalk/dark 的 css-vars 才会生效
document.documentElement.classList.add('dark')

createApp(App).use(ElementPlus, { locale: zhCn }).mount('#app')
