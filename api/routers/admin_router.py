"""管理路由：重建索引 + embedding 回填（仅 admin）。"""
import json

from fastapi import APIRouter, Depends, Request

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