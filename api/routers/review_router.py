"""审核路由：待审/已驳列表 + 通过/驳回/重提/重试 AI。

复用 ops.approve_entry / reject_entry / resubmit（含 YAML+PG 双写），
FastAPI 侧只做 HTTP 层 + 角色校验 + 审计。
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

import db
import ops
from api import auth, trace as trace_mod
from api.audit import audit_log
from api.schemas import Message, RejectRequest, ReviewOut
from output_schema import validate_review_output

router = APIRouter(prefix="/reviews", tags=["review"])


def _to_out(rec: dict) -> dict:
    """DB 行 → 响应模型（ai_scores 已是 dict/JSONB；附带 LLM 输出契约校验标记）。"""
    out = dict(rec)
    if isinstance(out.get("ai_scores"), str):
        try:
            out["ai_scores"] = json.loads(out["ai_scores"])
        except json.JSONDecodeError:
            out["ai_scores"] = None
    if isinstance(out.get("ai_scores"), dict):
        errs = validate_review_output(out["ai_scores"])
        out["ai_scores_valid"] = len(errs) == 0
        out["ai_scores_errors"] = errs
    else:
        out["ai_scores_valid"] = None
        out["ai_scores_errors"] = []
    return out


@router.get("/pending", response_model=list[ReviewOut])
def pending(user: auth.User = Depends(auth.require_roles("reviewer", "admin"))):
    return [_to_out(r) for r in db.list_pending_reviews()]


@router.get("/rejected", response_model=list[ReviewOut])
def rejected(user: auth.User = Depends(auth.require_roles("reviewer", "admin"))):
    return [_to_out(r) for r in db.list_rejected_reviews()]


@router.post("/{review_id}/approve", response_model=dict)
def approve(review_id: int, request: Request,
            user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
            _t: auth.User = Depends(trace_mod.trace("review_approve"))):
    """通过：文件移入 NEXUS/概念 + YAML status=active + 双写 + index 更新。"""
    rec = _find_review(review_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    try:
        target = ops.approve_entry(review_id, rec["nexus_path"],
                                   "NEXUS/概念/" + Path(rec["nexus_path"]).name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=f"文件不存在，可能已被处理：{e}")
    audit_log(user.username, "review_approve", target_path=target, detail={"review_id": review_id})
    request.state.trace_detail = {"operation": "approve", "target_path": target,
                                  "review_id": review_id}
    return {"message": "已通过", "target_path": target}


@router.post("/{review_id}/reject", response_model=dict)
def reject(review_id: int, body: RejectRequest, request: Request,
           user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
           _t: auth.User = Depends(trace_mod.trace("review_reject"))):
    """驳回：YAML status=draft + 双写 + reject_reason。"""
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="驳回原因不能为空")
    rec = _find_review(review_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    ops.reject_entry(review_id, rec["nexus_path"], body.reason)
    audit_log(user.username, "review_reject", target_path=rec["nexus_path"],
              detail={"review_id": review_id, "reason": body.reason})
    request.state.trace_detail = {"operation": "reject", "target_path": rec["nexus_path"],
                                  "review_id": review_id}
    return {"message": "已驳回"}


@router.post("/{review_id}/resubmit", response_model=dict)
def resubmit(review_id: int, request: Request,
             user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
             _t: auth.User = Depends(trace_mod.trace("review_resubmit"))):
    """重新提交审核：YAML status=pending + 双写 + review 触发文件。"""
    rec = _find_review(review_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    ops.resubmit(review_id, rec["nexus_path"])
    ops.write_trigger("review", [rec["nexus_path"]], "api")
    audit_log(user.username, "review_resubmit", target_path=rec["nexus_path"],
              detail={"review_id": review_id})
    request.state.trace_detail = {"operation": "resubmit", "target_path": rec["nexus_path"],
                                  "review_id": review_id}
    return {"message": "已重新提交 AI 审核"}


@router.post("/{review_id}/retry-ai", response_model=dict)
def retry_ai(review_id: int, request: Request,
             user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
             _t: auth.User = Depends(trace_mod.trace("review_retry_ai"))):
    """重试 AI 审核：写 review 触发文件（Claude Code 消费）。"""
    rec = _find_review(review_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    ops.write_trigger("review", [rec["nexus_path"]], "api")
    audit_log(user.username, "trigger_write", target_path=rec["nexus_path"],
              detail={"kind": "review", "review_id": review_id})
    request.state.trace_detail = {"operation": "retry_ai", "target_path": rec["nexus_path"],
                                  "review_id": review_id}
    return {"message": "已加入 AI 审核队列"}


def _find_review(review_id: int) -> dict | None:
    for r in db.list_pending_reviews():
        if r["id"] == review_id:
            return r
    for r in db.list_rejected_reviews():
        if r["id"] == review_id:
            return r
    return None