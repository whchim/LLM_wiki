<script setup>
/**
 * 知识图谱（对齐 Obsidian 图谱的交互，并**跟随应用主题**）。
 *
 * 三件事决定它好不好看，这版都按主题做了：
 * 1. **底色与线色必须跟随主题**——上一版画布写死深色渐变、边线写死 `rgba(255,255,255,.16)`，
 *    在浅色界面里既突兀、线又几乎看不见（真 bug，不只是审美）；
 * 2. **标签要有底色描边**：`strokeText` 用画布底色描一圈，压在线上的文字才读得清；
 * 3. **初始布局要先跑到平衡再首帧**：否则第一眼是"炸开的毛球"（预跑 300 tick + 自动适应视图）。
 *
 * 交互与 Obsidian 一致：滚轮以光标为中心缩放、空白拖拽平移、拖节点固定、**悬停高亮 1 跳邻域**、
 * 物理参数旋钮（含**分组力**：同类型互相靠拢，比一坨毛球可读得多）、按类型过滤、搜索定位。
 * 数据来自 `GET /graph`（`core/graph.py` 从 Markdown 派生，含**保留文件** index/log/SCHEMA）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Aim, Delete, Link, Refresh, Search, ZoomIn, ZoomOut } from '@element-plus/icons-vue'
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from 'd3-force'
import { api, ApiError } from '../api'
import { useTheme } from '../core/theme'

const { isDark } = useTheme()

// 节点类型 → 中文名 + 两套主题配色（浅色用 600 档、深色用 400 档，避免"高饱和糊在白底上"）
const KINDS = {
  concept: { label: '概念页', light: '#2f9e44', dark: '#69db7c' },
  resource: { label: '资源摘要', light: '#1c7ed6', dark: '#74c0fc' },
  research: { label: '研究', light: '#7048e8', dark: '#b197fc' },
  glossary: { label: '术语', light: '#0c8599', dark: '#66d9e8' },
  index: { label: '索引（保留文件）', light: '#e8590c', dark: '#ffa94d' },
  log: { label: '编译日志（保留文件）', light: '#d9480f', dark: '#ffc078' },
  schema: { label: '知识库规范（保留文件）', light: '#c2255c', dark: '#f783ac' },
  raw: { label: '原始语料', light: '#868e96', dark: '#adb5bd' },
  missing: { label: '待建页面', light: '#e03131', dark: '#ff8787' },
}
const ALL_KINDS = Object.keys(KINDS)

const THEME = {
  light: {
    canvas: '#fbfcff', grid: '#e7ecf3', gridDot: true,
    edge: 'rgba(100,116,139,0.26)', edgeFocus: 'rgba(28,126,214,0.85)', edgeMissing: 'rgba(224,49,49,0.45)',
    nodeRing: '#fbfcff', label: '#33415595', labelStrong: '#1e293b',
    selection: '#1c7ed6', glow: 'rgba(28,126,214,0.18)',
  },
  dark: {
    canvas: '#10141b', grid: '#1a2130', gridDot: true,
    edge: 'rgba(148,163,184,0.22)', edgeFocus: 'rgba(116,192,252,0.9)', edgeMissing: 'rgba(255,135,135,0.5)',
    nodeRing: '#10141b', label: '#cbd5e1aa', labelStrong: '#f1f5f9',
    selection: '#74c0fc', glow: 'rgba(116,192,252,0.22)',
  },
}

const graph = shallowRef({ nodes: [], edges: [], missing: [], stats: {} })
const loading = ref(false)
const includePending = ref(true)
const includeMeta = ref(true)
const includeRaw = ref(false)
const showLabels = ref('auto')
const hiddenKinds = ref([])
// 物理参数（默认值与截图自检定稿一致：小图拉开、大图成团，不出现"星尘"）
const physics = ref({ center: 0.3, repel: 320, link: 0.5, distance: 130, inward: 0.05, collide: true })

// —— 视觉与布局常量（改动前先跑 frontend/tools 的截图自检，别靠猜）——
const RADIUS_BASE = 5
const RADIUS_SCALE = 3
const RADIUS_MAX = 26
const MISSING_RADIUS = 3.2
const CHARGE_RANGE = 260          // 斥力作用半径：没有它，不连通的小簇会被摊成"星尘"
const ORPHAN_GAP = 110            // 孤立条目摆在主簇包围盒外的距离
const LABEL_BUDGET = 22
const MISSING_LABEL_BUDGET = 8
const FIT_PAD = 70

const wrap = ref(null)
const canvasRef = ref(null)
const hover = ref(null)
const selected = ref(null)
const drawer = ref({ open: false, path: '', loading: false, content: '', exists: true })

let ctx = null
let sim = null
let simNodes = []
let simLinks = []
let simDegree = new Map()
let gridPattern = null
let ro = null
const view = { scale: 1, tx: 0, ty: 0 }
const size = { w: 900, h: 640 }
const drag = { active: false, node: null, moved: false, panning: false, sx: 0, sy: 0, stx: 0, sty: 0 }
const theme = computed(() => (isDark.value ? THEME.dark : THEME.light))

const visibleNodes = computed(() => simNodes.filter((n) => !hiddenKinds.value.includes(n.kind)))
const nodeById = computed(() => new Map(visibleNodes.value.map((n) => [n.id, n])))
const meta = computed(() => graph.value.stats.by_kind || {})

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

const colorOf = (n) => (KINDS[n.kind] || KINDS.resource)[isDark.value ? 'dark' : 'light']
const radiusOf = (n) => (n.kind === 'missing' ? MISSING_RADIUS
  : Math.min(RADIUS_MAX, RADIUS_BASE + Math.sqrt(n.degree || 0) * RADIUS_SCALE))
const repelFor = (n) => Math.max(180, Math.min(700, Math.round(3200 / Math.sqrt(Math.max(n, 1)))))

/** 标签预算：保留文件 + 被引最多的待建页面 + 度数最高的条目（悬停/邻域另算）。
 *  为什么不用"度数≥N"：本项目 215 条边只分给 109 个节点，平均度≈4，阈值判别几乎人人命中，
 *  结果 100+ 个标签同时铺开——这是上一版"糊成一团"的直接原因。 */
