"""检索原语（SP4 混合检索的**可复用落点**）：grep 精确 + pgvector 向量语义 → 加权融合。

为什么需要这个模块：这些原语原来只活在 `api/routers/search_router.py` 里，于是
"应用内问答（`/ask`）"要么重复实现一遍检索、要么去 import 一个路由模块——两者都是坏味道。
现在**唯一实现**在这里，路由只做取参/鉴权/写日志，问答服务直接复用，口径不会再漂移。

降级铁律：embedding 服务故障（未配 key / 网络失败）自动退化为 grep-only，不崩、不阻塞。

`tenant` 分区：路径一律经 `paths.kb_root()` 解析（L3.5），检索天然只在本租户子树内。
"""
from __future__ import annotations

import json
import math
import os

# 融合默认权重（SP4 决策 5；tools/tune_search.py 网格标定：14 条黄金集上权重不敏感——
# vector 排序主导，MRR 全网格=1.00，样本不足区分 grep/vector 权重，扩集后重标定）
W_GREP_DEFAULT = 0.5
W_VEC_DEFAULT = 0.3

# 缺口判定阈值（SP4 v0.1.1 勘误落地）：grep 零命中 且 vector 最高相似度 < τ 才记缺口。
# 标定（tools/tune_search.py）：缺口样本 max_sim∈[0.360,0.487]，命中样本下限 0.545，
# 分隔区间 (0.487, 0.545)，取中值 0.52（gap 宁偏严，避免看板噪音）。
GAP_SIM_THRESHOLD = 0.52

# 问答取正文做依据时的上限（防把整库塞进 prompt）
SNIPPET_MAX_CHARS = 2500
SNIPPET_DIRS = ("NEXUS", "pending_review")


def kb_root() -> str:
    """知识库根：按租户分区（L3.5；默认租户 = KB_ROOT），每次调用动态解析。"""
    import paths
    return str(paths.kb_root())


def is_active(text: str) -> bool:
    """容错解析 YAML frontmatter；损坏或缺失状态的文件默认不返回。"""
    if not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False
    try:
        import yaml
        meta = yaml.safe_load(parts[1])
    except Exception:
        return False
    return isinstance(meta, dict) and meta.get("status") == "active"


def grep_files(query: str) -> list[str]:
    """grep -rl 同款语义：NEXUS 下正文包含 query 的 .md 文件（相对知识库根）。

    纯 Python 实现（os.walk + 全文包含匹配），不依赖系统 grep 命令。"""
    root = kb_root()
    nexus = os.path.join(root, "NEXUS")
    hits: list[str] = []
    for dirpath, _, files in os.walk(nexus):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8") as f:
                    text = f.read()
                # 文件系统可能比索引更新，必须以 frontmatter 的 active 为准，
                # 避免 draft/stale 文档通过 grep 通道泄露。
                if not is_active(text):
                    continue
                if query in text:
                    hits.append(os.path.relpath(full, root).replace(os.sep, "/"))
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def vector_search(query: str, top_k: int = 20) -> list[dict] | None:
    """pgvector 余弦 Top-K（status='active'）。失败/不可用返回 None（调用方降级）。"""
    import db
    from api import embedding
    if not embedding.is_available():
        return None
    try:
        vec = embedding.embed_query(query)
    except embedding.EmbeddingError:
        return None
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT path, title, 1 - (embedding <=> %s::vector) AS similarity "
                "FROM knowledge_entries WHERE status='active' AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (json.dumps(vec), json.dumps(vec), top_k)).fetchall()
        return [{"path": r[0], "title": r[1], "similarity": float(r[2])} for r in rows]
    except Exception:
        return None


