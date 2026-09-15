<script setup>
/**
 * 知识图谱（对齐 Obsidian 图谱的交互与观感）。
 *
 * 为什么是 canvas + d3-force：Obsidian 自己的图谱就是 **力导向物理 + canvas/WebGL 渲染**，
 * 交互上有四件必备能力——**缩放/平移**、**拖拽节点**、**悬停高亮邻域并淡出其余**、
 * **物理参数滑杆**（中心力/斥力/连线力/连线距离）。早先那版是手写的简化布局 + 固定视口，
 * 既不能缩放、悬停也没有邻域高亮（用户实测反馈），所以这里换成 Obsidian 同款技术栈。
 *
 * 数据来自 GET /graph（`core/graph.py` 从 Markdown 派生，含**知识库保留文件** index/log/SCHEMA）：
 * - 节点：条目（concept/resource/research/glossary）+ 保留文件 + 待审 + 可选 RAW 原始语料；
 * - 虚线空心节点 = **待建页面**（被引用但还没建立，wiki 的"红链"，即最精确的知识缺口）。
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Link, Refresh, Search, ZoomIn, ZoomOut, Rank } from '@element-plus/icons-vue'
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from 'd3-force'
import { api, ApiError } from '../api'

// 节点配色（对齐 Obsidian 的"按分组着色"；保留文件与待建页面用醒目色区分）
const KIND_META = {
  concept: { label: '概念页', color: '#52c41a' },
  resource: { label: '资源摘要', color: '#409eff' },
  research: { label: '研究', color: '#9254de' },
  glossary: { label: '术语', color: '#13c2c2' },
  index: { label: '索引（保留文件）', color: '#faad14' },
  log: { label: '编译日志（保留文件）', color: '#fa8c16' },
  schema: { label: '知识库规范（保留文件）', color: '#eb2f96' },
  raw: { label: '原始语料', color: '#8c8c8c' },
  missing: { label: '待建页面', color: '#d4380d' },
}
const ALL_KINDS = Object.keys(KIND_META)

const graph = shallowRef({ nodes: [], edges: [], missing: [], stats: {} })
const loading = ref(false)
const includePending = ref(true)
const includeMeta = ref(true)
const includeRaw = ref(false)
const showLabels = ref('auto')            // auto=缩放足够近或悬停时显示 / always / never
const hiddenKinds = ref([])               // 被过滤掉的节点类型

// Obsidian 同款物理参数
const physics = ref({ center: 0.5, repel: 260, link: 0.6, distance: 90, collide: true })
const stats = ref({ fps: 0, drawn: 0 })

const wrap = ref(null)
const canvasRef = ref(null)
const hover = ref(null)                   // 悬停节点（含邻域信息，用于右侧详情面板）
const selected = ref(null)
const drawer = ref({ open: false, path: '', loading: false, content: '', exists: true })

let ctx = null
let sim = null
let simNodes = []
let simLinks = []
let rafId = 0
let ro = null
const view = { scale: 1, tx: 0, ty: 0 }
const size = { w: 900, h: 620 }
const drag = { active: false, node: null, moved: false, panning: false, sx: 0, sy: 0, stx: 0, sty: 0 }

const visibleNodes = computed(() => simNodes.filter((n) => !hiddenKinds.value.includes(n.kind)))
const byId = computed(() => new Map(visibleNodes.value.map((n) => [n.id, n])))

const neighborIds = computed(() => {
  const id = hover.value?.node?.id || selected.value?.id
  if (!id) return null
  const set = new Set([id])
  simLinks.forEach((l) => {
    const s = typeof l.source === 'object' ? l.source.id : l.source
    const t = typeof l.target === 'object' ? l.target.id : l.target
    if (s === id) set.add(t)
    if (t === id) set.add(s)
  })
  return set
})

const radiusOf = (n) => (n.kind === 'missing' ? 4 : Math.min(22, 4.5 + Math.sqrt(n.degree || 0) * 2.6))
const colorOf = (n) => (KIND_META[n.kind] || KIND_META.resource).color

// ---------- 数据 → 仿真 ----------

function buildSim() {
  const raw = graph.value
  const nodes = raw.nodes
    .filter((n) => !hiddenKinds.value.includes(n.kind))
    .map((n) => ({ ...n }))
  const linkSource = raw.edges.filter((e) => e.resolved || e.target.startsWith('missing:'))
  // 待建页面：作为**虚线空心节点**进入仿真（Obsidian 的 unresolved links 同款）
  raw.missing.forEach((m) => {
    if (!hiddenKinds.value.includes('missing')) {
      nodes.push({ id: m.id, title: m.title, kind: 'missing', degree: m.count, path: null,
                   missing: true, sources: m.sources })
    }
  })
  const ids = new Set(nodes.map((n) => n.id))
  const links = linkSource
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ source: e.source, target: e.target, kind: e.kind, resolved: e.resolved }))

  simNodes = nodes
  simLinks = links
  restartSim(true)
}

function restartSim(fresh = false) {
  if (sim) sim.stop()
  const w = size.w; const h = size.h
  sim = forceSimulation(simNodes)
    .force('link', forceLink(simLinks).id((d) => d.id)
      .distance(physics.value.distance).strength(physics.value.link))
    .force('charge', forceManyBody().strength(-physics.value.repel))
    .force('center', forceCenter(w / 2, h / 2).strength(physics.value.center))
    .force('x', forceX(w / 2).strength(0.03))
    .force('y', forceY(h / 2).strength(0.03))
    .force('collide', physics.value.collide ? forceCollide().radius((d) => radiusOf(d) + 4) : null)
    .on('tick', draw)
  if (fresh) sim.alpha(1).restart()
}

function applyPhysics() {
  if (!sim) return
  sim.force('link').distance(physics.value.distance).strength(physics.value.link)
  sim.force('charge').strength(-physics.value.repel)
  sim.force('center').strength(physics.value.center)
  sim.force('collide', physics.value.collide ? forceCollide().radius((d) => radiusOf(d) + 4) : null)
  sim.alpha(0.6).restart()
}

// ---------- 渲染 ----------

function resize() {
  const el = wrap.value
  if (!el || !canvasRef.value) return
  const dpr = window.devicePixelRatio || 1
  size.w = el.clientWidth
  size.h = el.clientHeight
  canvasRef.value.width = Math.max(1, Math.floor(size.w * dpr))
  canvasRef.value.height = Math.max(1, Math.floor(size.h * dpr))
  canvasRef.value.style.width = `${size.w}px`
  canvasRef.value.style.height = `${size.h}px`
  ctx = canvasRef.value.getContext('2d')
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  if (sim) { sim.force('center', forceCenter(size.w / 2, size.h / 2).strength(physics.value.center)); draw() }
}

function draw() {
  if (!ctx) return
  const dpr = window.devicePixelRatio || 1
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, canvasRef.value.width, canvasRef.value.height)
  ctx.setTransform(view.scale * dpr, 0, 0, view.scale * dpr, view.tx * dpr, view.ty * dpr)

  const focus = neighborIds.value
  const nodes = visibleNodes.value
  const nodeById = new Map(nodes.map((n) => [n.id, n]))

  // 连线：邻域高亮，其余淡出
  simLinks.forEach((l) => {
    const s = typeof l.source === 'object' ? l.source : nodeById.get(l.source)
    const t = typeof l.target === 'object' ? l.target : nodeById.get(l.target)
    if (!s || !t || !nodeById.has(s.id) || !nodeById.has(t.id)) return
    const inFocus = focus && (focus.has(s.id) || focus.has(t.id))
    if (focus && !inFocus) return
    ctx.beginPath()
    ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y)
    ctx.strokeStyle = l.resolved === false ? 'rgba(212,56,13,0.45)'
      : (inFocus ? 'rgba(120,170,255,0.95)' : 'rgba(255,255,255,0.16)')
    ctx.lineWidth = (inFocus ? 1.6 : 0.7) / view.scale
    if (l.resolved === false) ctx.setLineDash([4 / view.scale, 3 / view.scale])
    ctx.stroke()
    ctx.setLineDash([])
  })

  // 结点
  const showAllLabels = showLabels.value === 'always'
  const labelsByZoom = showLabels.value === 'auto' && view.scale > 0.85
  nodes.forEach((n) => {
    const r = radiusOf(n)
    const dim = focus ? !focus.has(n.id) : false
    ctx.globalAlpha = dim ? 0.2 : 1
    ctx.beginPath()
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2)
    if (n.kind === 'missing') {
      ctx.fillStyle = 'rgba(0,0,0,0.25)'
      ctx.fill()
      ctx.strokeStyle = colorOf(n)
      ctx.lineWidth = 1.4 / view.scale
      ctx.setLineDash([3 / view.scale, 2 / view.scale]); ctx.stroke(); ctx.setLineDash([])
    } else {
      ctx.fillStyle = colorOf(n)
      ctx.fill()
      if (n.id === selected.value?.id || n.id === hover.value?.node?.id) {
        ctx.strokeStyle = '#ffffff'
        ctx.lineWidth = 2 / view.scale
        ctx.stroke()
      }
    }
    const focused = focus && focus.has(n.id)
    if (showAllLabels || labelsByZoom || focused || n.kind === 'missing' || (n.is_meta && view.scale > 0.5)) {
      ctx.globalAlpha = dim ? 0.25 : 1
      ctx.font = `${(n.is_meta || n.kind === 'missing' ? 11.5 : 11) / view.scale}px "Segoe UI", "Microsoft YaHei", sans-serif`
      ctx.fillStyle = focused ? '#ffffff' : 'rgba(226,232,240,0.86)'
      ctx.textAlign = 'center'
      const label = n.title.length > 16 ? `${n.title.slice(0, 16)}…` : n.title
      ctx.fillText(label, n.x, n.y - r - 4 / view.scale)
    }
    ctx.globalAlpha = 1
  })
  stats.value = { drawn: nodes.length, fps: 0 }
}

// ---------- 交互 ----------

function toGraph(px, py) {
  return { x: (px - view.tx) / view.scale, y: (py - view.ty) / view.scale }
}

function nodeAt(px, py) {
  const p = toGraph(px, py)
  let best = null; let bestD = Infinity
  visibleNodes.value.forEach((n) => {
    const d = Math.hypot(n.x - p.x, n.y - p.y)
    const r = radiusOf(n) + 6 / view.scale
    if (d <= r && d < bestD) { best = n; bestD = d }
  })
  return best
}

function onWheel(e) {
  e.preventDefault()
  const rect = canvasRef.value.getBoundingClientRect()
  const px = e.clientX - rect.left; const py = e.clientY - rect.top
  const factor = Math.exp(-e.deltaY * 0.0016)
  const next = Math.min(6, Math.max(0.15, view.scale * factor))
  view.tx = px - (px - view.tx) * (next / view.scale)
  view.ty = py - (py - view.ty) * (next / view.scale)
  view.scale = next
  draw()
}

function zoomBy(factor) {
  const px = size.w / 2; const py = size.h / 2
  const next = Math.min(6, Math.max(0.15, view.scale * factor))
  view.tx = px - (px - view.tx) * (next / view.scale)
  view.ty = py - (py - view.ty) * (next / view.scale)
  view.scale = next
  draw()
}

function fitView() {
  const nodes = visibleNodes.value
  if (!nodes.length) return
  const xs = nodes.map((n) => n.x); const ys = nodes.map((n) => n.y)
  const minX = Math.min(...xs); const maxX = Math.max(...xs)
  const minY = Math.min(...ys); const maxY = Math.max(...ys)
  const pad = 70
  const scale = Math.min(4, Math.max(0.15,
    Math.min((size.w - pad * 2) / Math.max(maxX - minX, 1), (size.h - pad * 2) / Math.max(maxY - minY, 1))))
  view.scale = scale
  view.tx = size.w / 2 - ((minX + maxX) / 2) * scale
  view.ty = size.h / 2 - ((minY + maxY) / 2) * scale
  draw()
}

function focusNode(id) {
  const n = simNodes.find((x) => x.id === id)
  if (!n) return
  selected.value = n
  view.scale = Math.max(view.scale, 1.2)
  view.tx = size.w / 2 - n.x * view.scale
  view.ty = size.h / 2 - n.y * view.scale
  draw()
}

function onDown(e) {
  const rect = canvasRef.value.getBoundingClientRect()
  const px = e.clientX - rect.left; const py = e.clientY - rect.top
  const n = nodeAt(px, py)
  drag.sx = e.clientX; drag.sy = e.clientY; drag.moved = false
  if (n) {
    drag.active = true; drag.node = n
    n.fx = n.x; n.fy = n.y
    sim.alphaTarget(0.25).restart()
  } else {
    drag.panning = true
    drag.stx = view.tx; drag.sty = view.ty
  }
  canvasRef.value.setPointerCapture(e.pointerId)
}

function onMove(e) {
  const rect = canvasRef.value.getBoundingClientRect()
  const px = e.clientX - rect.left; const py = e.clientY - rect.top
  if (drag.active && drag.node) {
    const p = toGraph(px, py)
    drag.node.fx = p.x; drag.node.fy = p.y
    drag.moved = true
    return
  }
  if (drag.panning) {
    view.tx = drag.stx + (e.clientX - drag.sx)
    view.ty = drag.sty + (e.clientY - drag.sy)
    drag.moved = true
    draw()
    return
  }
  const n = nodeAt(px, py)
  const changed = (n?.id || null) !== (hover.value?.node?.id || null)
  if (changed) {
    hover.value = n ? { node: n, neighbors: neighborOf(n.id) } : null
    draw()
  }
}

function neighborOf(id) {
  const out = []
  simLinks.forEach((l) => {
    const s = typeof l.source === 'object' ? l.source : byId.value.get(l.source)
    const t = typeof l.target === 'object' ? l.target : byId.value.get(l.target)
    if (!s || !t) return
    if (s.id === id) out.push(t)
    else if (t.id === id) out.push(s)
  })
  return out
}

function onUp(e) {
  if (drag.active && drag.node) {
    // 拖拽后**保持固定**（Obsidian 同款手感：你放哪儿它就在哪儿），用「释放固定」清空
    sim.alphaTarget(0)
    if (!drag.moved) { selected.value = drag.node; openEntry(drag.node.path) }
    drag.active = false; drag.node = null
    return
  }
  if (drag.panning) {
    drag.panning = false
    if (!drag.moved) { selected.value = null; hover.value = null; draw() }
  }
  if (e?.pointerId !== undefined) {
    try { canvasRef.value.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
  }
}

function releasePins() {
  simNodes.forEach((n) => { n.fx = null; n.fy = null })
  sim.alpha(0.8).restart()
  ElMessage.success('已释放全部固定节点')
}

async function openEntry(path) {
  if (!path) return
  drawer.value = { open: true, path, loading: true, content: '', exists: true }
  try {
    const res = await api.entryContent(path)
    drawer.value.content = res.content || ''
    drawer.value.exists = res.exists
  } catch (err) {
    drawer.value.content = err instanceof ApiError ? err.message : '读取失败'
    drawer.value.exists = false
  } finally {
    drawer.value.loading = false
  }
}

async function load() {
  loading.value = true
  try {
    graph.value = await api.graph({ includePending: includePending.value,
                                    includeMeta: includeMeta.value,
                                    includeRaw: includeRaw.value })
    buildSim()
    setTimeout(fitView, 600)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '图谱加载失败')
  } finally {
    loading.value = false
  }
}

const focusText = ref('')
function doFocus() {
  const q = focusText.value.trim()
  if (!q) return
  const hit = simNodes.find((n) => n.title.includes(q) || (n.path || '').includes(q))
  if (hit) focusNode(hit.id)
  else ElMessage.info(`图中没有匹配「${q}」的节点`)
}

const missing = computed(() => graph.value.missing.slice(0, 60))
const orphans = computed(() => graph.value.nodes.filter((n) => (n.degree || 0) === 0).slice(0, 60))
const kindCounts = computed(() => graph.value.stats.by_kind || {})

watch([hiddenKinds, includePending, includeMeta, includeRaw], () => {
  if (includePending.value || includeMeta.value || includeRaw.value) load()
}, { deep: true })
watch(physics, applyPhysics, { deep: true })

onMounted(() => {
  resize()
  ro = new ResizeObserver(resize)
  if (wrap.value) ro.observe(wrap.value)
  load()
})
onBeforeUnmount(() => {
  if (sim) sim.stop()
  if (ro) ro.disconnect()
  cancelAnimationFrame(rafId)
})
</script>

<template>
  <div class="graph-page" v-loading="loading">
    <div class="head">
      <div>
        <h2><el-icon><Link /></el-icon> 知识图谱</h2>
        <p class="muted">
          关系来自编译产物：<code>related_to</code> + 正文 <code>[[wikilink]]</code> + 链接。
          <strong>索引 / 编译日志 / 知识库规范（保留文件）都在图上</strong>——它们是知识库自我描述的一部分；
          <strong>虚线空心节点</strong>是被引用却还没建立的条目（最精确的知识缺口）。
          滚轮缩放、空白处拖拽平移、拖节点可固定位置。
        </p>
      </div>
      <div class="head-actions">
        <el-input v-model="focusText" size="small" placeholder="定位节点（标题或路径）" clearable
                  :prefix-icon="Search" style="width: 200px" @keyup.enter="doFocus" />
        <el-button :icon="ZoomOut" size="small" @click="zoomBy(1 / 1.25)" />
        <el-button :icon="ZoomIn" size="small" @click="zoomBy(1.25)" />
        <el-button :icon="Rank" size="small" @click="fitView">适应视图</el-button>
        <el-button :icon="Delete" size="small" @click="releasePins">释放固定</el-button>
        <el-button :icon="Refresh" size="small" @click="load">刷新</el-button>
      </div>
    </div>

    <div class="toolbar">
      <el-checkbox v-model="includeMeta" size="small">保留文件（索引/日志/规范）</el-checkbox>
      <el-checkbox v-model="includePending" size="small">待审概念页</el-checkbox>
      <el-checkbox v-model="includeRaw" size="small">RAW 原始语料</el-checkbox>
      <el-select v-model="showLabels" size="small" style="width: 132px">
        <el-option value="auto" label="标注：自动" />
        <el-option value="always" label="标注：总是" />
        <el-option value="never" label="标注：隐藏" />
      </el-select>
      <el-select v-model="hiddenKinds" multiple collapse-tags placeholder="按类型过滤" size="small"
                 style="width: 240px">
        <el-option v-for="k in ALL_KINDS" :key="k" :value="k" :label="KIND_META[k].label" />
      </el-select>
      <span class="muted">节点 {{ stats.drawn }} · 关系 {{ graph.stats.edges || 0 }} ·
        待建 {{ graph.stats.missing || 0 }} · 孤立 {{ graph.stats.orphans || 0 }}</span>
      <el-tag v-if="graph.stats.truncated" size="small" type="danger" effect="plain">超上限已截断</el-tag>
    </div>

    <div class="body">
      <div ref="wrap" class="canvas-wrap">
        <canvas
          ref="canvasRef"
          @wheel="onWheel"
          @pointerdown="onDown"
          @pointermove="onMove"
          @pointerup="onUp"
          @pointerleave="onUp"
        />
        <div class="legend">
          <span v-for="(meta, kind) in KIND_META" :key="kind" class="legend-item">
            <i :style="{ background: meta.color }" />{{ meta.label }}
            <em v-if="kindCounts[kind]">({{ kindCounts[kind] }})</em>
          </span>
        </div>
        <div v-if="hover" class="tip">
          <strong>{{ hover.node.title }}</strong>
          <div class="tip-meta">
            {{ KIND_META[hover.node.kind]?.label || hover.node.kind }}
            <template v-if="hover.node.department"> · {{ hover.node.department }}</template>
            · 连接 {{ hover.node.degree }}
          </div>
          <div v-if="hover.node.path" class="tip-path">{{ hover.node.path }}</div>
          <div v-if="hover.node.missing" class="tip-gap">
            还没建立该条目，被 {{ hover.node.degree }} 处引用
          </div>
          <div v-else-if="hover.neighbors.length" class="tip-neighbors">
            邻接：<a v-for="n in hover.neighbors.slice(0, 8)" :key="n.id"
                    @click.stop="focusNode(n.id)">{{ n.title }}</a>
          </div>
        </div>
        <div class="physics">
          <div class="physics-title">物理参数（Obsidian 同款旋钮）</div>
          <label>中心力 <el-slider v-model="physics.center" :min="0" :max="1" :step="0.05" size="small" /></label>
          <label>斥力 <el-slider v-model="physics.repel" :min="40" :max="800" :step="20" size="small" /></label>
          <label>连线力 <el-slider v-model="physics.link" :min="0" :max="1" :step="0.05" size="small" /></label>
          <label>连线距离 <el-slider v-model="physics.distance" :min="20" :max="300" :step="10" size="small" /></label>
          <el-checkbox v-model="physics.collide" size="small">避免重叠</el-checkbox>
        </div>
      </div>

      <div class="side">
        <div class="panel">
          <div class="panel-title">待建页面（被引用但未建立）</div>
          <el-empty v-if="!missing.length" description="没有被悬空引用的条目" :image-size="48" />
          <div v-for="m in missing" :key="m.id" class="row">
            <span class="row-title">{{ m.title }}</span>
            <span class="muted">被引用 {{ m.count }} 次</span>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">孤立条目（没有任何关系）</div>
          <el-empty v-if="!orphans.length" description="所有条目都有关系" :image-size="48" />
          <div v-for="n in orphans" :key="n.id" class="row clickable" @click="openEntry(n.path)">
            <span class="row-title">{{ n.title }}</span>
            <span class="muted">{{ n.kind }}</span>
          </div>
        </div>
      </div>
    </div>

    <el-drawer v-model="drawer.open" :title="drawer.path" size="50%" direction="rtl">
      <div v-loading="drawer.loading" class="preview">
        <el-alert v-if="!drawer.exists && !drawer.loading" type="info" show-icon :closable="false"
                  title="文件不存在（可尝试重建索引）" />
        <pre v-else>{{ drawer.content }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.head h2 { display: flex; align-items: center; gap: 8px; margin: 0 0 6px; font-size: 17px; color: var(--c-text-strong); }
.head p { margin: 0; max-width: 900px; line-height: 1.7; }
.head code { padding: 1px 5px; border-radius: 4px; background: var(--c-hover-bg); font-size: 11.5px; }
.head-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.muted { color: var(--c-text-muted); font-size: 12px; }

.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 14px 0; }

.body { display: flex; gap: 14px; align-items: stretch; }
.canvas-wrap {
  position: relative; flex: 1; min-width: 0; height: 620px;
  border: 1px solid var(--el-border-color); border-radius: 10px; overflow: hidden;
  background: radial-gradient(circle at 50% 40%, #1b1f2a 0%, #11141c 70%);
}
.canvas-wrap canvas { display: block; cursor: grab; }
.canvas-wrap canvas:active { cursor: grabbing; }

.legend {
  position: absolute; left: 10px; bottom: 10px; display: flex; flex-wrap: wrap; gap: 8px 12px;
  max-width: 62%; padding: 8px 10px; border-radius: 8px;
  background: rgba(12,15,22,0.72); font-size: 11px; color: #cbd5e1;
}
.legend-item { display: inline-flex; align-items: center; gap: 5px; }
.legend-item i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.legend-item em { color: #94a3b8; font-style: normal; }

.tip {
  position: absolute; right: 10px; top: 10px; max-width: 300px;
  padding: 10px 12px; border-radius: 8px; background: rgba(12,15,22,0.88);
  color: #e2e8f0; font-size: 12px; line-height: 1.6; pointer-events: none;
}
.tip strong { font-size: 13px; }
.tip-meta { color: #94a3b8; margin-top: 3px; }
.tip-path { color: #64748b; font-size: 11px; margin-top: 3px; word-break: break-all; }
.tip-gap { color: #fca5a5; margin-top: 4px; }
.tip-neighbors { margin-top: 5px; color: #cbd5e1; pointer-events: auto; }
.tip-neighbors a { color: #93c5fd; margin-right: 6px; cursor: pointer; }
.tip-neighbors a:hover { text-decoration: underline; }

.physics {
  position: absolute; right: 10px; bottom: 10px; width: 218px; padding: 10px 12px;
  border-radius: 8px; background: rgba(12,15,22,0.82); color: #cbd5e1; font-size: 11px;
}
.physics-title { margin-bottom: 6px; color: #e2e8f0; font-weight: 600; }
.physics label { display: block; margin-bottom: 2px; }
.physics :deep(.el-slider) { --el-slider-height: 3px; }
.physics :deep(.el-checkbox__label) { font-size: 11px; }

.side { width: 288px; display: flex; flex-direction: column; gap: 12px; }
.panel { border: 1px solid var(--el-border-color); border-radius: 10px; padding: 12px; max-height: 296px; overflow: auto; }
.panel-title { margin-bottom: 8px; font-size: 12px; font-weight: 600; color: var(--c-text-strong); }
.row { display: flex; justify-content: space-between; gap: 10px; padding: 4px 0; font-size: 12px; }
.row-title { color: var(--c-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.clickable { cursor: pointer; }
.clickable:hover .row-title { color: var(--el-color-primary); }

.preview pre {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12.5px; line-height: 1.75; color: var(--c-text);
}
</style>
