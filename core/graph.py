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
RAW_DIR = "RAW"
EDGE_RELATED = "related_to"
EDGE_WIKILINK = "wikilink"
EDGE_LINK = "link"

# **保留文件**（PRD WIKI-00 §Reserved Files）：index.md 是渐进式目录、log.md 是操作审计日志、
# SCHEMA.md 是知识库规范。它们**是知识库自我描述的一部分，必须出现在图谱里**——
# 早先版本把它们当"非条目"排除掉了，结果是"索引与日志在图上不存在"，与编译范式的理念不符。
# 正确的处理是**保留并分类**（`kind=index/log/schema`），由前端着色与过滤，
# 而不是藏起来；索引因列出全部条目而度数高，这是**事实**，不该用排除来抹平。
RESERVED_FILES = {"index.md": "index", "log.md": "log", "SCHEMA.md": "schema"}
# 保留文件的中文显示名（项目约定：知识文件与展示一律中文）
RESERVED_TITLES = {"index": "知识库索引", "log": "编译日志", "schema": "知识库规范"}
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


def _iter_files(root: Path, include_pending: bool, include_raw: bool, include_meta: bool):
    """要纳入图谱的 Markdown。默认：已发布条目 + 保留文件（index/log/SCHEMA）。

    - `include_pending`：带上 `pending_review/`（待审概念页）；
    - `include_raw`：带上 `RAW/`（**原始语料**，不是编译产物，前端用另一种颜色区分）；
    - `include_meta=False`：只画知识条目（不画保留文件）。

    注意 **根层只取根目录本身、不递归**：递归会让 NEXUS/pending/RAW 里的文件先以"根层"身份
    被收进来，等到专门的分支时又被 `seen` 挡掉，于是 RAW 文件被当成知识条目（实测踩过）。
    """
    seen: set[str] = set()

    def _yield(base: Path, scope: str):
        if not base.is_dir():
            return
        for dirpath, dirnames, files in os.walk(base):
            # 触发文件/建议目录不属知识内容；根层不递归（见 docstring）
            dirnames[:] = [d for d in dirnames if d not in ("_triggers", "_suggestions", "tenants")]
            if scope == "root":
                dirnames[:] = []
            for fn in sorted(files):
                if not fn.endswith(".md"):
                    continue
                p = Path(dirpath) / fn
                rel = p.relative_to(root).as_posix()
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                # include_meta=False：连 NEXUS 里的 index/log 一起不画（"只看知识条目"的语义）
                if (not include_meta and scope != "raw" and parent in ("NEXUS", "")
                        and fn in RESERVED_FILES):
                    continue
                if rel in seen:
                    continue
                seen.add(rel)
                yield p, scope

    if include_meta:
        yield from _yield(root, "root")                  # 根层保留文件（SCHEMA.md 等）
    yield from _yield(root / NEXUS_DIR, "nexus")
    if include_pending:
        yield from _yield(root / PENDING_DIR, "pending")
    if include_raw:
        yield from _yield(root / RAW_DIR, "raw")


def _classify(rel: str, scope: str, meta: dict) -> tuple[str, bool]:
    """返回 (kind, is_meta)。保留文件按文件名归类；RAW 一律 kind=raw（原始语料）。"""
    name = rel.rsplit("/", 1)[-1]
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if scope != "raw" and parent in ("NEXUS", "") and name in RESERVED_FILES:
        return RESERVED_FILES[name], True
    if scope == "raw":
        return "raw", False
    return str(meta.get("type") or "resource"), False


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
                include_meta: bool = True, include_raw: bool = False,
                max_nodes: int = 1500) -> dict:
    """构建该租户的知识图谱：`{nodes, edges, missing, stats}`。

    范围（对齐 Obsidian 的图谱口径——**整个知识库都该被看见**，包括知识库自己的保留文件）：
    默认 = `NEXUS/**`（已发布）+ 保留文件（`index.md`/`log.md`/`SCHEMA.md`）；
    `include_pending` 带待审概念页，`include_raw` 带 `RAW/` 原始语料（`kind=raw`，另行着色）。

    `max_nodes` 是安全阀（超大知识库不把响应撑爆）：按"先 NEXUS 后保留文件、再 pending、再 RAW、
    路径序"截断，并在 `stats.truncated=True` 里**如实标注**（不静默丢数据）。
    """
    root = Path(paths.kb_root(tenant_id))
    nodes: list[dict] = []
    by_path: dict[str, dict] = {}
    by_title: dict[str, str] = {}          # title/stem → path（后写覆盖，同标题时以路径序后者为准）
    truncated = False

    for file, scope in _iter_files(root, include_pending, include_raw, include_meta):
        rel = file.relative_to(root).as_posix()
        if len(nodes) >= max_nodes:
            truncated = True
            break
        meta, body = _read(file)
        kind, is_meta = _classify(rel, scope, meta)
        title = (RESERVED_TITLES.get(kind) if is_meta
                 else str(meta.get("title") or file.stem))
        node = {
            "id": rel, "path": rel, "title": title,
            "kind": kind, "is_meta": is_meta, "scope": scope,
            "type": kind if is_meta else (meta.get("type") or ("resource" if scope != "raw" else "raw")),
            "status": "meta" if is_meta else (meta.get("status") or ("raw" if scope == "raw" else "active")),
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
    by_kind: dict[str, int] = {}
    for n in nodes:
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
    return {
        "nodes": nodes,
        "edges": edges,
        "missing": missing_nodes,
        "stats": {
            "nodes": len(nodes), "edges": len(edges),
            "missing": len(missing_nodes), "orphans": len(orphans),
            "by_kind": by_kind,
            "pending_included": include_pending, "meta_included": include_meta,
            "raw_included": include_raw, "truncated": truncated,
        },
    }


def neighbors(path: str, tenant_id: str | None = None, *, include_pending: bool = True,
              include_meta: bool = True, include_raw: bool = False) -> dict:
    """某条目的 1 跳邻域（中心 + 出边/入边邻居）。路径不在图中时返回 `{found: False}`。"""
    graph = build_graph(tenant_id, include_pending=include_pending,
                        include_meta=include_meta, include_raw=include_raw)
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
