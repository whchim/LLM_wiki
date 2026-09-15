"""搜索路由（SP4 混合检索）：grep 精确 + pgvector 向量语义 → 加权融合 re-rank。

检索原语已抽到 `core/retrieval.py`（应用内问答 `/ask` 复用同一套，口径不漂移）；
本模块保留 `_grep/_vector_search/_fuse` 作为**模块级别名**——`tools/eval_search.py`、
`tools/tune_search.py`、`tests/test_trace.py`（会 monkeypatch 这三个名字）都依赖它们。

降级铁律：embedding 服务故障（未配 key / 网络失败）自动退化为 grep-only，
不崩、不阻塞——只失去模糊匹配能力，精确匹配行为与 Demo 完全一致。
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import db
import retrieval
from api import auth, trace as trace_mod

router = APIRouter(tags=["search"])

# 原语别名（唯一实现见 core/retrieval.py）
W_GREP_DEFAULT = retrieval.W_GREP_DEFAULT
W_VEC_DEFAULT = retrieval.W_VEC_DEFAULT
GAP_SIM_THRESHOLD = retrieval.GAP_SIM_THRESHOLD
_grep = retrieval.grep_files
_vector_search = retrieval.vector_search
_fuse = retrieval.fuse


def _kb_root() -> str:
    """知识库根：按租户分区（L3.5；默认租户 = KB_ROOT），每次调用动态解析。"""
    return retrieval.kb_root()


def _is_active(text: str) -> bool:
    return retrieval.is_active(text)


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
    # 走 retrieval.run_search 会另算一次缺口判据，但本端点必须让 monkeypatch 过的
    # `_grep`/`_vector_search`/`_fuse` 生效（工具与测试依赖），故仍按原语拼装。
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


@router.get("/entries/content")
def entry_content(path: str = Query(..., min_length=1, max_length=500),
                  user: auth.User = Depends(auth.get_current_user)) -> dict:
    """条目正文（供审核预览与知识浏览；只读，限 kb_root 内的 Markdown/文本）。

    Vue 前端无法访问共享卷，因此把只读预览做成受认证的 API。
    """
    import os
    from pathlib import Path

    kb_root = Path(_kb_root()).resolve()
    target = (kb_root / path).resolve()
    # 防路径穿越：解析后必须仍在 kb_root 内
    if kb_root not in target.parents and target != kb_root:
        raise HTTPException(status_code=400, detail="路径非法：越出知识库根目录")
    if target.suffix.lower() not in {".md", ".txt", ".markdown"}:
        raise HTTPException(status_code=400, detail="仅支持预览 .md / .markdown / .txt")
    if not target.is_file():
        return {"path": path, "exists": False, "content": None, "size": 0}
    size = target.stat().st_size
    if size > 1_000_000:
        raise HTTPException(status_code=413, detail=f"文件过大（{size} 字节），请用 Obsidian 打开 vault/ 查看")
    return {"path": path, "exists": True, "content": target.read_text(encoding="utf-8", errors="replace"),
            "size": size}


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
