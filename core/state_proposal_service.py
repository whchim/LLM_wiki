"""把澄清结论变成待确认状态建议——补齐"澄清 → 状态建议"这最后一公里。

链路：
    澄清会话 ready_for_proposal
        → 本模块（确定性规则，不调模型）
        → state_proposals(status='pending')
        → 负责人在「客户状态」确认/修改/驳回
        → state_decisions → state_events（事实唯一写入口）→ current_states

纪律（与 SA-01/SA-02 一致）：
- 只创建**建议**，绝不修改客户状态——`state_events` 仍只由负责人确认写入；
- 幂等：同一洽谈已有 pending 建议则复用，不重复创建（避免人工队列里堆重复项）；
- 允许从 `ready_for_proposal`（澄清完成）与 `needs_human_review`（转人工）两种状态生成：
  后者强制标 `needs_review`——**转人工的会话也必须给负责人一个可判断的对象**，
  否则"转人工"是死胡同（人工被叫来了却没有东西可决定）。
- `open`（仍在澄清中）拒绝生成，避免把未澄清完的结论固化成建议。
"""
from __future__ import annotations

from datetime import datetime

import db
import customer_state
import model_port
import sales_state_agent
import sales_state_rules
from clarification_service import load_system_prompt

# 可生成建议的会话状态：澄清完成 / 已转人工
ALLOWED_SESSION_STATUSES = {"ready_for_proposal", "needs_human_review"}
HUMAN_REVIEW_CONFIDENCE_CAP = 0.5
# 判定模式：auto=规则优先、判不出才请 LLM 复核；rules/llm 为强制单一路径
MODES = {"auto", "rules", "llm"}