def fuse(grep_hits: list[str], vec_hits: list[dict] | None,
         w_grep: float = W_GREP_DEFAULT, w_vec: float = W_VEC_DEFAULT) -> list[dict]:
    """加权分数融合：score = w_grep×grep贡献 + w_vec×similarity。

    grep_hits 是无序文件路径（字面命中等权，除以 sqrt(n) 温和归一）；
    vec_hits 带相似度直接加权。返回按 score 降序的统一条目列表。"""
    scores: dict[str, dict] = {}

    def _add(path: str, channel: str, pts: float):
        entry = scores.setdefault(path, {"path": path, "score": 0.0,
                                         "channels": {"grep": 0, "vector": 0}})
        entry["score"] += pts
        entry["channels"][channel] = 1

    if grep_hits:
        w = w_grep / math.sqrt(len(grep_hits))
        for p in grep_hits:
            _add(p, "grep", w)
    if vec_hits:
        for item in vec_hits:
            _add(item["path"], "vector", w_vec * item["similarity"])

    return sorted(scores.values(), key=lambda x: x["score"], reverse=True)


def read_entry(path: str, max_chars: int = SNIPPET_MAX_CHARS) -> dict | None:
    """读条目正文（供问答做依据）。**只允许知识库内** NEXUS/pending_review 下的 .md/.txt。

    返回 `{path, title, status, content, truncated}`；不存在/越界/不可读返回 None。
    截断是刻意的：问答 prompt 不能把整库塞进去（成本与串味都要控）。
    """
    import yaml
    from pathlib import Path

    root = Path(kb_root()).resolve()
    target = (root / path).resolve()
    if root not in target.parents:                       # 防路径穿越（越出知识库根）
        return None
    rel = target.relative_to(root).as_posix()
    if not rel.startswith(SNIPPET_DIRS) or target.suffix.lower() not in (".md", ".txt", ".markdown"):
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    title, status, body = target.stem, None, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1])
                if isinstance(meta, dict):
                    title = str(meta.get("title") or title)
                    status = meta.get("status")
            except Exception:
                pass
            body = parts[2]
    content = body.strip()
    truncated = len(content) > max_chars
    return {"path": rel, "title": title, "status": status,
            "content": content[:max_chars], "truncated": truncated}


def run_search(query: str, *, mode: str = "auto", top_k: int = 20,
               with_content: bool = False, content_k: int = 6,
               max_chars: int = SNIPPET_MAX_CHARS) -> dict:
    """一次完整检索（grep + 向量 → 融合 → 可选取正文），返回结构化结果。

    返回：entries（按分数降序，含 channels/score/content）、channels 计数、
    gap（缺口判据）、max_sim（向量最高相似度，向量不可用为 None）。
    **不写 search_logs**——是否记日志、怎么记由调用方决定（评测工具因此不会污染看板）。
    """
    q = (query or "").strip()
    if not q:
        return {"query": q, "entries": [], "channels": {"grep": 0, "vector": 0},
                "gap": True, "max_sim": None, "warnings": []}

    grep_hits = grep_files(q) if mode in ("auto", "grep") else []
    vec_hits = vector_search(q, top_k) if mode in ("auto", "vector") else None
    fused = fuse(grep_hits, vec_hits)[:top_k]

    # 缺口判据（SP4 v0.1.1）：grep 零命中 且 向量最高相似度 < τ；
    # 向量不可用/无向量 → 退化为"grep 零命中即缺口"（计分仍用 match_count=0，看板零改动）。
    max_sim = max((v["similarity"] for v in vec_hits), default=None) if vec_hits else None
    gap = (len(grep_hits) == 0) and (max_sim is None or max_sim < GAP_SIM_THRESHOLD)

    warnings: list[str] = []
    if mode in ("auto", "vector") and vec_hits is None:
        warnings.append("向量通道不可用，已降级为 grep-only")
    if with_content:
        for entry in fused[:content_k]:
            doc = read_entry(entry["path"], max_chars)
            if doc is None:
                warnings.append(f"条目不可读：{entry['path']}")
                continue
            entry["title"] = doc["title"]
            entry["status"] = doc["status"]
            entry["content"] = doc["content"]
            entry["truncated"] = doc["truncated"]

    return {"query": q, "entries": fused, "channels": {
        "grep": len(grep_hits),
        "vector": len(vec_hits) if vec_hits is not None else 0,
    }, "gap": gap, "max_sim": max_sim, "warnings": warnings}
