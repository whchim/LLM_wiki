<script setup>
/**
 * 知识图谱：把编译产物里的关系画出来，并把"被引用但没建"的条目列成待建清单。
 *
 * 数据来自 GET /graph（`core/graph.py` 从 Markdown 派生）：
 * - 节点 = 条目（NEXUS/ 已发布；可切换带上 pending_review/）
 * - 边 = frontmatter `related_to` / 正文 `[[wikilink]]` / markdown 链接
 * - 待建页面 = 被引用但解析不到目标的引用（红链）——比"搜不到"更精确的知识缺口信号
 *
 * 布局是**自己算的力导向**（斥力 + 弹簧 + 向心，固定迭代次数，无第三方依赖）：
 * 规模在几百节点内一次算完即可，不需要引 d3/echarts 这类大包（前端包体已 1MB+）。
 */
import { computed, onMounted, ref, shallowRef } from 'vue'
import { ElMessage } from 'element-plus'
import { Document, Link, Refresh } from '@element-plus/icons-vue'
import { api, ApiError } from '../api'

const loading = ref(false)
const includePending = ref(false)
const graph = shallowRef({ nodes: [], edges: [], missing: [], stats: {} })
const highlight = ref(null)           // 当前选中/悬停节点 id
const focusText = ref('')
const drawer = ref({ open: false, path: '', loading: false, content: '', exists: true })

const WIDTH = 900
const HEIGHT = 560

const DEPT_COLORS = {
  销售: '#409eff', 售前: '#36cfc9', 产品: '#9254de', 实施交付: '#f759ab',
  开发: '#597ef7', 财务: '#faad14', 人事: '#13c2c2', 行政: '#8c8c8c',
  共享层: '#52c41a',
}

function hashSeed(text) {
  let h = 2166136261
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return ((h >>> 0) % 10000) / 10000
}

/** 力导向布局：确定性初值（按路径哈希）保证同一份图每次形状一致，便于对照。 */
function layout(nodes, edges) {
  const pts = new Map()
  nodes.forEach((n, i) => {
    const angle = (i / Math.max(nodes.length, 1)) * Math.PI * 2
    const radius = 90 + hashSeed(n.id) * 170
    pts.set(n.id, {
      x: WIDTH / 2 + Math.cos(angle) * radius,
      y: HEIGHT / 2 + Math.sin(angle) * radius * 0.72,
      vx: 0, vy: 0,
    })
  })
  const links = edges.filter((e) => pts.has(e.source) && pts.has(e.target))
  for (let iter = 0; iter < 220; iter += 1) {
    const cooling = 1 - iter / 260
    // 斥力（库仑）
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = pts.get(nodes[i].id); const b = pts.get(nodes[j].id)
        let dx = a.x - b.x; let dy = a.y - b.y
        let d2 = dx * dx + dy * dy
        if (d2 < 1) { dx = (hashSeed(nodes[i].id) - 0.5) || 0.3; dy = (hashSeed(nodes[j].id) - 0.5) || 0.3; d2 = 1 }
        const force = 5200 / d2
        const d = Math.sqrt(d2)
        a.vx += (dx / d) * force; a.vy += (dy / d) * force
        b.vx -= (dx / d) * force; b.vy -= (dy / d) * force
      }
    }
    // 弹簧（相邻拉近）
    links.forEach((e) => {
      const a = pts.get(e.source); const b = pts.get(e.target)
      const dx = b.x - a.x; const dy = b.y - a.y
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1)
      const force = (d - 110) * 0.012
      a.vx += (dx / d) * force; a.vy += (dy / d) * force
      b.vx -= (dx / d) * force; b.vy -= (dy / d) * force
    })
    // 向心 + 位移
    pts.forEach((p) => {
      p.vx += (WIDTH / 2 - p.x) * 0.0022
      p.vy += (HEIGHT / 2 - p.y) * 0.0022
      p.x += Math.max(-14, Math.min(14, p.vx * cooling))
      p.y += Math.max(-14, Math.min(14, p.vy * cooling))
      p.vx *= 0.72; p.vy *= 0.72
      p.x = Math.max(24, Math.min(WIDTH - 24, p.x))
      p.y = Math.max(24, Math.min(HEIGHT - 24, p.y))
    })
  }
  return pts
}