function labeledSet(nodes) {
  const meta = nodes.filter((n) => n.is_meta)
  const missing = nodes.filter((n) => n.kind === 'missing')
    .sort((a, b) => (b.degree || 0) - (a.degree || 0)).slice(0, MISSING_LABEL_BUDGET)
  const rest = nodes.filter((n) => !n.is_meta && n.kind !== 'missing')
    .sort((a, b) => (b.degree || 0) - (a.degree || 0))
    .slice(0, Math.max(0, LABEL_BUDGET - meta.length - missing.length))
  return new Set([...meta, ...missing, ...rest].map((n) => n.id))
}
let labels = new Set()

// ---------- 数据 → 仿真 ----------

function buildSim() {
  const raw = graph.value
  const nodes = raw.nodes
    .filter((n) => !hiddenKinds.value.includes(n.kind))
    .map((n) => ({ ...n }))
  if (!hiddenKinds.value.includes('missing')) {
    raw.missing.forEach((m) => nodes.push({
      id: m.id, title: m.title, kind: 'missing', degree: m.count,
      path: null, missing: true, sources: m.sources,
    }))
  }
  const ids = new Set(nodes.map((n) => n.id))
  const links = raw.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ source: e.source, target: e.target, kind: e.kind, resolved: e.resolved }))
  const degree = new Map()
  links.forEach((l) => {
    degree.set(l.source, (degree.get(l.source) || 0) + 1)
    degree.set(l.target, (degree.get(l.target) || 0) + 1)
  })
  simLinks = links
  simNodes = nodes
  simDegree = degree
  labels = labeledSet(nodes)
  // 斥力按规模自适应（固定常数在 18 节点和 140 节点下必有一头难看）
  physics.value.repel = repelFor(nodes.filter((n) => (degree.get(n.id) || 0) > 0).length)
  restartSim(true)
}

