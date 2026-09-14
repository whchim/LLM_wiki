"""销售事实澄清会话 API（阶段 4）。"""
from fastapi import APIRouter, Depends, HTTPException, Request

import db
import rules
from api import auth, trace as trace_mod
from api.audit import audit_log
from api.schemas import (ClarificationAnswerRequest, ClarificationResolveRequest,
                         ClarificationSessionRequest, SalesIntakeRequest)

import customer_state
import sales_preprocess
import clarification_service
import model_port
import sensitive_cipher
import state_proposal_service

router = APIRouter(prefix="/clarifications", tags=["clarification"])


@router.post("/intake", response_model=dict)
def intake(body: SalesIntakeRequest, request: Request,
           user: auth.User = Depends(auth.get_current_user),
           _trace: auth.User = Depends(trace_mod.trace("sales_intake"))):
    """销售提交一次脱敏纪要；只创建证据和澄清会话，不调用模型或改状态。

    **内容级幂等**：同一客户 + 完全相同正文（sha256 指纹）的历史洽谈会直接复用，
    响应带 `duplicate`（原因/原会话/原提交时间）——前端据此提示"这份纪要提交过"，
    不再堆出多条看起来一模一样的会话；确实要再建一次洽谈时传 `force_new=true`。
    """
    # 别名解析先于门禁：别名只是"选择入口"，解析出的代号仍要过 customer_id 的格式与敏感检查
    customer_id = body.customer_id.strip()
    resolved_from_alias = None
    if body.customer_alias and body.customer_alias.strip():
        try:
            resolved_from_alias = db.resolve_customer_alias(body.customer_alias)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail=str(exc).strip("'\"")) from exc
        if resolved_from_alias is None:
            raise HTTPException(
                status_code=400,
                detail=f"别名为「{body.customer_alias.strip()}」的客户尚未登记；"
                       f"请先在「客户别名」中建立绑定，或直接填写 customer_id")
        customer_id = resolved_from_alias

    # 受控加密器：把正文里的精确金额/折扣/数量加密成密文，正文只留 [AMOUNT_REF:nv-xxx]。
    # 未配置密钥时 encrypt_numeric 传 None —— 门禁会拒绝该纪要进入 Agent（不静默降级为明文）。
    cipher = sensitive_cipher.SensitiveNumericCipher() if sensitive_cipher.is_available() else None
    prepared = sales_preprocess.preprocess_sales_input({
        "idempotency_key": body.idempotency_key,
        "customer_id": customer_id,
        "content": body.content,
        "occurred_at": body.occurred_at,
        "submitted_by": user.username,
        "source_type": body.source_type,
        "source_ref": body.source_ref,
        "language": "zh-CN",
    }, encrypt_numeric=cipher.encrypt if cipher else None)
    if not prepared["accepted"]:
        raise HTTPException(status_code=400, detail={"message": "纪要未通过提交门禁", "errors": prepared["errors"],
                                                       "risk_flags": prepared["risk_flags"]})
    normalized = prepared["normalized"]
    try:
        duplicate = None
        conversation = db.get_conversation_by_idempotency_key(normalized["idempotency_key"])
        if conversation is not None:
            if conversation["customer_id"] != normalized["customer_id"]:
                raise HTTPException(status_code=409, detail="idempotency_key 已绑定其他客户，拒绝复用")
            if user.role not in {"reviewer", "admin"} and not db.can_access_conversation(conversation["conversation_id"], user.username):
                raise HTTPException(status_code=403, detail="无权重复提交该销售纪要")
        else:
            # 内容级幂等：同一份纪要重复提交（前端每次读文件都会换幂等键）不应堆出多条会话。
            # 只复用自己提交的（或审核角色可见的）记录，避免"内容相同"变成越权读取他人会话；
            # 已归档的记录不复用——否则会把人引回已归档会话。`force_new` 显式要求新建时跳过。
            if not body.force_new:
                prior = db.find_duplicate_intake(normalized["customer_id"], normalized["content_hash"])
                if (prior is not None and not prior["archived"]
                        and (prior["submitted_by"] == user.username or user.role in {"reviewer", "admin"})):
                    conversation = db.get_conversation_by_idempotency_key(prior["idempotency_key"])
                    duplicate = {"reason": "same_content", "conversation_id": prior["conversation_id"],
                                 "session_id": prior["session_id"], "source_ref": prior["source_ref"],
                                 "submitted_at": prior["submitted_at"]}
            if conversation is None:
                conversation = customer_state.create_conversation(
                    customer_id=normalized["customer_id"], idempotency_key=normalized["idempotency_key"],
                    source_type=normalized["source_type"], occurred_at=normalized["occurred_at"],
                    submitted_by=user.username, source_ref=normalized.get("source_ref"), owner_user_id=user.username)
        existing_session = db.get_clarification_session_for_conversation(conversation["conversation_id"])
        if existing_session is not None:
            evidence = db.latest_evidence(conversation["conversation_id"])
            audit_log(user.username, "sales_intake", target_path=existing_session["session_id"],
                      detail={"idempotent_replay": duplicate is None,
                              "duplicate_reason": (duplicate or {}).get("reason"),
                              "conversation_id": conversation["conversation_id"]})
            return {"conversation": conversation, "evidence": evidence, "session": existing_session,
                    "duplicate": duplicate,
                    "gate": {"risk_flags": prepared["risk_flags"], "numeric_ref_count": 0,
                             "idempotent_replay": duplicate is None}}
        evidence = customer_state.add_evidence(
            conversation["conversation_id"], normalized["content_redacted"], normalized.get("source_ref"),
            content_hash=normalized["content_hash"])   # 数值加密前的指纹：重复提交判据（见 SA-01 §9.1）
        # 密文与粗区间落受限表：Agent/检索/日志只见到占位符与区间，精确值需授权才可解密
        for ref in prepared["numeric_refs"]:
            if not ref.get("ciphertext"):
                continue
            customer_state.add_sensitive_numeric(
                evidence["evidence_id"], ref["field_type"], ref["ciphertext"],
                cipher.current_version, comparison_bucket=ref.get("comparison_bucket"))
        session = db.create_clarification_session(conversation["conversation_id"], user.username)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_log(user.username, "sales_intake", target_path=session["session_id"],
              detail={"conversation_id": conversation["conversation_id"], "evidence_id": evidence["evidence_id"],
                      "numeric_ref_count": len(prepared["numeric_refs"])})
    request.state.trace_detail = {"operation": "sales_intake", "session_id": session["session_id"]}
    return {"conversation": conversation, "evidence": evidence, "session": session,
            "duplicate": None,          # 正常新建时为 None；内容重复时带原因（见上面的分支）
            "alias": body.customer_alias.strip() if body.customer_alias and body.customer_alias.strip() else None,
            "resolved_customer_id": resolved_from_alias,
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
def get_session(session_id: str, advance: bool = False,
                user: auth.User = Depends(auth.get_current_user)):
    """读取澄清会话。

    advance=true 且会话为 open 时，先推进一轮（调模型 → 契约校验 → 落轮次）再返回，
    使销售工作台无需额外调用即可看到首轮追问。模型/契约失败不会让请求失败：
    结果落为 human_review 轮次并在响应中体现，避免前端因 5xx 反复重试造成重复计费。
    """
    if not _allowed(user, session_id):
        raise HTTPException(status_code=403, detail="无权访问该澄清会话")
    if advance:
        _advance_quietly(session_id, user.username)
    result = db.get_clarification_session(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="澄清会话不存在")
    # 已交负责人的标记：让工作台能显示"建议已生成、等确认"，而不是看起来毫无变化
    result["pending_proposal"] = state_proposal_service.pending_proposal_for_conversation(
        result["conversation_id"])
    return result


def _advance_quietly(session_id: str, username: str) -> dict | None:
    """推进一轮；任何异常都吞掉并返回 None（失败原因已落 human_review 轮次，前端可读）。"""
    port = model_port.default_port()
    if port is None:
        return None
    try:
        outcome = clarification_service.advance_session(session_id, port)
    except Exception as exc:  # 模型不可用 / 会话已关闭 / 上下文缺失
        audit_log(username, "clarification_advance", target_path=session_id,
                  detail={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return None
    if outcome.get("advanced"):
        audit_log(username, "clarification_advance", target_path=session_id,
                  detail={"status": outcome.get("status"), "claims": outcome.get("claims"),
                          "questions": len(outcome.get("questions") or []),
                          "tokens": outcome.get("audit", {}).get("output_tokens")})
    return outcome


@router.post("/sessions/{session_id}/advance", response_model=dict)
def advance(session_id: str, user: auth.User = Depends(auth.get_current_user)):
    """显式推进一轮澄清（与 GET ?advance=true 等价，便于前端在提交回答后主动触发）。"""
    if not _allowed(user, session_id):
        raise HTTPException(status_code=403, detail="无权推进该澄清会话")
    if not model_port.is_available():
        raise HTTPException(status_code=503, detail="未配置模型（MODEL_API_KEY / DASHSCOPE_API_KEY），无法推进澄清")
    outcome = _advance_quietly(session_id, user.username)
    if outcome is None:
        raise HTTPException(status_code=502, detail="模型或契约失败，已转人工；详情见会话轮次")
    return outcome


@router.post("/sessions/{session_id}/proposal", response_model=dict)
def create_state_proposal(session_id: str, request: Request, mode: str = "auto",
                          user: auth.User = Depends(auth.get_current_user),
                          _trace: auth.User = Depends(trace_mod.trace("clarification_proposal"))):
    """把澄清结论转成**待确认状态建议**（建议不修改客户状态）。

    `mode`：auto（默认——规则优先，规则判不出（证据不足/信号冲突）时才请 LLM 复核）
    / rules（强制规则）/ llm（强制模型）。模型不可用或未过契约校验时 auto 回退规则，
    并在响应里标注 `used` 与 `llm_error`（可观测）。
    边界：只创建建议——客户状态仍只能由负责人在「客户状态」确认后经
    state_decisions → state_events 写入。同一洽谈已有 pending 建议则复用（幂等）。
    """
    if not _allowed(user, session_id):
        raise HTTPException(status_code=403, detail="无权为该澄清会话生成状态建议")
    try:
        result = state_proposal_service.generate_proposal_for_session(session_id, mode=mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    proposal = result.get("proposal") or {}
    audit_log(user.username, "clarification_proposal", target_path=session_id,
              detail={"generated": result.get("generated"), "reused": result.get("reused"),
                      "mode": mode, "used": result.get("used"),
                      "proposed_state": result.get("proposed_state") or proposal.get("proposed_state")})
    request.state.trace_detail = {"operation": "clarification_proposal", "session_id": session_id,
                                 "generated": result.get("generated"), "reused": result.get("reused"),
                                 "mode": mode, "used": result.get("used")}
    return result


@router.delete("/sessions/{session_id}", response_model=dict)
def delete_session(session_id: str, request: Request,
                   user: auth.User = Depends(auth.require_roles("admin")),
                   _trace: auth.User = Depends(trace_mod.trace("clarification_session_delete"))):
    """归档澄清会话（**仅管理员**）：软删除会话与其产生的状态建议。

    边界：**不删状态事件/决策**——客户事实只能追加更正/撤回/过期（SA-02）；归档可恢复。
    """
    try:
        result = db.soft_delete_clarification_session(session_id, user.username)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_log(user.username, "clarification_session_delete", target_path=session_id,
              detail={"already_deleted": result["already_deleted"],
                      "proposals_archived": result["proposals_archived"]})
    request.state.trace_detail = {"operation": "clarification_session_delete", "session_id": session_id}
    return result


@router.post("/sessions/{session_id}/restore", response_model=dict)
def restore_session(session_id: str, request: Request,
                    user: auth.User = Depends(auth.require_roles("admin")),
                    _trace: auth.User = Depends(trace_mod.trace("clarification_session_restore"))):
    """恢复已归档的澄清会话与建议（**仅管理员**）。"""
    try:
        result = db.restore_clarification_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    audit_log(user.username, "clarification_session_restore", target_path=session_id,
              detail={"restored": result.get("restored")})
    request.state.trace_detail = {"operation": "clarification_session_restore", "session_id": session_id}
    return result


@router.get("/mine", response_model=list[dict])
def mine(include_deleted: bool = False,
         user: auth.User = Depends(auth.get_current_user)):
    """我创建的澄清会话；archived（已归档）默认不列出，管理员可显式包含。"""
    if include_deleted and user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可查看已归档会话")
    return db.list_user_clarification_sessions(user.username, include_deleted=include_deleted)


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
    # 回答后立即推进下一轮：销售下一次打开会话就能看到新追问（或转人工）
    result["advance"] = _advance_quietly(session_id, user.username)
    return result


@router.post("/sessions/{session_id}/resolve", response_model=dict)
def resolve(session_id: str, body: ClarificationResolveRequest, request: Request,
            user: auth.User = Depends(auth.require_roles("reviewer", "admin")),
            _trace: auth.User = Depends(trace_mod.trace("clarification_resolve"))):
    """人工处置转人工的会话（闭环）：关闭（必填原因）或补充事实后重开继续。

    边界（SA-10/SA-11）：轮次上限由产品约束（max_rounds ≤ 2），人工不加轮次——
    轮次已用尽只能关闭并改走状态建议流程。人工处置**只动会话**，客户状态仍只能经
    状态建议 → 负责人确认 → state_events 写入。
    """
    if body.decision not in {"closed", "reopened"}:
        raise HTTPException(status_code=400, detail="decision 只能是 closed 或 reopened")
    if body.decision == "closed" and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="关闭会话必须填写原因")
    if body.answer_text_redacted and rules.check_sensitive(body.answer_text_redacted) != "pass":
        raise HTTPException(status_code=400, detail="补充事实必须先完成脱敏，不得提交个人信息、密钥或精确敏感数值")
    try:
        result = clarification_service.resolve_session(
            session_id, body.decision, user.username,
            answer_text_redacted=body.answer_text_redacted, question_id=body.question_id,
            note=body.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_log(user.username, "clarification_resolve", target_path=session_id,
              detail={"decision": body.decision, "reason": (body.reason or "")[:200]})
    request.state.trace_detail = {"operation": "clarification_resolve", "session_id": session_id,
                                  "decision": body.decision}
    return result


@router.get("/sessions", response_model=list[dict])
def list_sessions(user: auth.User = Depends(auth.require_roles("reviewer", "admin"))):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, conversation_id, status, round_count, max_rounds, created_by, created_at, updated_at "
            "FROM clarification_sessions WHERE deleted_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 100").fetchall()
    keys = ("session_id", "conversation_id", "status", "round_count", "max_rounds", "created_by", "created_at", "updated_at")
    return [dict(zip(keys, row)) for row in rows]