const positions = computed(() => layout(graph.value.nodes, graph.value.edges))

const neighborIds = computed(() => {
  const id = highlight.value
  if (!id) return null
  const set = new Set([id])
  graph.value.edges.forEach((e) => {
    if (e.source === id) set.add(e.target)
    if (e.target === id) set.add(e.source)
  })
  return set
})

const radius = (node) => 6 + Math.min(10, Math.sqrt(node.degree || 0) * 4)

function edgeLine(edge) {
  const a = positions.value.get(edge.source)
  const b = positions.value.get(edge.target)
  return a && b ? { x1: a.x, y1: a.y, x2: b.x, y2: b.y } : null
}

const missingPositions = computed(() => {
  // 待建页面：挂在引用它的条目外侧，用虚线表示"还不存在"
  const out = new Map()
  graph.value.missing.forEach((m, i) => {
    const angle = (i / Math.max(graph.value.missing.length, 1)) * Math.PI * 2
    out.set(m.id, { x: WIDTH / 2 + Math.cos(angle) * 320, y: HEIGHT / 2 + Math.sin(angle) * 210 })
  })
  return out
})

function dimmed(id) {
  const set = neighborIds.value
  return set ? !set.has(id) : false
}

async function load() {
  loading.value = true
  try {
    graph.value = await api.graph(includePending.value)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '图谱加载失败')
  } finally {
    loading.value = false
  }
}

