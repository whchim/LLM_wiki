"""搜索路由（SP4 混合检索）：grep 精确 + pgvector 向量语义 → 加权融合 re-rank。

降级铁律：embedding 服务故障（未配 key / 网络失败）自动退化为 grep-only，
不崩、不阻塞——只失去模糊匹配能力，精确匹配行为与 Demo 完全一致。
"""
import json
import math
import os

from fastapi import APIRouter, Depends, Query, Request

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
                    text = f.read()
                # 文件系统可能比索引更新，必须以 frontmatter 的 active 为准，
                # 避免 draft/stale 文档通过 grep 通道泄露。
                if not _is_active(text):
                    continue
                if query in text:
                    hits.append(os.path.relpath(full, _kb_root()).replace(os.sep, "/"))
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def _is_active(text: str) -> bool:
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
def search(request: Request, query: str = Query(..., min_length=1, max_length=300),
           mode: str = Query("auto", pattern="^(auto|grep|vector)$"),
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
def missed(limit: int = Query(20, ge=1, le=500),
           user: auth.User = Depends(auth.get_current_user)) -> dict:
    """搜索未命中 Top N（知识缺口）——混合检索后=双通道都零命中的查询。"""
    return {"items": db.top_missed_queries(limit)}


@router.get("/search/stats")
def stats(user: auth.User = Depends(auth.get_current_user)) -> dict:
    """搜索统计：总次数/未命中数/未命中率。"""
    return db.search_stats()


@router.get("/entries")
def entries(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0, le=100000),
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


@router.get("/entries/mine")
def my_entries(limit: int = Query(200, ge=1, le=1000),
               user: auth.User = Depends(auth.get_current_user)) -> dict:
    """我的知识库：按流转阶段追溯"我提交了什么、现在到哪一步"。

    只读、无新表——归属从 audit_logs 的 upload 事件追溯（operator=当前用户），
    再按 raw_path 左连 compile_tasks（编译）→ knowledge_entries（是否已入库）、
    pending_reviews（审核结论）。

    ⚠️ 已知边界：knowledge_entries 目前没有归属字段，所以这里的"我的"指
    "我上传过的"，而不是"归我所有的"；条目一旦发布，无法反查作者。
    """
    with db.get_conn() as conn:
        # 1) 当前用户上传过哪些 RAW 路径（target_path 为逗号拼接，截断风险可接受）
        rows = conn.execute(
            "SELECT target_path AS raw_path, MAX(timestamp) AS uploaded_at "
            "FROM audit_logs WHERE operator=%s AND action='upload' "
            "GROUP BY target_path ORDER BY MAX(timestamp) DESC LIMIT %s",
            (user.username, limit)).fetchall()
        raw_paths: list[tuple[str, object]] = []
        for raw_path, uploaded_at in rows:
            for p in str(raw_path or "").split(","):
                p = p.strip()
                if p:
                    raw_paths.append((p, uploaded_at))
        if not raw_paths:
            return {"items": [], "counts": {}, "owner_field": False}

        paths = [p for p, _ in raw_paths]
        # 2) 编译任务：同一 raw_path 可能多次编译，只取最近一次
        task_rows = conn.execute(
            "SELECT DISTINCT ON (raw_path) raw_path, status, nexus_path, error_msg, completed_at "
            "FROM compile_tasks WHERE raw_path = ANY(%s) "
            "ORDER BY raw_path, id DESC", (paths,)).fetchall()
        tasks = {r[0]: r for r in task_rows}
        # 3) 已入库？用 basename 匹配（审核会把路径从 pending_review/ 移到 NEXUS/）
        name_rows = conn.execute(
            "SELECT path, title, type, status, version, updated_at, "
            "       split_part(path, '/', -1) AS name FROM knowledge_entries").fetchall()
        by_name = {r[6]: r for r in name_rows}
        # 4) 审核结论（按 basename 关联）
        review_rows = conn.execute(
            "SELECT split_part(nexus_path, '/', -1) AS name, submitter, ai_verdict, "
            "       human_decision, reject_reason FROM pending_reviews").fetchall()
        reviews: dict[str, list] = {}
        for r in review_rows:
            reviews.setdefault(r[0], []).append(r)

    items = []
    for raw_path, uploaded_at in raw_paths:
        name = raw_path.split("/")[-1]
        task = tasks.get(raw_path)
        entry = by_name.get(name)
        revs = reviews.get(name) or []
        human = next((r[3] for r in revs if r[3]), None)
        reject_reason = next((r[4] for r in revs if r[4]), None)

        if human == "approved" or (entry is not None and entry[3] == "active"):
            stage = "已发布"
        elif human == "rejected":
            stage = "已驳回"
        elif task is not None and task[1] == "failed":
            stage = "编译失败"
        elif task is not None and task[1] in ("pending", "processing"):
            stage = "编译中"
        elif revs or (entry is not None and entry[3] == "pending"):
            stage = "待审核"
        elif task is not None and task[1] == "done":
            stage = "已编译"
        else:
            stage = "已上传"

        items.append({
            "raw_name": name,
            "raw_path": raw_path,
            "uploaded_at": uploaded_at,
            "stage": stage,
            "compile_status": task[1] if task else None,
            "compile_error": task[3] if task else None,
            "nexus_path": task[2] if task else None,
            "entry_path": entry[0] if entry else None,
            "entry_title": entry[1] if entry else None,
            "entry_type": entry[2] if entry else None,
            "entry_version": entry[4] if entry else None,
            "entry_updated_at": entry[5] if entry else None,
            "review_decision": human,
            "reject_reason": reject_reason,
        })

    counts: dict[str, int] = {}
    for it in items:
        counts[it["stage"]] = counts.get(it["stage"], 0) + 1
    return {"items": items, "counts": counts, "owner_field": False}
