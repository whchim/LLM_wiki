"""管理路由：重建索引（仅 admin）。"""
from fastapi import APIRouter, Depends, Request

import db
from api import auth, trace as trace_mod
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