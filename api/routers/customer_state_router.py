"""销售客户状态负责人工作台 API。"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

import customer_state
from api import auth, trace as trace_mod
from api.audit import audit_log
from api.schemas import StateCorrectionRequest, StateDecisionRequest, StateWithdrawRequest

router = APIRouter(prefix="/customer-states", tags=["customer-state"])


@router.get("/proposals/pending", response_model=list[dict])
def pending_proposals(limit: int = Query(100, ge=1, le=500),
                      user: auth.User = Depends(auth.require_roles("reviewer", "admin"))):
    return customer_state.list_pending_proposals(limit)


@router.get("/customers", response_model=list[dict])
def customers(limit: int = Query(100, ge=1, le=500),
              user: auth.User = Depends(auth.require_roles("reviewer", "admin"))):
    """客户当前状态总览（运营视角）：负责人确认后的结果在这里可见。

    注意：本路由必须声明在 `/{customer_id}` 之前，否则会被当成客户标识匹配。
    """
    return customer_state.list_customers_with_state(limit)


@router.get("/{customer_id}", response_model=dict)
def current_state(customer_id: str, user: auth.User = Depends(auth.get_current_user)):
    result = customer_state.get_current_state_with_customer(customer_id)
    if result is None:
        raise HTTPException(status_code=404, detail="客户当前没有已确认状态")
    return result


@router.get("/{customer_id}/events", response_model=list[dict])
def state_events(customer_id: str, limit: int = Query(100, ge=1, le=500),
                 user: auth.User = Depends(auth.get_current_user)):
    return customer_state.list_state_events(customer_id, limit)


@router.get("/ops/due-expirations", response_model=list[dict])
def due_expirations(limit: int = Query(100, ge=1, le=500),
                    user: auth.User = Depends(auth.require_roles("reviewer", "admin"))):
    return customer_state.list_due_expirations(limit=limit)


@router.get("/ops/conflicts", response_model=list[dict])
def conflicts(limit: int = Query(100, ge=1, le=500),
              user: auth.User = Depends(auth.require_roles("reviewer", "admin"))):
    return customer_state.list_proposal_conflicts(limit)


@router.post("/ops/expire", response_model=list[dict])
def expire(request: Request, limit: int = Query(100, ge=1, le=500),
           user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
           _trace: auth.User = Depends(trace_mod.trace("customer_state_expire"))):
    results = customer_state.expire_due_states(actor=user.username, limit=limit)
    audit_log(user.username, "customer_state_expire", detail={"count": len(results)})
    request.state.trace_detail = {"operation": "customer_state_expire", "count": len(results)}
    return results


@router.post("/proposals/{proposal_id}/decision", response_model=dict)
def decide(proposal_id: str, body: StateDecisionRequest, request: Request,
           user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
           _trace: auth.User = Depends(trace_mod.trace("customer_state_decision"))):
    if body.decision not in {"approved", "modified", "rejected"}:
        raise HTTPException(status_code=400, detail="decision 必须是 approved、modified 或 rejected")
    if body.decision == "rejected" and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="驳回必须填写原因")
    if body.decision == "modified" and (not body.final_state or not (body.reason or "").strip()):
        raise HTTPException(status_code=400, detail="修改确认必须填写目标状态和原因")
    try:
        result = customer_state.decide_proposal(
            proposal_id, body.decision, user.username, body.final_state,
            body.reason, body.evidence_refs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_log(user.username, "customer_state_decision", target_path=proposal_id,
              detail={"decision": body.decision, "final_state": body.final_state})
    request.state.trace_detail = {"operation": "customer_state_decision", "proposal_id": proposal_id,
                                  "decision": body.decision}
    return result


@router.post("/{customer_id}/withdraw", response_model=dict)
def withdraw(customer_id: str, body: StateWithdrawRequest, request: Request,
             user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
             _trace: auth.User = Depends(trace_mod.trace("customer_state_withdraw"))):
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="撤回原因不能为空")
    try:
        result = customer_state.withdraw_current_state(customer_id, user.username, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_log(user.username, "customer_state_withdraw", target_path=customer_id,
              detail={"reason": body.reason})
    request.state.trace_detail = {"operation": "customer_state_withdraw", "customer_id": customer_id}
    return result


@router.post("/{customer_id}/correct", response_model=dict)
def correct(customer_id: str, body: StateCorrectionRequest, request: Request,
            user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
            _trace: auth.User = Depends(trace_mod.trace("customer_state_correction"))):
    if not body.reason.strip() or not body.evidence_refs:
        raise HTTPException(status_code=400, detail="更正必须填写原因和证据引用")
    valid_until = None
    if body.valid_until:
        try:
            from datetime import datetime
            valid_until = datetime.fromisoformat(body.valid_until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="valid_until 不是合法 ISO-8601 时间") from exc
    try:
        result = customer_state.correct_current_state(
            customer_id, body.final_state, user.username, body.reason, body.evidence_refs, valid_until)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_log(user.username, "customer_state_correction", target_path=customer_id,
              detail={"final_state": body.final_state, "reason": body.reason})
    request.state.trace_detail = {"operation": "customer_state_correction", "customer_id": customer_id}
    return result
