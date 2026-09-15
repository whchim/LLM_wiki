/**
 * 图谱视觉自检工具（改图谱布局/配色前先跑它，别靠猜）。
 *
 * 用途：用**与 `src/pages/GraphPage.vue` 相同的参数**把真实 `/graph` 数据渲染成独立 HTML
 * （浅色 + 深色两套），再用无头 Edge 截图，于是"丑不丑/挤不挤/标签糊不糊"变成看得见的东西。
 *
 * 用法（需要 API 在跑，且 KB 里有真实数据）：
 *     cd frontend && node tools/graph_snapshot.mjs        # 生成 tools/_graph_*.html + 打印布局指标
 *     "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu \
 *       --window-size=1180,720 --screenshot=tools/_shot_full_light.png \
 *       "file:///<abs>/frontend/tools/_graph_full_light.html"
 *
 * 打印的指标比眼睛更早发现问题：
 * - `连通分量`：分量多说明图天然碎片化（本项目 pending 概念与已发布图基本不连通），
 *   此时**全局斥力会把各簇摊成"星尘"**——所以斥力必须带 `distanceMax`（只做局部排斥）；
 * - `重叠`：必须为 0（否则节点糊在一起，标签也没法读）；
 * - `跨度`：包围盒尺寸，与画布比例差太多说明 `fit` 会留大片空白。
 */
import fs from 'node:fs'
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from 'd3-force'

const BASE = 'http://127.0.0.1:8000'
const W = 1180
const H = 720

// 与 GraphPage.vue 保持一致的参数
const radiusOf = (n) => (n.kind === 'missing' ? 3.2 : Math.min(26, 5 + Math.sqrt(n.degree || 0) * 3))
const repelFor = (n) => Math.max(180, Math.min(700, Math.round(3200 / Math.sqrt(n))))
const CHARGE_RANGE = 260
const LINK_DISTANCE = 130
const LINK_STRENGTH = 0.5
const CENTER_STRENGTH = 0.3
const INWARD_STRENGTH = 0.05
const LABEL_BUDGET = 22
const MISSING_LABEL_BUDGET = 8

const KINDS = {
  concept: { light: '#2f9e44', dark: '#69db7c' },
  resource: { light: '#1c7ed6', dark: '#74c0fc' },
  research: { light: '#7048e8', dark: '#b197fc' },
  glossary: { light: '#0c8599', dark: '#66d9e8' },
  index: { light: '#e8590c', dark: '#ffa94d' },
  log: { light: '#d9480f', dark: '#ffc078' },
  schema: { light: '#c2255c', dark: '#f783ac' },
  raw: { light: '#868e96', dark: '#adb5bd' },
  missing: { light: '#e03131', dark: '#ff8787' },
}
const THEME = {
  light: { canvas: '#fbfcff', grid: '#e7ecf3', edge: 'rgba(100,116,139,0.26)',
           edgeMissing: 'rgba(224,49,49,0.45)', ring: '#fbfcff', label: '#33415595' },
  dark: { canvas: '#10141b', grid: '#1a2130', edge: 'rgba(148,163,184,0.22)',
          edgeMissing: 'rgba(255,135,135,0.5)', ring: '#10141b', label: '#cbd5e1aa' },
}

async function login() {
  const r = await fetch(`${BASE}/auth/login`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password: 'admin123' }),
  })
  return (await r.json()).access_token
}

async function getGraph(token, qs) {
  const r = await fetch(`${BASE}/graph${qs}`, { headers: { Authorization: `Bearer ${token}` } })
  return r.json()
}

function labeledSet(nodes) {
  const meta = nodes.filter((n) => n.is_meta)
  const missing = nodes.filter((n) => n.kind === 'missing')
    .sort((a, b) => (b.degree || 0) - (a.degree || 0)).slice(0, MISSING_LABEL_BUDGET)
  const rest = nodes.filter((n) => !n.is_meta && n.kind !== 'missing')
    .sort((a, b) => (b.degree || 0) - (a.degree || 0))
    .slice(0, Math.max(0, LABEL_BUDGET - meta.length - missing.length))
  return new Set([...meta, ...missing, ...rest].map((n) => n.id))
}

