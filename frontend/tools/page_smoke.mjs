/**
 * 页面 E2E 冒烟（真浏览器、真登录、真渲染）：`node tools/page_smoke.mjs`
 *
 * 为什么需要它：本轮踩到一个只有真页面才暴露的坑——图谱画布**全空**，而统计行还显示
 * "节点 0 · 关系 215"。根因是 Vue 里 `simNodes` 用了普通 `let` 数组，`visibleNodes` 这个 computed
 * 在首屏（数据未到）就被模板读了一次并永久缓存成空数组。**这类问题快照脚本、单测都抓不到**，
 * 必须真起浏览器看画布有没有画出东西。
 *
 * 用它做的事（无第三方下载：直接用系统已装的 Edge）：
 *   1) 调 API 登录拿 token，写进 localStorage；
 *   2) 打开工作台 → 切到「知识图谱」；
 *   3) 读统计行文本，并对 canvas 采样：**非背景像素占比**必须 > 0.2%；
 *   4) 截图落盘 tools/_shot_page.png 供人看。
 */
import { chromium } from 'playwright-core'
import fs from 'node:fs'

const APP = process.env.APP_URL || 'http://127.0.0.1:8501'
const API = process.env.API_URL || 'http://127.0.0.1:8000'
const USER = process.env.APP_USER || 'admin'
const PASS = process.env.APP_PASS || 'admin123'

const login = await fetch(`${API}/auth/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: USER, password: PASS }),
})
if (!login.ok) {
  console.error(`登录失败：HTTP ${login.status}（先确认 API 在 ${API} 上跑着）`)
  process.exit(1)
}
const auth = await login.json()

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })

await page.goto(APP)
await page.evaluate(([token, role, name]) => {
  localStorage.setItem('llmwiki_token', token)
  localStorage.setItem('llmwiki_auth', JSON.stringify({ token, role, username: name, display_name: name }))
}, [auth.access_token, auth.role, USER])
await page.reload()
await page.getByText('知识图谱', { exact: true }).first().click()
await page.waitForSelector('canvas', { timeout: 15000 })
await page.waitForTimeout(2500)          // 等力导向布局跑完 + 首帧绘制

const stats = await page.locator('.stat-line').innerText().catch(() => '(未找到统计行)')
const ink = await page.evaluate(() => {
  const canvas = document.querySelector('canvas')
  const ctx = canvas.getContext('2d')
  const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height)
  const bg = [data[0], data[1], data[2]]
  let diff = 0
  for (let i = 0; i < data.length; i += 4) {
    if (Math.abs(data[i] - bg[0]) + Math.abs(data[i + 1] - bg[1]) + Math.abs(data[i + 2] - bg[2]) > 24) diff += 1
  }
  return { total: data.length / 4, diff, ratio: diff / (data.length / 4) }
})

fs.writeFileSync('tools/_shot_page.png', await page.screenshot({ fullPage: false }))
await browser.close()

console.log(`统计行：${stats.replace(/\s+/g, ' ')}`)
console.log(`画布像素：非背景 ${ink.diff}/${ink.total}（${(ink.ratio * 100).toFixed(2)}%）`)
if (errors.length) console.log(`控制台错误：\n  - ${errors.slice(0, 5).join('\n  - ')}`)

const failures = []
if (ink.ratio < 0.002) failures.push(`画布几乎是空的（非背景像素仅 ${(ink.ratio * 100).toFixed(2)}%）`)
if (/节点\s*0\b/.test(stats)) failures.push('统计行显示节点数为 0')
if (errors.length) failures.push(`页面有 ${errors.length} 条控制台错误`)
if (failures.length) {
  console.error(`[FAIL] ${failures.join('；')}`)
  process.exit(1)
}
console.log('[OK] 图谱页渲染正常（画布有内容、无控制台错误）')
