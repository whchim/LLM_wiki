"""搜索路由（SP4 混合检索）：grep 精确 + pgvector 向量语义 → 加权融合 re-rank。

降级铁律：embedding 服务故障（未配 key / 网络失败）自动退化为 grep-only，
不崩、不阻塞——只失去模糊匹配能力，精确匹配行为与 Demo 完全一致。
"""
import json
import math
import os

from fastapi import APIRouter, Depends, Request

import db
from api import auth, trace as trace_mod

router = APIRouter(tags=["search"])

# 融合默认权重（SP4 决策 5；tools/tune_search.py 网格标定：14 条黄金集上权重不敏感——
# vector 排序主导，MRR 全网格=1.00，样本不足区分 grep/vector 权重，扩集后重标定）
W_GREP_DEFAULT = 0.5
W_VEC_DEFAULT = 0.3

# 缺口判定阈值（SP4 v0.1.1 勘误落地）：grep 零命中 且 vector 最高相似度 < τ 才记缺口。
# 标定（tools/tune_search.py）：缺口样本 max_sim∈[0.360,0.487]，命中样本下限 0.545，
# 分隔区间 (0.487, 0.545)，取中值 0.52（gap 宁偏严，避免看板噪音）。
GAP_SIM_THRESHOLD = 0.52


def _kb_root() -> str:
    """动态读取 KB_ROOT（每次调用），保证测试/容器的 env 生效。"""
    return os.environ.get("KB_ROOT", os.path.join(os.path.dirname(db.__file__), "..", "vault"))


def _grep(query: str) -> list[str]:
    """grep -rl 同款语义：NEXUS 下正文包含 query 的 .md 文件（相对 KB_ROOT）。

    纯 Python 实现（os.walk + 全文包含匹配），不依赖系统 grep 命令。"""
    nexus = os.path.join(_kb_root(), "NEXUS")
    hits: list[str] = []
    for dirpath, _, files in os.walk(nexus):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8") as f:
                    if query in f.read():
                        hits.append(os.path.relpath(full, _kb_root()).replace(os.sep, "/"))
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def _vector_search(query: str, top_k: int = 20) -> list[dict] | None:
    """pgvector 余弦 Top-K（status='active'）。失败/不可用返回 None（调用方降级）。"""
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


def _fuse(grep_hits: list[str], vec_hits: list[dict] | None,
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


@router.get("/search")
def search(query: str, request: Request, mode: str = "auto",
           user: auth.User = Depends(trace_mod.trace("search"))) -> dict:
    """混合检索（SP4）：grep 精确 + 向量语义（auto=融合；grep/vector=单通道）。

    降级：embedding 不可用/失败 → 自动 grep-only。"""
    if not query.strip():
        request.state.trace_detail = {"operation": "search", "query": query, "hit_count": 0}
        return {"query": query, "matches": 0, "files": [], "channels": {"grep": 0, "vector": 0}}

    q = query.strip()
    grep_hits = _grep(q) if mode in ("auto", "grep") else []
    vec_hits = _vector_search(q) if mode in ("auto", "vector") else None

    fused = _fuse(grep_hits, vec_hits)
    channels = {"grep": len(grep_hits),
                "vector": len(vec_hits) if vec_hits is not None else 0}
    files = [e["path"] for e in fused][:50]

    # 缺口判据（SP4 v0.1.1 勘误）：grep 零命中 且 向量最高相似度 < τ。
    # 向量不可用/库中无向量（vec_hits 为 None 或空）→ 退化为旧语义（grep 零命中即缺口）；
    # 计分以 match_count=0 表达，看板缺口查询（match_count=0）零改动自动对齐。
    max_sim = max((v["similarity"] for v in vec_hits), default=None) if vec_hits else None
    gap = (len(grep_hits) == 0) and (max_sim is None or max_sim < GAP_SIM_THRESHOLD)

    db.insert_search_log(q, 0 if gap else len(fused), "api")
    request.state.trace_detail = {
        "operation": "search", "query": q, "hit_count": len(fused),
        "channels": channels, "mode": mode, "gap": gap, "max_sim": max_sim,
    }
    return {"query": q, "matches": len(fused), "files": files, "gap": gap,
            "channels": channels, "entries": fused[:20]}


@router.get("/search/missed")
def missed(limit: int = 20,
           user: auth.User = Depends(auth.get_current_user)) -> dict:
    """搜索未命中 Top N（知识缺口）——混合检索后=双通道都零命中的查询。"""
    return {"items": db.top_missed_queries(limit)}


@router.get("/search/stats")
def stats(user: auth.User = Depends(auth.get_current_user)) -> dict:
    """搜索统计：总次数/未命中数/未命中率。"""
    return db.search_stats()


@router.get("/entries")
def entries(limit: int = 100, offset: int = 0,
            type_: str | None = None, status: str | None = None,
            user: auth.User = Depends(auth.get_current_user)) -> dict:
    """条目列表（分页/过滤）。"""
    sql = "SELECT path, type, title, department, status, version, updated_at FROM knowledge_entries"
    conds, params = [], []
    if type_:
        conds.append("type=%s")
        params.append(type_)
    if status:
        conds.append("status=%s")
        params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY path LIMIT %s OFFSET %s"
    params += [limit, offset]
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
    keys = ["path", "type", "title", "department", "status", "version", "updated_at"]
    return {"total": total, "items": [dict(zip(keys, r)) for r in rows]}