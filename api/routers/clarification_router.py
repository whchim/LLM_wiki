"""销售事实澄清会话 API（阶段 4）。"""
from fastapi import APIRouter, Depends, HTTPException, Request

import db
import rules
from api import auth, trace as trace_mod
from api.audit import audit_log
from api.schemas import ClarificationAnswerRequest, ClarificationSessionRequest, SalesIntakeRequest

import customer_state
import sales_preprocess

router = APIRouter(prefix="/clarifications", tags=["clarification"])


@router.post("/intake", response_model=dict)
def intake(body: SalesIntakeRequest, request: Request,
           user: auth.User = Depends(auth.get_current_user),
           _trace: auth.User = Depends(trace_mod.trace("sales_intake"))):
    """销售提交一次脱敏纪要；只创建证据和澄清会话，不调用模型或改状态。"""
    prepared = sales_preprocess.preprocess_sales_input({
        "idempotency_key": body.idempotency_key,
        "customer_id": body.customer_id,
        "content": body.content,
        "occurred_at": body.occurred_at,
        "submitted_by": user.username,
        "source_type": body.source_type,
        "source_ref": body.source_ref,
        "language": "zh-CN",
    })
    if not prepared["accepted"]:
        raise HTTPException(status_code=400, detail={"message": "纪要未通过提交门禁", "errors": prepared["errors"],
                                                       "risk_flags": prepared["risk_flags"]})
    normalized = prepared["normalized"]
    try:
        conversation = db.get_conversation_by_idempotency_key(normalized["idempotency_key"])
        if conversation is not None:
            if conversation["customer_id"] != normalized["customer_id"]:
                raise HTTPException(status_code=409, detail="idempotency_key 已绑定其他客户，拒绝复用")
            if user.role not in {"reviewer", "admin"} and not db.can_access_conversation(conversation["conversation_id"], user.username):
                raise HTTPException(status_code=403, detail="无权重复提交该销售纪要")
        else:
            conversation = customer_state.create_conversation(
                customer_id=normalized["customer_id"], idempotency_key=normalized["idempotency_key"],
                source_type=normalized["source_type"], occurred_at=normalized["occurred_at"],
                submitted_by=user.username, source_ref=normalized.get("source_ref"), owner_user_id=user.username)
        existing_session = db.get_clarification_session_for_conversation(conversation["conversation_id"])
        if existing_session is not None:
            evidence = db.latest_evidence(conversation["conversation_id"])
            audit_log(user.username, "sales_intake", target_path=existing_session["session_id"],
                      detail={"idempotent_replay": True, "conversation_id": conversation["conversation_id"]})
            return {"conversation": conversation, "evidence": evidence, "session": existing_session,
                    "gate": {"risk_flags": prepared["risk_flags"], "numeric_ref_count": 0, "idempotent_replay": True}}
        evidence = customer_state.add_evidence(
            conversation["conversation_id"], normalized["content_redacted"], normalized.get("source_ref"))
        session = db.create_clarification_session(conversation["conversation_id"], user.username)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_log(user.username, "sales_intake", target_path=session["session_id"],
              detail={"conversation_id": conversation["conversation_id"], "evidence_id": evidence["evidence_id"],
                      "numeric_ref_count": len(prepared["numeric_refs"])})
    request.state.trace_detail = {"operation": "sales_intake", "session_id": session["session_id"]}
    return {"conversation": conversation, "evidence": evidence, "session": session,
            "gate": {"risk_flags": prepared["risk_flags"], "numeric_ref_count": len(prepared["numeric_refs"])}}


def _allowed(user: auth.User, session_id: str) -> bool:
    return user.role in {"reviewer", "admin"} or db.can_access_clarification_session(session_id, user.username)


@router.post("/sessions", response_model=dict)
def create_session(body: ClarificationSessionRequest, request: Request,
                   user: auth.User = Depends(auth.get_current_user),
                   _trace: auth.User = Depends(trace_mod.trace("clarification_session_create"))):
    if user.role not in {"reviewer", "admin"} and not db.can_access_conversation(body.conversation_id, user.username):
        raise HTTPException(status_code=403, detail="无权为该销售纪要创建澄清会话")
    try:
        result = db.create_clarification_session(body.conversation_id, user.username, body.max_rounds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_log(user.username, "clarification_session_create", target_path=result["session_id"],
              detail={"conversation_id": body.conversation_id})
    request.state.trace_detail = {"operation": "clarification_session_create", "session_id": result["session_id"]}
    return result


@router.get("/sessions/{session_id}", response_model=dict)
def get_session(session_id: str, user: auth.User = Depends(auth.get_current_user)):
    if not _allowed(user, session_id):
        raise HTTPException(status_code=403, detail="无权访问该澄清会话")
    result = db.get_clarification_session(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="澄清会话不存在")
    return result


@router.get("/mine", response_model=list[dict])
def mine(user: auth.User = Depends(auth.get_current_user)):
    return db.list_user_clarification_sessions(user.username)


@router.post("/sessions/{session_id}/answers", response_model=dict)
def answer(session_id: str, body: ClarificationAnswerRequest, request: Request,
           user: auth.User = Depends(auth.get_current_user),
           _trace: auth.User = Depends(trace_mod.trace("clarification_answer"))):
    if not _allowed(user, session_id):
        raise HTTPException(status_code=403, detail="无权提交该澄清会话回答")
    if rules.check_sensitive(body.answer_text_redacted) != "pass":
        raise HTTPException(status_code=400, detail="回答必须先完成脱敏，不得提交个人信息、密钥或精确敏感数值")
    try:
        result = db.add_clarification_answer(
            session_id, body.turn_id, body.question_id, body.answer_text_redacted, user.username)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_log(user.username, "clarification_answer", target_path=session_id,
              detail={"turn_id": body.turn_id, "question_id": body.question_id})
    request.state.trace_detail = {"operation": "clarification_answer", "session_id": session_id, "turn_id": body.turn_id}
    return result


@router.get("/sessions", response_model=list[dict])
def list_sessions(user: auth.User = Depends(auth.require_roles("reviewer", "admin"))):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, conversation_id, status, round_count, max_rounds, created_by, created_at, updated_at "
            "FROM clarification_sessions ORDER BY updated_at DESC LIMIT 100").fetchall()
    keys = ("session_id", "conversation_id", "status", "round_count", "max_rounds", "created_by", "created_at", "updated_at")
    return [dict(zip(keys, row)) for row in rows]