function countComponents(nodes, links) {
  const adj = new Map(nodes.map((n) => [n.id, []]))
  links.forEach((l) => {
    if (!l.a || !l.b) return
    adj.get(l.source)?.push(l.target)
    adj.get(l.target)?.push(l.source)
  })
  const seen = new Set(); let comps = 0
  nodes.forEach((n) => {
    if (seen.has(n.id)) return
    comps += 1
    const stack = [n.id]
    while (stack.length) {
      const cur = stack.pop()
      if (seen.has(cur)) continue
      seen.add(cur)
      ;(adj.get(cur) || []).forEach((nx) => { if (!seen.has(nx)) stack.push(nx) })
    }
  })
  return comps
}

function layout(graph) {
  const all = graph.nodes.map((n) => ({ ...n }))
  graph.missing.forEach((m) => all.push({ id: m.id, title: m.title, kind: 'missing',
                                          degree: m.count, missing: true }))
  const ids = new Set(all.map((n) => n.id))
  const rawLinks = graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target))
  const deg = new Map()
  rawLinks.forEach((e) => {
    deg.set(e.source, (deg.get(e.source) || 0) + 1)
    deg.set(e.target, (deg.get(e.target) || 0) + 1)
  })
  const cx = W / 2; const cy = H / 2
  const connected = all.filter((n) => (deg.get(n.id) || 0) > 0)
  const isolates = all.filter((n) => (deg.get(n.id) || 0) === 0)
  const byId = new Map(connected.map((n) => [n.id, n]))
  const links = rawLinks
    .filter((e) => byId.has(e.source) && byId.has(e.target))
    .map((e) => ({ source: byId.get(e.source), target: byId.get(e.target), resolved: e.resolved }))
  const repel = repelFor(connected.length)
  connected.forEach((n, i) => {
    const a = (i / Math.max(connected.length, 1)) * Math.PI * 2
    n.x = cx + Math.cos(a) * 90; n.y = cy + Math.sin(a) * 90
  })
  const sim = forceSimulation(connected)
    .force('link', forceLink(links).id((d) => d.id).distance(LINK_DISTANCE).strength(LINK_STRENGTH))
    .force('charge', forceManyBody().strength(-repel).distanceMax(CHARGE_RANGE))
    .force('center', forceCenter(cx, cy).strength(CENTER_STRENGTH))
    .force('inX', forceX(cx).strength(INWARD_STRENGTH))
    .force('inY', forceY(cy).strength(INWARD_STRENGTH))
    .force('collide', forceCollide().radius((d) => radiusOf(d) + 8))
    .stop()
  sim.tick(420)
  if (connected.length) {
    const xs = connected.map((n) => n.x); const ys = connected.map((n) => n.y)
    const bx = (Math.min(...xs) + Math.max(...xs)) / 2
    const by = (Math.min(...ys) + Math.max(...ys)) / 2
    const br = Math.max((Math.max(...xs) - Math.min(...xs)) / 2, (Math.max(...ys) - Math.min(...ys)) / 2)
    isolates.forEach((n, i) => {
      const a = (i / Math.max(isolates.length, 1)) * Math.PI * 2
      const r = br + 95
      n.x = bx + Math.cos(a) * r; n.y = by + Math.sin(a) * r; n.orphan = true
    })
  }
  return { nodes: [...connected, ...isolates], repel,
           links: rawLinks.map((e) => ({ ...e, a: byId.get(e.source), b: byId.get(e.target) })) }
}

function fit(nodes, pad = 70) {
  const xs = nodes.map((n) => n.x); const ys = nodes.map((n) => n.y)
  const minX = Math.min(...xs); const maxX = Math.max(...xs)
  const minY = Math.min(...ys); const maxY = Math.max(...ys)
  const scale = Math.min(3, Math.max(0.15, Math.min((W - pad * 2) / Math.max(maxX - minX, 1),
                                                    (H - pad * 2) / Math.max(maxY - minY, 1))))
  return { scale, tx: W / 2 - ((minX + maxX) / 2) * scale, ty: H / 2 - ((minY + maxY) / 2) * scale }
}

