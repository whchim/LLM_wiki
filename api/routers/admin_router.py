"""管理路由：重建索引 + embedding 回填（仅 admin）。"""
import json

from fastapi import APIRouter, Depends, Query, Request

import db
from api import auth, embedding, trace as trace_mod
from api.audit import audit_log

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/rebuild-index", response_model=dict)
def rebuild_index(request: Request,
                  user: auth.User = Depends(auth.require_roles("admin")),
                  _t: auth.User = Depends(trace_mod.trace("rebuild_index"))):
    """从 YAML 全量重建索引（缓存恢复）。"""
    n = db.rebuild_index()
    audit_log(user.username, "rebuild_index", target_path="NEXUS", detail={"entries": n})
    request.state.trace_detail = {"operation": "rebuild_index", "entries": n}
    return {"message": "索引已重建", "entries": n}


@router.post("/backfill-embeddings", response_model=dict)
def backfill_embeddings(request: Request, batch: int = 10,
                        user: auth.User = Depends(auth.require_roles("admin")),
                        _t: auth.User = Depends(trace_mod.trace("rebuild_index"))):
    """SP4：为 embedding 为空的条目批量补算向量（幂等，可重复执行直至 remaining=0）。

    向量 = 可重建缓存：模型换版/索引损坏时清空 embedding 列后重跑即可。"""
    if not embedding.is_available():
        request.state.trace_detail = {"operation": "backfill", "filled": 0, "error": "key 未配置"}
        return {"message": "DASHSCOPE_API_KEY 未配置，回填跳过", "filled": 0, "remaining": None}
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT path, COALESCE(title,'') || ' ' || COALESCE(description,'') "
            "FROM knowledge_entries WHERE embedding IS NULL LIMIT %s",
            (max(1, min(batch, 50)),)).fetchall()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM knowledge_entries WHERE embedding IS NULL").fetchone()[0]
    if not rows:
        return {"message": "全部条目已向量化", "filled": 0, "remaining": 0}
    paths = [r[0] for r in rows]
    texts = [r[1].strip() or r[0] for r in rows]   # 标题+描述为空则退化为路径
    try:
        vecs = embedding.embed_texts(texts)
    except embedding.EmbeddingError as e:
        request.state.trace_detail = {"operation": "backfill", "filled": 0, "error": str(e)}
        return {"message": f"embedding 失败：{e}", "filled": 0, "remaining": remaining}
    with db.get_conn() as conn:
        for path, vec in zip(paths, vecs):
            conn.execute(
                "UPDATE knowledge_entries SET embedding = %s::vector WHERE path = %s",
                (json.dumps(vec), path))
        remaining = conn.execute(
            "SELECT COUNT(*) FROM knowledge_entries WHERE embedding IS NULL").fetchone()[0]
    audit_log(user.username, "backfill_embeddings", target_path=",".join(paths)[:300],
              detail={"filled": len(paths)})
    request.state.trace_detail = {"operation": "backfill", "filled": len(paths)}
    return {"message": f"已补算 {len(paths)} 条", "filled": len(paths), "remaining": remaining}


# span_type → 中文展示名（与 Streamlit 可观测性页保持一致）
_SPAN_LABELS = {
    "compile_session": "编译会话", "search": "检索",
    "review_approve": "审核-通过", "review_reject": "审核-驳回",
    "review_resubmit": "审核-重提", "review_retry_ai": "审核-重试AI",
    "rebuild_index": "重建索引", "login": "登录",
}


@router.get("/observability", response_model=dict)
def observability(user: auth.User = Depends(auth.require_roles("reviewer", "admin"))) -> dict:
    """SP2.5 可观测性指标：当日编译、检索成功/失败、按类型平均延迟、Top 失败模式。

    原先只有 Streamlit 直连 trace_events 能看，Vue 迁移必须经 API 暴露。
    """
    with db.get_conn() as conn:
        today = conn.execute("SELECT to_char(now(), 'YYYY-MM-DD')").fetchone()[0]
        compile_stat = conn.execute(
            "SELECT COUNT(*) AS sessions, "
            "       COALESCE(SUM((detail->>'compiled')::int), 0) AS files, "
            "       COALESCE(SUM((detail->>'cached')::int), 0) AS cached, "
            "       COALESCE(AVG(latency_ms)::int, 0) AS avg_ms "
            "FROM trace_events WHERE span_type='compile_session' AND created_at >= %s",
            (today + " 00:00:00",)).fetchone()
        search_stat = conn.execute(
            "SELECT COUNT(*) AS total, "
            "       COALESCE(SUM((status='ok')::int),0) AS ok_cnt, "
            "       COALESCE(SUM((status='error')::int),0) AS err_cnt "
            "FROM trace_events WHERE span_type='search'").fetchone()
        latency = conn.execute(
            "SELECT span_type, AVG(latency_ms)::int AS avg_ms, COUNT(*) AS n "
            "FROM trace_events GROUP BY span_type ORDER BY n DESC LIMIT 8").fetchall()
        top_fail = conn.execute(
            "SELECT span_type, COALESCE(detail->>'error','unknown') AS err, COUNT(*) AS n "
            "FROM trace_events WHERE status='error' "
            "GROUP BY span_type, detail->>'error' ORDER BY n DESC LIMIT 10").fetchall()

    def label(s: str) -> str:
        return _SPAN_LABELS.get(s, s)

    total = search_stat[0]
    return {
        "date": today,
        "compile": {"sessions": compile_stat[0], "files": compile_stat[1],
                    "cached": compile_stat[2], "avg_ms": compile_stat[3]},
        "search": {"total": total, "ok": search_stat[1], "error": search_stat[2],
                   "success_rate": round(search_stat[1] / total, 4) if total else None},
        "latency": [{"span_type": r[0], "label": label(r[0]), "avg_ms": r[1], "count": r[2]}
                    for r in latency],
        "top_errors": [{"span_type": r[0], "label": label(r[0]), "error": r[1], "count": r[2]}
                       for r in top_fail],
    }


@router.get("/reports", response_model=dict)
def reports(kind: str = Query("growth", pattern="^(growth|health)$"),
            user: auth.User = Depends(auth.require_roles("reviewer", "admin"))) -> dict:
    """周报读取（自增长 / 健康巡检）。周报由 Claude Code 写在共享卷，Vue 经 API 取。
    与 Streamlit 原实现一致：取最新一份。"""
    import glob
    import os
    from pathlib import Path

    kb = Path(os.environ.get("KB_ROOT", os.path.join(os.path.dirname(db.__file__), "..", "vault")))
    prefix = "自增长周报_" if kind == "growth" else "健康周报_"
    found = sorted(glob.glob(str(kb / "NEXUS" / "研究" / f"{prefix}*.md")), reverse=True)
    if not found:
        return {"kind": kind, "exists": False, "name": None, "content": None}
    latest = Path(found[0])
    return {"kind": kind, "exists": True, "name": latest.name,
            "content": latest.read_text(encoding="utf-8", errors="replace")}