def _parse_valid_until(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _infer_with_llm(*, customer_id: str, content_redacted: str, current_state: str | None,
                    claims: list[dict], answer_texts: dict[str, str]) -> tuple[dict | None, str | None]:
    """用状态 Agent 判定一次；返回 (推断结果, 失败原因)。不可用/失败时由调用方回退规则。"""
    port = model_port.default_port()
    if port is None:
        return None, "未配置模型（MODEL_API_KEY / DASHSCOPE_API_KEY）"
    try:
        system_prompt = load_system_prompt(sales_state_agent.PROMPT_NAME)
    except (FileNotFoundError, ValueError) as exc:
        return None, f"缺少状态 Agent 提示词：{exc}"
    run = sales_state_agent.run_state_agent(
        port, customer_id=customer_id, content_redacted=content_redacted,
        current_state=current_state, claims=claims, answer_texts=answer_texts,
        allowed_sources=["initial_note", *answer_texts.keys()] if answer_texts else ["initial_note"],
        system_prompt=system_prompt)
    if not run.accepted or not isinstance(run.output, dict):
        detail = "；".join(run.errors) if run.errors else "模型未通过契约校验"
        return None, f"模型判定失败：{detail}"[:200]
    output = run.output
    evidence = [
        {"quote": item["quote"], "start": item["start"], "end": item["end"],
         "meaning": item["meaning"], "source": item.get("source") or "initial_note"}
        for item in output["evidence"] if isinstance(item, dict)
    ]
    return {
        "generated": True,
        "proposed_state": output["proposed_state"],
        "decision": output["decision"],
        "confidence": float(output["confidence"]),
        "evidence": evidence,
        "risk_flags": list(output.get("risk_flags") or []),
        "missing": [], "conflicts": [],
        "valid_until": _parse_valid_until(output.get("valid_until")),
        "reasoning_summary": output["reasoning_summary"],
        "next_action": output["next_action"],
        "model_version": output.get("model_version") or run.model_version or "unknown",
        "prompt_version": output.get("prompt_version") or sales_state_agent.PROMPT_VERSION,
        "engine": "llm",
    }, None


def _human_review_reason(session: dict) -> str:
    """转人工原因（取最后一轮的错误或结论），用于向负责人说明为什么没自动判定。"""
    for turn in reversed(session.get("turns") or []):
        output = turn.get("agent_output") if isinstance(turn.get("agent_output"), dict) else {}
        if turn.get("status") == "human_review":
            errors = output.get("error")
            if isinstance(errors, list) and errors:
                return str(errors[0])[:80]
            return "模型或契约失败"
        if turn.get("status") == "insufficient_evidence":
            return "证据不足"
    return "需人工判断"


def _as_human_review(inference: dict, session: dict) -> dict:
    """转人工会话：产出**仅供负责人判断**的建议，不把"没判出来"包装成"建议推进"。"""
    if not inference.get("generated"):
        return inference
    out = dict(inference)
    reason = _human_review_reason(session)
    out["decision"] = "needs_review"
    out["confidence"] = min(float(out.get("confidence") or 0), HUMAN_REVIEW_CONFIDENCE_CAP)
    flags = {f for f in (out.get("risk_flags") or []) if f != "low_confidence"}
    flags.add("low_confidence")
    out["risk_flags"] = sorted(flags)
    out["next_action"] = "请负责人在「客户状态」判断该客户的当前阶段（自动判定未得出结论）"[:500]
    prefix = f"澄清已转人工（{reason}）：自动判定未能得出结论，以下为规则推断，仅供负责人判断。"
    out["reasoning_summary"] = f"{prefix}{out.get('reasoning_summary', '')}"[:500]
    return out


def _session_claims(session: dict) -> list[dict]:
    """会话内所有轮次的事实声明（澄清 Agent 的 claims）。"""
    out: list[dict] = []
    for turn in session.get("turns") or []:
        output = turn.get("agent_output")
        if isinstance(output, dict) and isinstance(output.get("claims"), list):
            out.extend(c for c in output["claims"] if isinstance(c, dict))
    return out


def pending_proposal_for_conversation(conversation_id: str) -> dict | None:
    """该洽谈是否已有待确认建议（幂等判据）。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT proposal_id, proposed_state, decision, confidence, created_at "
            "FROM state_proposals WHERE conversation_id=%s AND status='pending' AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
    if row is None:
        return None
    keys = ("proposal_id", "proposed_state", "decision", "confidence", "created_at")
    return dict(zip(keys, row))


def generate_proposal_for_session(session_id: str, *, mode: str = "auto") -> dict:
    """从澄清会话的已确认事实生成状态建议。

    判定引擎：**规则优先**（`sales_state_rules`，零成本、可断言）。
    `auto` 下仅当规则给不出 `propose`（证据不足 / 信号冲突——正是关键词覆盖不到的模糊情形）
    才调用状态 Agent（LLM）复核；`llm` 强制走模型；`rules` 强制走规则。
    模型不可用或未通过契约校验 → **回退规则结论**，并在返回里标注 `used` / `llm_error`（可观测）。

    返回：{"generated": bool, "reused": bool, "used": "rules|llm|...+human_review",
           "proposal_id"?, "proposed_state"?, "decision"?, "missing": [...], "reason"?}
    """
    if mode not in MODES:
        raise ValueError(f"mode 只能是 {'/'.join(sorted(MODES))}")
    session = db.get_clarification_session(session_id)
    if session is None:
        raise KeyError("澄清会话不存在")
    status = session.get("status")
    if status not in ALLOWED_SESSION_STATUSES:
        raise ValueError(
            f"会话状态为 {status}，仅在 ready_for_proposal（澄清完成）或 "
            f"needs_human_review（已转人工）时可生成状态建议")

    context = db.clarification_context(session_id)
    conversation_id = context["conversation_id"]
    claims = _session_claims(session)
    answer_texts = {a["question_id"]: a.get("answer_text_redacted", "")
                    for a in session.get("answers") or []
                    if isinstance(a, dict) and a.get("question_id")}
    content_redacted = context.get("content_redacted") or ""
    current_state = context.get("current_state")

    inference = sales_state_rules.infer_proposal(
        claims=claims, current_state=current_state,
        content_redacted=content_redacted, answer_texts=answer_texts)
    used = "rules"
    llm_error: str | None = None
    # 规则已能给出推进结论时不调模型（省成本）；只有模糊情形才请 LLM 复核
    if mode != "rules" and (mode == "llm" or inference.get("decision") != "propose"):
        llm_inference, llm_error = _infer_with_llm(
            customer_id=context.get("customer_id", ""), content_redacted=content_redacted,
            current_state=current_state, claims=claims, answer_texts=answer_texts)
        if llm_inference is not None:
            inference, used = llm_inference, "llm"
        elif mode == "llm":
            raise ValueError(llm_error or "模型判定失败，无法使用 llm 模式")

    if status == "needs_human_review":
        inference = _as_human_review(inference, session)
        used = f"{used}+human_review"
    missing = inference.get("missing") or []

    existing = pending_proposal_for_conversation(conversation_id)
    if existing is not None:
        return {"generated": True, "reused": True, "proposal": existing, "missing": missing,
                "used": used, "llm_error": llm_error,
                "reason": "该洽谈已有待确认建议，未重复创建"}
    if not inference.get("generated"):
        return {"generated": False, "reused": False, "missing": missing, "used": used,
                "llm_error": llm_error, "reason": inference.get("reason") or "无法生成建议"}

    proposal_id = customer_state.create_proposal(
        conversation_id=conversation_id,
        proposed_state=inference["proposed_state"],
        confidence=float(inference["confidence"]),
        evidence_refs=inference["evidence"],
        current_state=current_state,
        decision=inference["decision"],
        reasoning_summary=inference["reasoning_summary"],
        next_action=inference["next_action"],
        valid_until=inference.get("valid_until"),
        risk_flags=inference["risk_flags"],
        model_version=inference["model_version"],
        prompt_version=inference["prompt_version"],
    )
    return {"generated": True, "reused": False, "proposal_id": proposal_id,
            "proposed_state": inference["proposed_state"], "decision": inference["decision"],
            "confidence": inference["confidence"], "risk_flags": inference["risk_flags"],
            "missing": missing, "evidence_count": len(inference["evidence"]),
            "used": used, "llm_error": llm_error}