async function openEntry(path) {
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

function focusNode() {
  const q = focusText.value.trim()
  if (!q) { highlight.value = null; return }
  const hit = graph.value.nodes.find((n) => n.title.includes(q) || n.id.includes(q))
  if (hit) highlight.value = hit.id
  else ElMessage.info(`图中没有匹配「${q}」的条目`)
}

const missing = computed(() => graph.value.missing.slice(0, 40))
const orphans = computed(() => graph.value.nodes.filter((n) => (n.degree || 0) === 0).slice(0, 40))

onMounted(load)
</script>

<template>
  <div class="graph-page" v-loading="loading">
    <div class="head">
      <div>
        <h2><el-icon><Link /></el-icon> 知识图谱</h2>
        <p class="muted">
          关系来自编译产物的 <code>related_to</code> 与正文 <code>[[wikilink]]</code>；
          <strong>虚线节点</strong>是被引用但还没建立的条目——这就是最精确的知识缺口（该补哪一页）。
        </p>
      </div>
      <div class="head-actions">
        <el-input
          v-model="focusText"
          size="small"
          placeholder="定位条目（标题或路径）"
          clearable
          style="width: 210px"
          @keyup.enter="focusNode"
        />
        <el-checkbox v-model="includePending" size="small" @change="load">含待审条目</el-checkbox>
        <el-button :icon="Refresh" size="small" @click="load">刷新</el-button>
      </div>
    </div>

    <div class="stats">
      <el-tag size="small" effect="plain">条目 {{ graph.stats.nodes || 0 }}</el-tag>
      <el-tag size="small" effect="plain" type="success">关系 {{ graph.stats.edges || 0 }}</el-tag>
      <el-tag size="small" effect="plain" type="warning">待建 {{ graph.stats.missing || 0 }}</el-tag>
      <el-tag size="small" effect="plain" type="info">孤立 {{ graph.stats.orphans || 0 }}</el-tag>
      <el-tag v-if="graph.stats.truncated" size="small" type="danger" effect="plain">
        节点数超上限已截断
      </el-tag>
    </div>

    <div class="body">
      <svg :viewBox="`0 0 ${WIDTH} ${HEIGHT}`" class="canvas" @click="highlight = null">
        <g>
          <line
            v-for="(e, i) in graph.edges"
            :key="`e${i}`"
            v-bind="edgeLine(e) || {}"
            :class="['edge', { dashed: !e.resolved, faded: highlight && dimmed(e.source) && dimmed(e.target) }]"
          />
        </g>
        <g>
          <line
            v-for="(m, i) in graph.missing"
            :key="`m${i}`"
            :x1="positions.get(m.sources[0])?.x" :y1="positions.get(m.sources[0])?.y"
            :x2="missingPositions.get(m.id).x" :y2="missingPositions.get(m.id).y"
            class="edge dashed"
          />
        </g>
        <g>
          <g
            v-for="n in graph.nodes"
            :key="n.id"
            :class="['node', { faded: dimmed(n.id) }]"
            @click.stop="highlight = n.id; openEntry(n.path)"
            @mouseenter="highlight = n.id"
          >
            <circle
              :cx="positions.get(n.id).x" :cy="positions.get(n.id).y" :r="radius(n)"
              :fill="DEPT_COLORS[n.department] || '#8c8c8c'"
              :class="{ active: highlight === n.id }"
            />
            <text
              :x="positions.get(n.id).x" :y="positions.get(n.id).y - radius(n) - 5"
              text-anchor="middle" class="label"
            >{{ n.title.length > 12 ? n.title.slice(0, 12) + '…' : n.title }}</text>
          </g>
        </g>
        <g>
          <g v-for="m in graph.missing" :key="m.id" class="missing-node">
            <circle
              :cx="missingPositions.get(m.id).x" :cy="missingPositions.get(m.id).y"
              r="4.5" class="missing-dot"
            />
            <text
              :x="missingPositions.get(m.id).x" :y="missingPositions.get(m.id).y - 9"
              text-anchor="middle" class="label missing-label"
            >{{ m.title }} ×{{ m.count }}</text>
          </g>
        </g>
      </svg>

      <div class="side">
        <div class="panel">
          <div class="panel-title">待建页面（被引用但未建立）</div>
          <el-empty v-if="!missing.length" description="没有被悬空引用的条目" :image-size="52" />
          <div v-for="m in missing" :key="m.id" class="row">
            <span class="row-title">{{ m.title }}</span>
            <span class="muted">被引用 {{ m.count }} 次</span>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">孤立条目（没有任何关系）</div>
          <el-empty v-if="!orphans.length" description="所有条目都有关系" :image-size="52" />
          <div v-for="n in orphans" :key="n.id" class="row clickable" @click="openEntry(n.path)">
            <span class="row-title">{{ n.title }}</span>
            <span class="muted">{{ n.type }}</span>
          </div>
        </div>
      </div>
    </div>

    <el-drawer v-model="drawer.open" :title="drawer.path" size="50%" direction="rtl">
      <div v-loading="drawer.loading" class="preview">
        <el-alert
          v-if="!drawer.exists && !drawer.loading"
          type="info" show-icon :closable="false" title="文件不存在（可尝试重建索引）"
        />
        <pre v-else>{{ drawer.content }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.head h2 { display: flex; align-items: center; gap: 8px; margin: 0 0 6px; font-size: 17px; color: var(--c-text-strong); }
.head p { margin: 0; max-width: 760px; line-height: 1.7; }
.head code { padding: 1px 5px; border-radius: 4px; background: var(--c-hover-bg); font-size: 11.5px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.muted { color: var(--c-text-muted); font-size: 12px; }

.stats { display: flex; gap: 8px; margin: 14px 0; }

.body { display: flex; gap: 16px; align-items: flex-start; }
.canvas {
  flex: 1; min-width: 0; height: 560px;
  border: 1px solid var(--el-border-color); border-radius: 10px;
  background: var(--c-sidebar);
}
.edge { stroke: var(--el-border-color); stroke-width: 1; }
.edge.dashed { stroke-dasharray: 4 4; stroke: var(--el-color-warning); opacity: .55; }
.node { cursor: pointer; }
.node circle { stroke: var(--c-sidebar); stroke-width: 1.5; }
.node circle.active { stroke: var(--c-text-strong); stroke-width: 2.5; }
.faded { opacity: .18; }
.label { font-size: 10.5px; fill: var(--c-text-muted); pointer-events: none; }
.missing-dot { fill: none; stroke: var(--el-color-warning); stroke-width: 1.5; stroke-dasharray: 3 2; }
.missing-label { fill: var(--el-color-warning); }

.side { width: 300px; display: flex; flex-direction: column; gap: 12px; }
.panel { border: 1px solid var(--el-border-color); border-radius: 10px; padding: 12px; max-height: 266px; overflow: auto; }
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