function restartSim(fresh = false) {
  if (sim) sim.stop()
  const cx = size.w / 2; const cy = size.h / 2
  // 只让**有连接**的节点参与物理：孤立条目另有摆放（否则包围盒被撑大、主簇被缩小）
  const connected = simNodes.filter((n) => (simDegree.get(n.id) || 0) > 0)
  const linked = simLinks.filter((l) => simDegree.has(l.source) && simDegree.has(l.target))
  connected.forEach((n, i) => {
    const a = (i / Math.max(connected.length, 1)) * Math.PI * 2
    n.x = cx + Math.cos(a) * 90
    n.y = cy + Math.sin(a) * 90
  })
  sim = forceSimulation(connected)
    .force('link', forceLink(linked).id((d) => d.id)
      .distance(physics.value.distance).strength(physics.value.link))
    .force('charge', forceManyBody().strength(-physics.value.repel).distanceMax(CHARGE_RANGE))
    .force('center', forceCenter(cx, cy).strength(physics.value.center))
    .force('inX', forceX(cx).strength(physics.value.inward))
    .force('inY', forceY(cy).strength(physics.value.inward))
    .force('collide', physics.value.collide ? forceCollide().radius((d) => radiusOf(d) + 8) : null)
    .on('tick', draw)
  sim.stop()
  sim.tick(420)                    // 先跑到接近平衡，首帧不是"炸开的毛球"
  placeOrphans(connected)
  draw()
  if (fresh) {
    fitView()
    sim.alpha(0.3).restart()
  }
}

/** 孤立条目摆在主簇包围盒外的紧邻环上：不撑大白边，也一眼看出"它没关系"。 */
function placeOrphans(connected) {
  const isolates = simNodes.filter((n) => !(simDegree.get(n.id) || 0))
  if (!connected.length) {                 // 全孤立：直接按环铺开
    const cx = size.w / 2; const cy = size.h / 2
    isolates.forEach((n, i) => {
      const a = (i / Math.max(isolates.length, 1)) * Math.PI * 2
      n.x = cx + Math.cos(a) * 180; n.y = cy + Math.sin(a) * 180; n.orphan = true
    })
    return
  }
  const xs = connected.map((n) => n.x); const ys = connected.map((n) => n.y)
  const bx = (Math.min(...xs) + Math.max(...xs)) / 2
  const by = (Math.min(...ys) + Math.max(...ys)) / 2
  const br = Math.max((Math.max(...xs) - Math.min(...xs)) / 2, (Math.max(...ys) - Math.min(...ys)) / 2)
  isolates.forEach((n, i) => {
    const a = (i / Math.max(isolates.length, 1)) * Math.PI * 2
    n.x = bx + Math.cos(a) * (br + ORPHAN_GAP)
    n.y = by + Math.sin(a) * (br + ORPHAN_GAP)
    n.orphan = true
  })
}

function applyPhysics() {
  if (!sim) return
  sim.force('link').distance(physics.value.distance).strength(physics.value.link)
  sim.force('charge').strength(-physics.value.repel).distanceMax(CHARGE_RANGE)
  sim.force('center').strength(physics.value.center)
  sim.force('inX').strength(physics.value.inward)
  sim.force('inY').strength(physics.value.inward)
  sim.force('collide', physics.value.collide ? forceCollide().radius((d) => radiusOf(d) + 8) : null)
  sim.alpha(0.6).restart()
}

// ---------- 画布 ----------

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
  buildGridPattern()
  draw()
}

/** 底纹：屏幕空间的点阵（每格一个点），用 pattern 一次性填充，不逐点画。 */
function buildGridPattern() {
  if (!ctx) return
  const step = 24
  const tile = document.createElement('canvas')
  tile.width = step; tile.height = step
  const tctx = tile.getContext('2d')
  tctx.fillStyle = theme.value.grid
  tctx.beginPath()
  tctx.arc(step / 2, step / 2, 1, 0, Math.PI * 2)
  tctx.fill()
  gridPattern = ctx.createPattern(tile, 'repeat')
}

