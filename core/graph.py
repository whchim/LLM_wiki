"""知识图谱：从编译产物抽取条目与关系（**从文件派生，不新增权威表**）。

数据来源（三路，全部来自已编译的 Markdown，权威仍是文件本身）：

1. **`related_to` frontmatter** —— 编译契约里的结构化关联（最强信号，模型按契约给出）；
2. **正文 `[[wikilink]]`** —— `## 关联知识` 区块与行内引用（`[[标题]]`、`[[标题|显示]]`、`[[路径]]`）；
3. **正文 `[文字](路径.md)`** —— 显式相对链接（忽略 http(s) 与外链锚点）。

解析约定：
- 节点 id = 相对知识库根的路径（稳定、可直接喂给 `/entries/content` 预览）；
- 边长解析顺序：路径（含 `.md` 相对路径）→ frontmatter `title` → 文件名主干（stem）；
- **解析不到的目标不丢**：作为 `missing` 节点返回（"被引用但未建立"），
  这是比"搜不到"更精确的知识缺口信号（wiki 的红链思路）——补文档时按引用次数排优先级；
- 默认只扫 `NEXUS/`（已发布），`include_pending=True` 时带上 `pending_review/`（审核视角看全貌）；
- 按租户分区：扫描根走 `paths.kb_root()`（L3.5），跨租户互不可见。

不落库的理由：图谱是**可重建的派生视图**（与 `knowledge_entries`、embedding 同一性质），
且规模是"每租户几千条目"级别，实时解析足够快；真到十万级再引入物化表 + 增量更新，
届时同样以文件为权威（`rebuild` 语义）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import paths

NEXUS_DIR = "NEXUS"
PENDING_DIR = "pending_review"
EDGE_RELATED = "related_to"
EDGE_WIKILINK = "wikilink"
EDGE_LINK = "link"

# 自动生成的目录/流水文件不是知识条目：`index.md` 是索引（列出所有条目），
# `log.md` 是编译日志。它们混进图谱会变成度数最高的"超级节点"，
# 而且索引里的 `[[概念-XXX]]` 这种带类型前缀的写法会被误判成"待建页面"（实测）。
NON_ENTRY_FILES = {"index.md", "log.md", "SCHEMA.md"}
# 索引里的 wikilink 可能带类型前缀（`概念-文件资产数字化`）——解析兜底时剥掉再试一次
TYPE_PREFIXES = ("概念-", "资源-", "研究-", "术语-")

# [[标题]] / [[标题|显示文字]] / [[路径]]
_WIKILINK = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]*))?\]\]")
# [文字](目标)：目标只取 .md/.markdown，忽略 http(s)/mailto/锚点
_MDLINK = re.compile(r"(?<!!)\[([^\[\]]*)\]\(([^)\s]+?)\)")
_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _read(path: Path) -> tuple[dict, str]:
    """读一个 Markdown：返回 (frontmatter dict, 正文)。解析失败返回 ({}, 原文)。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        import yaml
        meta = yaml.safe_load(parts[1])
    except Exception:
        return {}, parts[2]
    return (meta if isinstance(meta, dict) else {}), parts[2]


def _iter_files(root: Path, include_pending: bool):
    dirs = [root / NEXUS_DIR] + ([root / PENDING_DIR] if include_pending else [])
    for base in dirs:
        if not base.is_dir():
            continue
        for dirpath, _, files in os.walk(base):
            for fn in sorted(files):
                if fn.endswith(".md") and fn not in NON_ENTRY_FILES:
                    yield Path(dirpath) / fn


def _norm(target: str) -> str:
    """链接目标归一化：去锚点、去首尾空白、反斜杠转正斜杠。"""
    t = (target or "").strip().replace("\\", "/")
    return t.split("#", 1)[0].strip()


def _resolve(target: str, source_path: str, by_path: dict[str, dict],
             by_title: dict[str, str]) -> str | None:
    """把引用目标解析成条目路径。解析顺序（每一层都是实测需要才加的）：

    1. **不区分类型**：目标若已带 `.md`，先按"相对当前文件目录"解析（Markdown 链接语义），
       再按"相对知识库根"解析；
    2. 按 frontmatter `title` / 文件名主干精确匹配（`[[新质生产力]]` 走这层）；
    3. 去掉索引工具的**类型前缀**再试（`[[概念-新质生产力]]` → 新质生产力）；
    4. 目标是路径但前缀不完整时（`概念/新质生产力.md`），用**最后一段**匹配 title/stem。
    """
    if not target:
        return None
    if target.endswith((".md", ".markdown")):
        base = source_path.rsplit("/", 1)[0] if "/" in source_path else ""
        for candidate in (_join_rel(base, target), target.lstrip("./")):
            if candidate in by_path:
                return candidate
    if target in by_title:
        return by_title[target]
    for prefix in TYPE_PREFIXES:
        if target.startswith(prefix):
            stripped = target[len(prefix):]
            if stripped in by_title:
                return by_title[stripped]
    if "/" in target:
        last = target.rstrip("/").rsplit("/", 1)[-1]
        last = re.sub(r"\.(md|markdown)$", "", last, flags=re.IGNORECASE)
        if last in by_title:
            return by_title[last]
    return None


