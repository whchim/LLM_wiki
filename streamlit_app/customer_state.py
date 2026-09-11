"""销售客户状态领域服务（阶段 2）。

客户状态采用追加事件模型：Agent 只能创建 proposal，只有负责人决定后才会
在同一数据库事务内写入 decision、state_event 和 current_states 投影。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

import db

STATES = {
    "new_lead", "contacted", "need_confirmed", "solution_eval",
    "commercial_negotiation", "won", "lost_or_paused", "expired",
}
SOURCE_TYPES = {"meeting_note", "transcript", "chat_summary"}
PROPOSAL_DECISIONS = {"propose", "needs_review", "reject"}
DECISION_TYPES = {"approved", "modified", "rejected"}

# 只允许符合销售生命周期的前进、暂停和恢复；过期由系统事件单独产生。
ALLOWED_TRANSITIONS = {
    None: {"new_lead"},
    "new_lead": {"contacted", "lost_or_paused"},
    "contacted": {"need_confirmed", "lost_or_paused"},
    "need_confirmed": {"solution_eval", "lost_or_paused"},
    "solution_eval": {"commercial_negotiation", "lost_or_paused"},
    "commercial_negotiation": {"won", "lost_or_paused"},
    "lost_or_paused": {"contacted", "need_confirmed"},
    "expired": {"contacted", "need_confirmed", "solution_eval"},
}


def validate_transition(current_state: str | None, proposed_state: str) -> None:
    """校验状态枚举和状态机边，供 API 与测试复用。"""
    if proposed_state not in STATES or proposed_state == "expired":
        raise ValueError(f"非法目标状态：{proposed_state}")
    if proposed_state not in ALLOWED_TRANSITIONS.get(current_state, set()):
        raise ValueError(f"不允许状态转移：{current_state or 'none'} -> {proposed_state}")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def create_conversation(customer_id: str, idempotency_key: str,
                        source_type: str, occurred_at: datetime,
                        submitted_by: str, source_ref: str | None = None,
                        display_name_redacted: str | None = None,
                        owner_user_id: str | None = None) -> dict:
    """创建客户和洽谈；相同幂等键返回已存在记录，不重复创建。"""
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"非法来源类型：{source_type}")
    if not idempotency_key.strip() or not customer_id.strip() or not submitted_by.strip():
        raise ValueError("customer_id、idempotency_key、submitted_by 不能为空")
    with db.get_conn() as conn:
        # 先确保客户主体存在；若后续发现幂等键冲突，事务会整体回滚，不会留下孤立客户。
        conn.execute(
            "INSERT INTO customers (customer_id, display_name_redacted, owner_user_id) "
            "VALUES (%s,%s,%s) ON CONFLICT (customer_id) DO UPDATE SET "
            "updated_at=now(), owner_user_id=COALESCE(EXCLUDED.owner_user_id, customers.owner_user_id)",
            (customer_id, display_name_redacted, owner_user_id))
        row = conn.execute(
            "SELECT conversation_id, customer_id, idempotency_key, source_type, occurred_at, "
            "submitted_by, source_ref, processing_status, created_at FROM conversations "
            "WHERE idempotency_key=%s",
            (idempotency_key,)).fetchone()
        if row is not None and row[1] != customer_id:
            # 幂等键代表同一条外部输入，跨客户复用通常意味着调用方数据串线，必须显式失败。
            raise ValueError("idempotency_key 已绑定其他客户，拒绝复用")
        if row is None:
            conversation_id = _id("conv")
            row = conn.execute(
                "INSERT INTO conversations "
                "(conversation_id, customer_id, idempotency_key, source_type, occurred_at, submitted_by, source_ref) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING conversation_id, customer_id, idempotency_key, source_type, occurred_at, "
                "submitted_by, source_ref, processing_status, created_at",
                (conversation_id, customer_id, idempotency_key, source_type, occurred_at,
                 submitted_by, source_ref)).fetchone()
        keys = ["conversation_id", "customer_id", "idempotency_key", "source_type",
                "occurred_at", "submitted_by", "source_ref", "processing_status", "created_at"]
        return dict(zip(keys, row))


def add_evidence(conversation_id: str, content_redacted: str,
                 source_ref: str | None = None) -> dict:
    """追加不可变脱敏证据，内容哈希用于回放和完整性检查。"""
    if not content_redacted.strip():
        raise ValueError("脱敏证据不能为空")
    evidence_id = _id("ev")
    content_hash = hashlib.sha256(content_redacted.encode("utf-8")).hexdigest()
    with db.get_conn() as conn:
        # 证据、建议和决定分开落库：Agent 可以写建议，但不能绕过负责人直接改变业务事实。
        conn.execute(
            "INSERT INTO evidence (evidence_id, conversation_id, content_redacted, content_hash, source_ref) "
            "VALUES (%s,%s,%s,%s,%s)",
            (evidence_id, conversation_id, content_redacted, content_hash, source_ref))
    return {"evidence_id": evidence_id, "conversation_id": conversation_id,
            "content_hash": content_hash}


def add_sensitive_numeric(evidence_id: str, field_type: str, ciphertext: str,
                          key_version: str, unit: str | None = None,
                          comparison_bucket: str | None = None,
                          access_policy: str = "owner_only") -> str:
    """写入精确敏感数值的受控密文，不接受明文参数。"""
    allowed = {"amount", "budget", "discount", "quote", "quantity", "other"}
    if field_type not in allowed or not ciphertext or not key_version:
        raise ValueError("field_type、ciphertext、key_version 不合法")
    numeric_id = _id("num")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO sensitive_numeric_values "
            "(numeric_value_id, evidence_id, field_type, ciphertext, key_version, unit, comparison_bucket, access_policy) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (numeric_id, evidence_id, field_type, ciphertext, key_version, unit,
             comparison_bucket, access_policy))
    return numeric_id


def create_proposal(conversation_id: str, proposed_state: str, confidence: float,
                    evidence_refs: list[dict], current_state: str | None = None,
                    decision: str = "propose", reasoning_summary: str | None = None,
                    next_action: str | None = None, valid_until: datetime | None = None,
                    risk_flags: list[str] | None = None, model_version: str | None = None,
                    prompt_version: str | None = None) -> str:
    """创建 Agent 建议；该操作不会改变客户当前状态。"""
    if decision not in PROPOSAL_DECISIONS:
        raise ValueError(f"非法建议决策：{decision}")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence 必须在 0 到 1 之间")
    validate_transition(current_state, proposed_state)
    if not evidence_refs:
        raise ValueError("状态建议至少需要一条证据引用")
    proposal_id = _id("proposal")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO state_proposals "
            "(proposal_id, conversation_id, current_state, proposed_state, decision, confidence, "
            "evidence_refs, reasoning_summary, next_action, valid_until, needs_human_confirmation, "
            "risk_flags, model_version, prompt_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,%s)",
            (proposal_id, conversation_id, current_state, proposed_state, decision, confidence,
             Jsonb(evidence_refs), reasoning_summary, next_action, valid_until,
             Jsonb(risk_flags or []), model_version, prompt_version))
    return proposal_id


def decide_proposal(proposal_id: str, decision: str, decided_by: str,
                    final_state: str | None = None, reason: str | None = None,
                    evidence_refs: list[dict] | None = None) -> dict:
    """负责人确认/修改/驳回建议，并在同一事务内更新状态投影。"""
    if decision not in DECISION_TYPES or not decided_by.strip():
        raise ValueError("非法决定类型或确认人为空")
    decision_id = _id("decision")
    event_id = _id("event")
    with db.get_conn() as conn:
        # 锁住建议和客户当前投影，避免并发确认同一建议或基于旧状态产生非法跳转。
        proposal = conn.execute(
            "SELECT proposal_id, conversation_id, current_state, proposed_state, evidence_refs, valid_until, status "
            "FROM state_proposals WHERE proposal_id=%s FOR UPDATE", (proposal_id,)).fetchone()
        if proposal is None:
            raise KeyError(f"建议不存在：{proposal_id}")
        if proposal[6] != "pending":
            raise ValueError(f"建议当前状态不允许处理：{proposal[6]}")
        conversation = conn.execute(
            "SELECT customer_id FROM conversations WHERE conversation_id=%s", (proposal[1],)).fetchone()
        if conversation is None:
            raise KeyError(f"洽谈不存在：{proposal[1]}")
        customer_id = conversation[0]
        conn.execute("SELECT customer_id FROM customers WHERE customer_id=%s FOR UPDATE", (customer_id,))
        current = conn.execute(
            "SELECT state, source_event_id FROM current_states WHERE customer_id=%s FOR UPDATE",
            (customer_id,)).fetchone()
        current_state = current[0] if current else proposal[2]

        if decision == "rejected":
            # 驳回只记录决定并关闭建议，不产生状态事件，当前事实保持不变。
            conn.execute(
                "INSERT INTO state_decisions (decision_id, proposal_id, decision, decided_by, reason) "
                "VALUES (%s,%s,%s,%s,%s)",
                (decision_id, proposal_id, decision, decided_by, reason))
            conn.execute("UPDATE state_proposals SET status='rejected' WHERE proposal_id=%s", (proposal_id,))
            return {"decision_id": decision_id, "proposal_id": proposal_id,
                    "decision": decision, "event_id": None}

        final_state = final_state or proposal[3]
        validate_transition(current_state, final_state)
        refs = evidence_refs if evidence_refs is not None else proposal[4]
        conn.execute(
            "INSERT INTO state_decisions (decision_id, proposal_id, decision, final_state, decided_by, reason) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (decision_id, proposal_id, decision, final_state, decided_by, reason))
        conn.execute(
            "INSERT INTO state_events (event_id, customer_id, decision_id, event_type, state, effective_at, "
            "valid_until, created_by, evidence_refs, reason) "
            "VALUES (%s,%s,%s,'state_confirmed',%s,now(),%s,%s,%s,%s)",
            (event_id, customer_id, decision_id, final_state, proposal[5], decided_by,
             Jsonb(refs), reason))
        # 事件和投影在同一事务中提交；任一步失败都会回滚，避免出现“有事件但查不到当前状态”。
        conn.execute("UPDATE state_proposals SET status=%s WHERE proposal_id=%s",
                     ("approved", proposal_id))
        conn.execute(
            "INSERT INTO current_states (customer_id, state, source_event_id, effective_at, valid_until, projection_version) "
            "VALUES (%s,%s,%s,now(),%s,1) ON CONFLICT (customer_id) DO UPDATE SET "
            "state=EXCLUDED.state, source_event_id=EXCLUDED.source_event_id, effective_at=EXCLUDED.effective_at, "
            "valid_until=EXCLUDED.valid_until, projection_version=current_states.projection_version+1, updated_at=now()",
            (customer_id, final_state, event_id, proposal[5]))
        return {"decision_id": decision_id, "proposal_id": proposal_id,
                "decision": decision, "event_id": event_id,
                "customer_id": customer_id, "state": final_state}


def expire_state(customer_id: str, at: datetime | None = None,
                 actor: str = "system") -> dict | None:
    """将已过期当前状态转为 expired 事件；重复调用幂等。"""
    at = at or datetime.now(timezone.utc)
    event_id = _id("event")
    with db.get_conn() as conn:
        # 过期是追加事件而不是覆盖原确认事件；重复调用通过当前状态检查保持幂等。
        conn.execute("SELECT customer_id FROM customers WHERE customer_id=%s FOR UPDATE", (customer_id,))
        current = conn.execute(
            "SELECT state, source_event_id, valid_until FROM current_states WHERE customer_id=%s FOR UPDATE",
            (customer_id,)).fetchone()
        if current is None or current[0] == "expired" or current[2] is None or current[2] > at:
            return None
        conn.execute(
            "INSERT INTO state_events (event_id, customer_id, event_type, state, effective_at, created_by, evidence_refs, reason) "
            "VALUES (%s,%s,'state_expired','expired',%s,%s,'[]'::jsonb,%s)",
            (event_id, customer_id, at, actor, f"expired source event {current[1]}"))
        conn.execute(
            "UPDATE current_states SET state='expired', source_event_id=%s, effective_at=%s, valid_until=NULL, "
            "projection_version=projection_version+1, updated_at=now() WHERE customer_id=%s",
            (event_id, at, customer_id))
        return {"event_id": event_id, "customer_id": customer_id, "state": "expired"}


def list_due_expirations(at: datetime | None = None, limit: int = 100) -> list[dict]:
    """列出需要过期处理的当前状态，不在查询时隐式修改业务事实。"""
    at = at or datetime.now(timezone.utc)
    limit = max(1, min(limit, 500))
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT cs.customer_id, c.display_name_redacted, cs.state, cs.source_event_id, cs.valid_until "
            "FROM current_states cs JOIN customers c ON c.customer_id=cs.customer_id "
            "WHERE cs.state <> 'expired' AND cs.valid_until IS NOT NULL AND cs.valid_until <= %s "
            "ORDER BY cs.valid_until ASC LIMIT %s", (at, limit)).fetchall()
    keys = ["customer_id", "display_name_redacted", "state", "source_event_id", "valid_until"]
    return [dict(zip(keys, row)) for row in rows]


def expire_due_states(at: datetime | None = None, actor: str = "system", limit: int = 100) -> list[dict]:
    """显式处理到期项；逐客户复用幂等过期写入，允许任务安全重试。"""
    at = at or datetime.now(timezone.utc)
    results = []
    for item in list_due_expirations(at, limit):
        expired = expire_state(item["customer_id"], at, actor)
        if expired is not None:
            results.append(expired)
    return results


def correct_current_state(customer_id: str, final_state: str, actor: str, reason: str,
                          evidence_refs: list[dict], valid_until: datetime | None = None) -> dict:
    """负责人基于新证据更正当前状态，保留被更正事件而不回写历史。"""
    if not actor.strip() or not reason.strip() or not evidence_refs:
        raise ValueError("更正人、原因和证据引用不能为空")
    event_id = _id("event")
    with db.get_conn() as conn:
        conn.execute("SELECT customer_id FROM customers WHERE customer_id=%s FOR UPDATE", (customer_id,))
        current = conn.execute(
            "SELECT state FROM current_states WHERE customer_id=%s FOR UPDATE", (customer_id,)).fetchone()
        if current is None:
            raise ValueError("客户没有可更正的当前状态")
        # 更正并非跳过销售流程：仍需符合当前生命周期边，过期状态可被新证据恢复。
        validate_transition(current[0], final_state)
        conn.execute(
            "INSERT INTO state_events (event_id, customer_id, event_type, state, effective_at, valid_until, "
            "created_by, evidence_refs, reason) VALUES (%s,%s,'state_corrected',%s,now(),%s,%s,%s,%s)",
            (event_id, customer_id, final_state, valid_until, actor, Jsonb(evidence_refs), reason))
        conn.execute(
            "UPDATE current_states SET state=%s, source_event_id=%s, effective_at=now(), valid_until=%s, "
            "projection_version=projection_version+1, updated_at=now() WHERE customer_id=%s",
            (final_state, event_id, valid_until, customer_id))
    return {"event_id": event_id, "customer_id": customer_id, "state": final_state}


def withdraw_current_state(customer_id: str, actor: str, reason: str) -> dict:
    """撤回当前状态，保留历史并将投影恢复到上一个确认状态或 expired。"""
    if not actor.strip() or not reason.strip():
        raise ValueError("撤回人和原因不能为空")
    event_id = _id("event")
    with db.get_conn() as conn:
        # 撤回只改变查询投影并追加撤回事件，历史确认、过期记录和证据始终保留以便审计回放。
        conn.execute("SELECT customer_id FROM customers WHERE customer_id=%s FOR UPDATE", (customer_id,))
        current = conn.execute(
            "SELECT state, source_event_id FROM current_states WHERE customer_id=%s FOR UPDATE",
            (customer_id,)).fetchone()
        if current is None:
            raise ValueError("客户没有可撤回的当前状态")
        previous = conn.execute(
            "SELECT state, event_id, effective_at, valid_until FROM state_events "
            "WHERE customer_id=%s AND event_id<>%s AND event_type IN ('state_confirmed','state_corrected') "
            "ORDER BY effective_at DESC, created_at DESC LIMIT 1",
            (customer_id, current[1])).fetchone()
        target_state = previous[0] if previous else "expired"
        conn.execute(
            "INSERT INTO state_events (event_id, customer_id, event_type, state, effective_at, valid_until, created_by, evidence_refs, reason) "
            "VALUES (%s,%s,'state_withdrawn',%s,now(),%s,%s,'[]'::jsonb,%s)",
            (event_id, customer_id, target_state, previous[3] if previous else None, actor, reason))
        conn.execute(
            "UPDATE current_states SET state=%s, source_event_id=%s, effective_at=now(), valid_until=%s, "
            "projection_version=projection_version+1, updated_at=now() WHERE customer_id=%s",
            (target_state, event_id, previous[3] if previous else None, customer_id))
        return {"event_id": event_id, "customer_id": customer_id, "state": target_state}


def get_current_state(customer_id: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT customer_id, state, source_event_id, effective_at, valid_until, projection_version, updated_at "
            "FROM current_states WHERE customer_id=%s", (customer_id,)).fetchone()
        if row is None:
            return None
        keys = ["customer_id", "state", "source_event_id", "effective_at", "valid_until",
                "projection_version", "updated_at"]
        return dict(zip(keys, row))


def list_pending_proposals(limit: int = 100) -> list[dict]:
    """负责人工作台使用的待确认建议列表，只返回脱敏证据和业务字段。"""
    limit = max(1, min(limit, 500))
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.proposal_id, p.conversation_id, c.customer_id, p.current_state, "
            "p.proposed_state, p.decision, p.confidence, p.evidence_refs, p.reasoning_summary, "
            "p.next_action, p.valid_until, p.needs_human_confirmation, p.risk_flags, "
            "p.model_version, p.prompt_version, p.created_at "
            "FROM state_proposals p JOIN conversations c ON c.conversation_id=p.conversation_id "
            "WHERE p.status='pending' ORDER BY p.created_at ASC LIMIT %s", (limit,)).fetchall()
        keys = ["proposal_id", "conversation_id", "customer_id", "current_state", "proposed_state",
                "decision", "confidence", "evidence_refs", "reasoning_summary", "next_action",
                "valid_until", "needs_human_confirmation", "risk_flags", "model_version",
                "prompt_version", "created_at"]
        return [dict(zip(keys, row)) for row in rows]


def get_current_state_with_customer(customer_id: str) -> dict | None:
    """当前状态查询的稳定接口；不存在客户或状态时返回 None。"""
    current = get_current_state(customer_id)
    if current is None:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT customer_id, display_name_redacted, owner_user_id FROM customers WHERE customer_id=%s",
            (customer_id,)).fetchone()
    if row is None:
        return None
    current.update({"display_name_redacted": row[1], "owner_user_id": row[2]})
    return current


def list_proposal_conflicts(limit: int = 100) -> list[dict]:
    """识别待审建议的竞争目标和过期基线，仅用于人工分流，不自动裁决。"""
    limit = max(1, min(limit, 500))
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.proposal_id, c.customer_id, p.current_state, p.proposed_state, cs.state "
            "FROM state_proposals p JOIN conversations c ON c.conversation_id=p.conversation_id "
            "LEFT JOIN current_states cs ON cs.customer_id=c.customer_id WHERE p.status='pending' "
            "ORDER BY p.created_at ASC LIMIT %s", (limit,)).fetchall()
    grouped: dict[str, list[tuple]] = {}
    for row in rows:
        grouped.setdefault(row[1], []).append(row)
    conflicts: list[dict] = []
    for customer_id, proposals in grouped.items():
        targets = {proposal[3] for proposal in proposals}
        if len(targets) > 1:
            conflicts.append({"customer_id": customer_id, "type": "competing_proposals",
                              "proposal_ids": [proposal[0] for proposal in proposals],
                              "detail": "同一客户存在不同目标状态的待审建议"})
        for proposal in proposals:
            if proposal[2] != proposal[4]:
                conflicts.append({"customer_id": customer_id, "type": "stale_base_state",
                                  "proposal_ids": [proposal[0]],
                                  "detail": f"建议基线={proposal[2]!r}，当前状态={proposal[4]!r}"})
    return conflicts

def list_state_events(customer_id: str, limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, 500))
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT event_id, customer_id, decision_id, event_type, state, effective_at, valid_until, "
            "created_by, evidence_refs, reason, created_at FROM state_events "
            "WHERE customer_id=%s ORDER BY effective_at DESC, created_at DESC LIMIT %s",
            (customer_id, limit)).fetchall()
        keys = ["event_id", "customer_id", "decision_id", "event_type", "state", "effective_at",
                "valid_until", "created_by", "evidence_refs", "reason", "created_at"]
        return [dict(zip(keys, row)) for row in rows]