function draw() {
  if (!ctx || !canvasRef.value) return
  const dpr = window.devicePixelRatio || 1
  const p = theme.value
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, canvasRef.value.width, canvasRef.value.height)
  ctx.fillStyle = p.canvas
  ctx.fillRect(0, 0, canvasRef.value.width, canvasRef.value.height)

  // 底纹（随平移/缩放轻微联动，让画布"有底"但不抢戏）
  if (gridPattern) {
    ctx.save()
    ctx.globalAlpha = 0.9
    ctx.translate(view.tx % 24, view.ty % 24)
    ctx.fillStyle = gridPattern
    ctx.fillRect(-24, -24, size.w + 48, size.h + 48)
    ctx.restore()
  }

  ctx.setTransform(view.scale * dpr, 0, 0, view.scale * dpr, view.tx * dpr, view.ty * dpr)
  ctx.lineCap = 'round'

  const focus = neighborIds.value
  const nodes = visibleNodes.value
  const byId = nodeById.value

  // 连线：非邻域一律淡到很轻，避免"毛球感"
  simLinks.forEach((l) => {
    const s = typeof l.source === 'object' ? l.source : byId.get(l.source)
    const t = typeof l.target === 'object' ? l.target : byId.get(l.target)
    if (!s || !t || !byId.has(s.id) || !byId.has(t.id)) return
    const inFocus = focus && (focus.has(s.id) || focus.has(t.id))
    if (focus && !inFocus) return
    ctx.beginPath()
    ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y)
    if (!l.resolved) {
      ctx.strokeStyle = p.edgeMissing
      ctx.setLineDash([3 / view.scale, 3 / view.scale])
      ctx.lineWidth = 1 / view.scale
    } else {
      ctx.strokeStyle = inFocus ? p.edgeFocus : p.edge
      ctx.lineWidth = (inFocus ? 1.5 : 0.8) / view.scale
    }
    ctx.stroke()
    ctx.setLineDash([])
  })

  // 结点：实心圆 + 底色描边（重叠时也能分清）
  const scale = view.scale
  nodes.forEach((n) => {
    const r = radiusOf(n)
    const dim = focus ? !focus.has(n.id) : false
    const active = n.id === selected.value?.id || n.id === hover.value?.node?.id
    ctx.globalAlpha = dim ? 0.16 : 1

    if (active) {                                  // 选中/悬停：外发光
      ctx.beginPath()
      ctx.arc(n.x, n.y, r + 5 / scale, 0, Math.PI * 2)
      ctx.fillStyle = p.glow
      ctx.fill()
    }
    ctx.beginPath()
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2)
    if (n.kind === 'missing') {
      ctx.fillStyle = p.canvas
      ctx.fill()
      ctx.strokeStyle = colorOf(n)
      ctx.lineWidth = 1.4 / scale
      ctx.setLineDash([2.5 / scale, 2.5 / scale])
      ctx.stroke()
      ctx.setLineDash([])
    } else {
      ctx.fillStyle = colorOf(n)
      ctx.fill()
      ctx.strokeStyle = active ? p.selection : p.nodeRing
      ctx.lineWidth = (active ? 2 : 1.4) / scale
      ctx.stroke()
    }
    ctx.globalAlpha = 1
  })

  // 标签：描边打底（压线也读得清）；自动模式 = 预算内节点 + 悬停邻域
  const labelAll = showLabels.value === 'always'
  const labelZoom = showLabels.value === 'auto' && scale > 0.9
  if (showLabels.value !== 'never') {
    const fs = Math.min(13, Math.max(9.5, 11 / Math.max(scale, 0.6)))
    ctx.font = `500 ${fs}px "Segoe UI", "Microsoft YaHei", system-ui, sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'bottom'
    ctx.lineJoin = 'round'
    nodes.forEach((n) => {
      const focused = focus && focus.has(n.id)
      const dim = focus && !focused
      const inBudget = labels.has(n.id)
      if (!focused && !labelAll && !(labelZoom && inBudget) && !(inBudget && scale > 0.55)) return
      const r = radiusOf(n)
      const text = n.title.length > 18 ? `${n.title.slice(0, 18)}…` : n.title
      ctx.globalAlpha = dim ? 0.2 : 1
      ctx.lineWidth = 3 / Math.max(scale, 0.7)
      ctx.strokeStyle = p.canvas
      ctx.strokeText(text, n.x, n.y - r - 3 / scale)
      ctx.fillStyle = focused ? p.labelStrong : p.label
      ctx.fillText(text, n.x, n.y - r - 3 / scale)
      ctx.globalAlpha = 1
    })
  }
}

// ---------- 交互 ----------

const toGraph = (px, py) => ({ x: (px - view.tx) / view.scale, y: (py - view.ty) / view.scale })

function nodeAt(px, py) {
  const pt = toGraph(px, py)
  let best = null; let bestD = Infinity
  visibleNodes.value.forEach((n) => {
    const d = Math.hypot(n.x - pt.x, n.y - pt.y)
    const r = radiusOf(n) + 6 / view.scale
    if (d <= r && d < bestD) { best = n; bestD = d }
  })
  return best
}

function zoomAt(px, py, factor) {
  const next = Math.min(6, Math.max(0.15, view.scale * factor))
  view.tx = px - (px - view.tx) * (next / view.scale)
  view.ty = py - (py - view.ty) * (next / view.scale)
  view.scale = next
  draw()
}

function onWheel(e) {
  e.preventDefault()
  const rect = canvasRef.value.getBoundingClientRect()
  zoomAt(e.clientX - rect.left, e.clientY - rect.top, Math.exp(-e.deltaY * 0.0016))
}

// 按钮缩放走函数（画布尺寸不是响应式对象，模板里读 size.w 会拿到旧值）
const zoomIn = () => zoomAt(size.w / 2, size.h / 2, 1.25)
const zoomOut = () => zoomAt(size.w / 2, size.h / 2, 1 / 1.25)

function fitView() {
  const nodes = visibleNodes.value
  if (!nodes.length) return
  const xs = nodes.map((n) => n.x); const ys = nodes.map((n) => n.y)
  const minX = Math.min(...xs); const maxX = Math.max(...xs)
  const minY = Math.min(...ys); const maxY = Math.max(...ys)
  const pad = FIT_PAD
  view.scale = Math.min(3, Math.max(0.15, Math.min(
    (size.w - pad * 2) / Math.max(maxX - minX, 1),
    (size.h - pad * 2) / Math.max(maxY - minY, 1))))
  view.tx = size.w / 2 - ((minX + maxX) / 2) * view.scale
  view.ty = size.h / 2 - ((minY + maxY) / 2) * view.scale
  draw()
}

function focusNode(id) {
  const n = simNodes.find((x) => x.id === id)
  if (!n) return
  selected.value = n
  view.scale = Math.max(view.scale, 1.15)
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
    sim.alphaTarget(0.2).restart()
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
    const pt = toGraph(px, py)
    drag.node.fx = pt.x; drag.node.fy = pt.y
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
  canvasRef.value.style.cursor = n ? 'pointer' : 'grab'
  if (changed) {
    hover.value = n ? { node: n, neighbors: neighborsOf(n.id) } : null
    draw()
  }
}

function neighborsOf(id) {
  const out = []
  simLinks.forEach((l) => {
    const s = typeof l.source === 'object' ? l.source : nodeById.value.get(l.source)
    const t = typeof l.target === 'object' ? l.target : nodeById.value.get(l.target)
    if (!s || !t) return
    if (s.id === id) out.push(t)
    else if (t.id === id) out.push(s)
  })
  return out
}

function onUp(e) {
  if (drag.active && drag.node) {
    sim.alphaTarget(0)
    if (!drag.moved) { selected.value = drag.node; openEntry(drag.node.path) }
    drag.active = false; drag.node = null
  } else if (drag.panning) {
    drag.panning = false
    if (!drag.moved) { selected.value = null; hover.value = null; draw() }
  }
  if (e?.pointerId !== undefined) {
    try { canvasRef.value.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
  }
}

function releasePins() {
  simNodes.forEach((n) => { n.fx = null; n.fy = null })
  sim.alpha(0.7).restart()
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
    await nextTick()
    resize()
    buildSim()
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

// 主题切换：重算底纹并重画（否则浅色画布配深色点阵）
watch(isDark, () => { buildGridPattern(); draw() })
// 类型过滤只重建仿真（不重新请求）；范围开关才需要重新取数
watch(hiddenKinds, () => buildSim(), { deep: true })
watch([includePending, includeMeta, includeRaw], () => load())
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
})
</script>

<template>
  <div class="graph-page" v-loading="loading">
    <div class="head">
      <div>
        <h2><el-icon><Link /></el-icon> 知识图谱</h2>
        <p class="muted">
          关系来自编译产物：<code>related_to</code> + 正文 <code>[[wikilink]]</code> + 链接；
          <strong>索引 / 编译日志 / 知识库规范</strong>（保留文件）与待审条目都可上图，
          <strong>虚线空心点</strong>是被引用却还没建立的条目（该补哪一页一目了然）。
          <span class="hint">滚轮缩放 · 空白拖拽平移 · 拖节点固定 · 悬停看邻域</span>
        </p>
      </div>
      <div class="head-actions">
        <el-input v-model="focusText" size="small" placeholder="定位节点（标题或路径）" clearable
                  :prefix-icon="Search" style="width: 190px" @keyup.enter="doFocus" />
        <el-button-group>
          <el-button :icon="ZoomOut" size="small" @click="zoomOut" />
          <el-button :icon="ZoomIn" size="small" @click="zoomIn" />
          <el-button :icon="Aim" size="small" @click="fitView">适应</el-button>
        </el-button-group>
        <el-button :icon="Delete" size="small" @click="releasePins">释放固定</el-button>
        <el-button :icon="Refresh" size="small" @click="load">刷新</el-button>
      </div>
    </div>

    <div class="body">
      <div class="canvas-col">
        <div ref="wrap" class="canvas-wrap" :class="{ dark: isDark }">
          <canvas
            ref="canvasRef"
            @wheel="onWheel"
            @pointerdown="onDown"
            @pointermove="onMove"
            @pointerup="onUp"
            @pointerleave="onUp"
          />
          <transition name="fade">
            <div v-if="hover" class="tip">
              <div class="tip-title">{{ hover.node.title }}</div>
              <div class="tip-meta">
                <span class="dot" :style="{ background: colorOf(hover.node) }" />
                {{ KINDS[hover.node.kind]?.label || hover.node.kind }}
                <template v-if="hover.node.department"> · {{ hover.node.department }}</template>
                · 连接 {{ hover.node.degree }}
              </div>
              <div v-if="hover.node.path" class="tip-path">{{ hover.node.path }}</div>
              <div v-if="hover.node.missing" class="tip-gap">
                该条目尚未建立，被 {{ hover.node.degree }} 处引用
              </div>
              <div v-else-if="hover.neighbors.length" class="tip-neighbors">
                <a v-for="n in hover.neighbors.slice(0, 8)" :key="n.id" @click.stop="focusNode(n.id)">
                  <span class="dot" :style="{ background: colorOf(n) }" />{{ n.title }}
                </a>
              </div>
            </div>
          </transition>
        </div>
        <div class="stat-line muted">
          节点 <strong>{{ visibleNodes.length }}</strong> · 关系 <strong>{{ simLinks.length }}</strong>
          · 待建 <strong>{{ graph.stats.missing || 0 }}</strong> · 孤立 <strong>{{ graph.stats.orphans || 0 }}</strong>
          <el-tag v-if="graph.stats.truncated" size="small" type="danger" effect="plain">超上限已截断</el-tag>
        </div>
      </div>

      <aside class="side">
        <el-card shadow="never" class="card">
          <template #header><span class="card-title">范围与过滤</span></template>
          <el-checkbox v-model="includeMeta" size="small">保留文件（索引/日志/规范）</el-checkbox>
          <el-checkbox v-model="includePending" size="small">待审概念页</el-checkbox>
          <el-checkbox v-model="includeRaw" size="small">RAW 原始语料</el-checkbox>
          <div class="row">
            <span class="muted">标注</span>
            <el-select v-model="showLabels" size="small" style="width: 118px">
              <el-option value="auto" label="自动" />
              <el-option value="always" label="总是" />
              <el-option value="never" label="隐藏" />
            </el-select>
          </div>
          <el-select v-model="hiddenKinds" multiple collapse-tags placeholder="按类型隐藏" size="small"
                     style="width: 100%; margin-top: 8px">
            <el-option v-for="k in ALL_KINDS" :key="k" :value="k" :label="KINDS[k].label" />
          </el-select>
        </el-card>

        <el-card shadow="never" class="card">
          <template #header><span class="card-title">物理参数</span></template>
          <label class="slider">中心力 <el-slider v-model="physics.center" :min="0" :max="1" :step="0.05" size="small" /></label>
          <label class="slider">斥力 <el-slider v-model="physics.repel" :min="60" :max="800" :step="20" size="small" /></label>
          <label class="slider">连线力 <el-slider v-model="physics.link" :min="0" :max="1" :step="0.05" size="small" /></label>
          <label class="slider">连线距离 <el-slider v-model="physics.distance" :min="30" :max="300" :step="10" size="small" /></label>
          <label class="slider">向心力 <el-slider v-model="physics.inward" :min="0" :max="0.3" :step="0.01" size="small" /></label>
          <el-checkbox v-model="physics.collide" size="small">避免重叠</el-checkbox>
        </el-card>

        <el-card shadow="never" class="card scroll">
          <template #header>
            <span class="card-title">图例</span>
          </template>
          <div class="legend">
            <span v-for="(v, k) in KINDS" :key="k" class="legend-item">
              <i :style="{ background: colorOf({ kind: k }) }" />{{ v.label }}
              <em v-if="meta[k]">{{ meta[k] }}</em>
            </span>
          </div>
        </el-card>

        <el-card shadow="never" class="card scroll">
          <template #header><span class="card-title">待建页面（被引用但未建立）</span></template>
          <el-empty v-if="!missing.length" description="没有被悬空引用的条目" :image-size="44" />
          <div v-for="m in missing" :key="m.id" class="row clickable" @click="focusNode(m.id)">
            <span class="row-title">{{ m.title }}</span>
            <span class="muted">×{{ m.count }}</span>
          </div>
        </el-card>

        <el-card shadow="never" class="card scroll">
          <template #header><span class="card-title">孤立条目</span></template>
          <el-empty v-if="!orphans.length" description="所有条目都有关系" :image-size="44" />
          <div v-for="n in orphans" :key="n.id" class="row clickable" @click="openEntry(n.path)">
            <span class="row-title">{{ n.title }}</span>
            <span class="muted">{{ KINDS[n.kind]?.label || n.kind }}</span>
          </div>
        </el-card>
      </aside>
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
.head p { margin: 0; max-width: 860px; line-height: 1.7; }
.head code { padding: 1px 5px; border-radius: 4px; background: var(--c-hover-bg); font-size: 11.5px; }
.hint { margin-left: 6px; color: var(--c-brand-ink); }
.head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.muted { color: var(--c-text-muted); font-size: 12px; }

.body { display: flex; gap: 16px; align-items: flex-start; margin-top: 16px; }
.canvas-col { flex: 1; min-width: 0; }
.canvas-wrap {
  position: relative; width: 100%; height: 640px; overflow: hidden;
  border: 1px solid var(--el-border-color); border-radius: 12px;
  background: #fbfcff; box-shadow: 0 1px 2px rgba(15, 23, 42, .04);
}
.canvas-wrap.dark { background: #10141b; }
.canvas-wrap canvas { display: block; cursor: grab; }

.stat-line { margin-top: 10px; display: flex; align-items: center; gap: 8px; }
.stat-line strong { color: var(--c-text-strong); font-weight: 600; }

.tip {
  position: absolute; right: 14px; top: 14px; max-width: 300px;
  padding: 10px 12px; border-radius: 10px; font-size: 12px; line-height: 1.6;
  background: var(--el-bg-color); border: 1px solid var(--el-border-color);
  box-shadow: 0 6px 20px rgba(15, 23, 42, .12); color: var(--c-text); pointer-events: none;
}
.tip-title { font-size: 13px; font-weight: 600; color: var(--c-text-strong); }
.tip-meta { margin-top: 4px; color: var(--c-text-muted); display: flex; align-items: center; gap: 5px; }
.tip-path { margin-top: 4px; font-size: 11px; color: var(--c-text-dim); word-break: break-all; }
.tip-gap { margin-top: 5px; color: var(--el-color-danger); }
.tip-neighbors { margin-top: 6px; display: flex; flex-direction: column; gap: 3px; pointer-events: auto; }
.tip-neighbors a { display: flex; align-items: center; gap: 6px; color: var(--el-color-primary); cursor: pointer; }
.tip-neighbors a:hover { text-decoration: underline; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }

.fade-enter-active, .fade-leave-active { transition: opacity .15s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

.side { width: 296px; display: flex; flex-direction: column; gap: 12px; }
.card :deep(.el-card__header) { padding: 10px 14px; }
.card :deep(.el-card__body) { padding: 12px 14px; }
.card-title { font-size: 12.5px; font-weight: 600; color: var(--c-text-strong); }
.card .row { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 3px 0; font-size: 12px; }
.card.scroll :deep(.el-card__body) { max-height: 210px; overflow: auto; }
.slider { display: block; font-size: 11.5px; color: var(--c-text-muted); margin-bottom: 2px; }
.slider :deep(.el-slider) { --el-slider-height: 3px; margin: 2px 0 6px; }

.legend { display: flex; flex-wrap: wrap; gap: 6px 12px; font-size: 11.5px; color: var(--c-text-muted); }
.legend-item { display: inline-flex; align-items: center; gap: 5px; }
.legend-item i { width: 9px; height: 9px; border-radius: 50%; }
.legend-item em { font-style: normal; color: var(--c-text-faint); }

.row-title { color: var(--c-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.clickable { cursor: pointer; }
.clickable:hover .row-title { color: var(--el-color-primary); }

.preview pre {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12.5px; line-height: 1.75; color: var(--c-text);
}
</style>