def _join_rel(base: str, target: str) -> str:
    """把相对链接按 `base` 目录归一（处理 `./` 与 `../`），返回知识库根相对路径。"""
    parts = [] if not base else base.split("/")
    for seg in target.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _is_external(target: str) -> bool:
    """外链/站外目标不进图谱（`https://x.md` 这种也算外链——否则会污染"待建页面"）。"""
    t = target.lower()
    return bool(re.match(r"^[a-z][a-z0-9+.\-]*:", t)) or t.startswith("//")


def build_graph(tenant_id: str | None = None, *, include_pending: bool = False,
                max_nodes: int = 1500) -> dict:
    """构建该租户的知识图谱：`{nodes, edges, missing, stats}`。

    `max_nodes` 是安全阀（超大知识库不把响应撑爆）：按"先 NEXUS 后 pending、路径序"截断，
    并在 `stats.truncated=True` 里**如实标注**（不静默丢数据）。
    """
    root = Path(paths.kb_root(tenant_id))
    nodes: list[dict] = []
    by_path: dict[str, dict] = {}
    by_title: dict[str, str] = {}          # title/stem → path（后写覆盖，同标题时以路径序后者为准）
    truncated = False

    for file in _iter_files(root, include_pending):
        rel = file.relative_to(root).as_posix()
        if len(nodes) >= max_nodes:
            truncated = True
            break
        meta, body = _read(file)
        title = str(meta.get("title") or file.stem)
        node = {
            "id": rel, "path": rel, "title": title,
            "type": meta.get("type") or "resource",
            "status": meta.get("status") or "active",
            "department": meta.get("department"),
            "description": meta.get("description"),
            "degree": 0,
        }
        nodes.append(node)
        by_path[rel] = node
        for key in {title, file.stem}:
            by_title.setdefault(key, rel)

        related = meta.get("related_to")
        targets: list[tuple[str, str]] = []
        if isinstance(related, list):
            targets += [(str(r), EDGE_RELATED) for r in related if str(r).strip()]
        elif isinstance(related, str) and related.strip():
            targets.append((related, EDGE_RELATED))
        targets += [(m.group(1), EDGE_WIKILINK) for m in _WIKILINK.finditer(body)]
        targets += [(m.group(2), EDGE_LINK) for m in _MDLINK.finditer(body)]
        node["_targets"] = targets

    edges: list[dict] = []
    missing: dict[str, dict] = {}
    seen: set[tuple[str, str, str]] = set()
    for node in nodes:
        for raw_target, kind in node.pop("_targets", []):
            target = _norm(raw_target)
            if not target or _is_external(target):
                continue
            resolved = _resolve(target, node["id"], by_path, by_title)
            source = node["id"]
            if resolved == source:
                continue                                   # 自链接无信息
            if resolved:
                key = (source, resolved, kind)
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"source": source, "target": resolved, "kind": kind,
                              "resolved": True})
                by_path[source]["degree"] += 1
                by_path[resolved]["degree"] += 1
            else:
                # 被引用但不存在 → 待建页面（知识缺口信号）
                item = missing.setdefault(target, {"id": f"missing:{target}", "title": target,
                                                   "count": 0, "sources": []})
                item["count"] += 1
                if len(item["sources"]) < 10 and source not in item["sources"]:
                    item["sources"].append(source)
                key = (source, item["id"], kind)
                if key not in seen:
                    seen.add(key)
                    edges.append({"source": source, "target": item["id"], "kind": kind,
                                  "resolved": False})
                    by_path[source]["degree"] += 1

    missing_nodes = sorted(missing.values(), key=lambda m: (-m["count"], m["title"]))
    orphans = [n["id"] for n in nodes if n["degree"] == 0]
    return {
        "nodes": nodes,
        "edges": edges,
        "missing": missing_nodes,
        "stats": {
            "nodes": len(nodes), "edges": len(edges),
            "missing": len(missing_nodes), "orphans": len(orphans),
            "pending_included": include_pending, "truncated": truncated,
        },
    }


def neighbors(path: str, tenant_id: str | None = None, *, include_pending: bool = True) -> dict:
    """某条目的 1 跳邻域（中心 + 出边/入边邻居）。路径不在图中时返回 `{found: False}`。"""
    graph = build_graph(tenant_id, include_pending=include_pending)
    ids = {n["id"]: n for n in graph["nodes"]}
    if path not in ids:
        return {"found": False, "path": path, "neighbors": []}
    out = [e for e in graph["edges"] if e["source"] == path]
    inn = [e for e in graph["edges"] if e["target"] == path]
    related_ids = {e["target"] for e in out} | {e["source"] for e in inn}
    related_ids.discard(path)
    missing_ids = {e["target"] for e in out if not e["resolved"]}
    return {
        "found": True, "path": path, "node": ids[path],
        "outgoing": out, "incoming": inn,
        "neighbors": [ids[i] for i in sorted(related_ids) if i in ids],
        "missing": [m for m in graph["missing"] if m["id"] in missing_ids],
    }