function metrics(data) {
  const { nodes, links } = data
  let overlaps = 0
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const d = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y)
      if (d < radiusOf(nodes[i]) + radiusOf(nodes[j])) overlaps += 1
    }
  }
  const lens = links.filter((l) => l.a && l.b).map((l) => Math.hypot(l.a.x - l.b.x, l.a.y - l.b.y))
  const avg = lens.reduce((a, b) => a + b, 0) / Math.max(lens.length, 1)
  const xs = nodes.map((n) => n.x); const ys = nodes.map((n) => n.y)
  return { nodes: nodes.length, edges: links.length, overlaps, repel: data.repel,
           avgEdgeLen: Math.round(avg), labels: labeledSet(nodes).size,
           span: `${Math.round(Math.max(...xs) - Math.min(...xs))}x${Math.round(Math.max(...ys) - Math.min(...ys))}`,
           components: countComponents(nodes, links) }
}

function html(data, mode) {
  const p = THEME[mode]
  const { nodes, links } = data
  const labeled = labeledSet(nodes)
  const view = fit(nodes)
  const out = []
  out.push(`<rect width="${W}" height="${H}" fill="${p.canvas}"/>`)
  for (let x = 12; x < W; x += 24) for (let y = 12; y < H; y += 24) {
    out.push(`<circle cx="${x}" cy="${y}" r="1" fill="${p.grid}"/>`)
  }
  links.forEach((l) => {
    if (!l.a || !l.b) return
    const dash = l.resolved === false ? ' stroke-dasharray="3 3"' : ''
    const color = l.resolved === false ? p.edgeMissing : p.edge
    out.push(`<line x1="${l.a.x.toFixed(1)}" y1="${l.a.y.toFixed(1)}" x2="${l.b.x.toFixed(1)}" `
      + `y2="${l.b.y.toFixed(1)}" stroke="${color}" stroke-width="${l.resolved === false ? 1 : 0.8}"${dash}/>`)
  })
  nodes.forEach((n) => {
    const r = radiusOf(n)
    const fill = (KINDS[n.kind] || KINDS.resource)[mode]
    if (n.kind === 'missing') {
      out.push(`<circle cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${r}" fill="${p.canvas}" `
        + `stroke="${fill}" stroke-width="1.4" stroke-dasharray="2.5 2.5"/>`)
    } else {
      out.push(`<circle cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${r}" fill="${fill}" `
        + `stroke="${p.ring}" stroke-width="1.4"/>`)
    }
  })
  nodes.filter((n) => labeled.has(n.id)).forEach((n) => {
    const r = radiusOf(n)
    const t = n.title.length > 18 ? `${n.title.slice(0, 18)}…` : n.title
    out.push(`<text x="${n.x.toFixed(1)}" y="${(n.y - r - 3).toFixed(1)}" text-anchor="middle" `
      + `font-size="11" font-weight="500" fill="${p.label}" stroke="${p.canvas}" stroke-width="3" `
      + `paint-order="stroke" font-family="Segoe UI, Microsoft YaHei, sans-serif">`
      + `${t.replace(/&/g, '&amp;').replace(/</g, '&lt;')}</text>`)
  })
  return `<!doctype html><meta charset="utf-8"><body style="margin:0">`
    + `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`
    + `<g transform="translate(${view.tx.toFixed(1)},${view.ty.toFixed(1)}) scale(${view.scale.toFixed(3)})">`
    + out.join('') + '</g></svg></body>'
}

const token = await login()
for (const [name, qs] of [['published', '?include_pending=false&include_meta=true'],
                          ['full', ''], ['with_raw', '?include_raw=true']]) {
  const graph = await getGraph(token, qs)
  const data = layout(graph)
  const m = metrics(data)
  console.log(`[${name}] 节点=${m.nodes} 边=${m.edges} 连通分量=${m.components} 重叠=${m.overlaps} `
    + `平均边长=${m.avgEdgeLen} 跨度=${m.span} 斥力=${m.repel} 标签=${m.labels} 待建=${graph.stats.missing}`)
  for (const mode of ['light', 'dark']) {
    fs.writeFileSync(`tools/_graph_${name}_${mode}.html`, html(data, mode))
  }
}
