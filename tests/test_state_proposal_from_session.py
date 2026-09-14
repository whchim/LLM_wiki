"""「澄清 → 状态建议」最后一公里测试。

覆盖两层：
1. 确定性规则（纯函数，no_db）：状态机逐级推进、最低证据要求、冲突/证据不足的处理；
2. 服务与 API（真 PG）：生成建议 → 出现在「客户状态」待确认列表 → 负责人确认 → 当前状态更新。
"""
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

from api.main import app  # noqa: E402

import sales_state_rules  # noqa: E402

CONTENT = "客户确认正在评估方案，销售将在下周跟进预算反馈。"


def _claim(value: str, ctype: str = "customer_commitment", quote: str = "客户确认正在评估方案",
           source: str = "initial_note") -> dict:
    return {"id": "claim-1", "type": ctype, "attribution": "customer_quote", "certainty": "explicit",
            "value": value, "evidence": [{"source": source, "quote": quote}]}


def _infer(claims, current_state=None, content=CONTENT, answers=None) -> dict:
    return sales_state_rules.infer_proposal(
        claims=claims, current_state=current_state, content_redacted=content,
        answer_texts=answers or {})


# ---------- 1. 确定性规则（纯函数） ----------

@pytest.mark.no_db
def test_first_proposal_is_new_lead_with_baseline_evidence():
    """没有任何事实时：只能建议 new_lead（状态机唯一入口），证据用纪要本体。"""
    result = _infer([])
    assert result["generated"] is True
    assert result["proposed_state"] == "new_lead"
    assert result["decision"] == "propose"
    assert result["evidence"], "必须有至少一条可定位证据"
    assert result["evidence"][0]["source"] == "initial_note"


@pytest.mark.no_db
def test_state_machine_never_jumps():
    """证据显示已到方案评估，但当前无状态 → 只能建议 new_lead，不跳跃。"""
    result = _infer([_claim("客户正在做技术评估")])
    assert result["proposed_state"] == "new_lead"


@pytest.mark.no_db
def test_progressive_step_implied_by_higher_evidence():
    """当前 new_lead + 证据支持方案评估 → 建议 contacted（被更高状态蕴含）。"""
    result = _infer([_claim("客户正在做技术评估")], current_state="new_lead")
    assert result["proposed_state"] == "contacted"
    assert result["decision"] == "propose"


@pytest.mark.no_db
def test_missing_evidence_marks_needs_review():
    """证据不足以支撑下一步 → needs_review + 低置信 + 告知缺什么（不猜测状态）。"""
    result = _infer([_claim("客户表示下周再聊", ctype="next_step")], current_state="contacted")
    assert result["proposed_state"] == "need_confirmed"
    assert result["decision"] == "needs_review"
    assert "missing_strong_evidence" in result["risk_flags"]
    assert "low_confidence" in result["risk_flags"]
    assert result["missing"] == [sales_state_rules.MISSING_HINT["need_confirmed"]]


@pytest.mark.no_db
def test_conflicting_signals_go_to_human():
    """同一事实同时含推进与暂停信号 → 交人工，不自动选边。"""
    result = _infer([_claim("客户考虑暂停方案评估")], current_state="contacted")
    assert result["decision"] == "needs_review"
    assert "conflicting_signals" in result["risk_flags"]
    assert result["conflicts"]


@pytest.mark.no_db
def test_won_requires_contract_keywords():
    result = _infer([_claim("双方已签署合同")], current_state="commercial_negotiation")
    assert result["proposed_state"] == "won"
    assert result["decision"] == "propose"


@pytest.mark.no_db
def test_rejection_keywords_target_lost_or_paused():
    result = _infer([_claim("客户明确拒绝继续推进", ctype="objection")], current_state="contacted")
    assert result["proposed_state"] == "lost_or_paused"


@pytest.mark.no_db
def test_evidence_offsets_are_relocatable():
    """证据偏移必须能在来源文本中精确命中（不允许编造偏移）。"""
    result = _infer([_claim("客户正在做技术评估")], current_state="new_lead")
    for ref in result["evidence"]:
        text = CONTENT if ref["source"] == "initial_note" else ""
        assert text[ref["start"]:ref["end"]] == ref["quote"]


@pytest.mark.no_db
def test_answer_sourced_evidence_is_kept():
    """追加回答作为证据来源时，偏移相对该回答文本（澄清流程的真实场景）。"""
    answer = "客户说预算已经批下来了"
    result = _infer([_claim("客户正在做技术评估", quote="预算已经批下来了", source="question-1")],
                    current_state="new_lead", answers={"question-1": answer})
    assert any(ref["source"] == "question-1" and ref["quote"] == "预算已经批下来了"
               for ref in result["evidence"])


# ---------- 2. 服务与 API（真 PG） ----------

@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def admin_headers(client):
    response = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _ready_output() -> dict:
    quote = "客户确认正在评估方案"
    return {
        "schema_version": "clarification.v1",
        "claims": [{"id": "claim-1", "type": "next_step", "attribution": "customer_quote",
                    "certainty": "explicit", "value": "客户正在做方案评估",
                    "evidence": [{"source": "initial_note", "quote": quote}]}],
        "missing_facts": [], "questions": [], "stop_reason": "ready_for_proposal",
        "can_propose": True, "model_version": "test-model", "prompt_version": "v1",
    }


def _intake(client, headers, key: str, customer: str) -> dict:
    response = client.post("/clarifications/intake", headers=headers, json={
        "idempotency_key": key, "customer_id": customer, "content": CONTENT,
        "occurred_at": datetime.now(timezone.utc).isoformat(), "source_type": "meeting_note",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    return {"session_id": body["session"]["session_id"], "customer_id": customer,
            "conversation_id": body["conversation"]["conversation_id"]}


def _session_ready_for_proposal(client, headers, key: str, customer: str) -> dict:
    """建一条走到 ready_for_proposal 的会话。"""
    import db
    ctx = _intake(client, headers, key, customer)
    db.append_clarification_turn(ctx["session_id"], "ready_for_proposal", _ready_output(), 0)
    assert db.get_clarification_session(ctx["session_id"])["status"] == "ready_for_proposal"
    return ctx


def _session_needs_human_review(client, headers, key: str, customer: str) -> dict:
    """建一条已转人工的会话（模型/契约失败场景）。"""
    import db
    ctx = _intake(client, headers, key, customer)
    db.append_clarification_turn(ctx["session_id"], "human_review", {"error": ["模型或契约失败"]}, 0)
    assert db.get_clarification_session(ctx["session_id"])["status"] == "needs_human_review"
    return ctx


def test_generate_proposal_appears_in_customer_state_queue(client, admin_headers):
    """最后一公里打通：生成建议 → 出现在「客户状态」的待确认列表。"""
    ctx = _session_ready_for_proposal(client, admin_headers, "prop-001", "customer-prop-001")
    created = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal", headers=admin_headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["generated"] is True and body["reused"] is False
    assert body["proposed_state"] == "new_lead"          # 无当前状态 → 状态机唯一入口
    assert body["decision"] == "propose"

    pending = client.get("/customer-states/proposals/pending", headers=admin_headers)
    assert pending.status_code == 200
    ids = [item["proposal_id"] for item in pending.json()]
    assert body["proposal_id"] in ids
    assert any(item["customer_id"] == ctx["customer_id"] for item in pending.json())


def test_generate_proposal_is_idempotent(client, admin_headers):
    ctx = _session_ready_for_proposal(client, admin_headers, "prop-002", "customer-prop-002")
    first = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal", headers=admin_headers).json()
    second = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal", headers=admin_headers).json()
    assert second["reused"] is True
    assert second["proposal"]["proposal_id"] == first["proposal_id"]


def test_proposal_then_approve_updates_current_state(client, admin_headers):
    """端到端：生成建议 → 负责人确认 → 当前状态投影更新（事实由负责人写入）。"""
    ctx = _session_ready_for_proposal(client, admin_headers, "prop-003", "customer-prop-003")
    proposal_id = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                              headers=admin_headers).json()["proposal_id"]

    decision = client.post(f"/customer-states/proposals/{proposal_id}/decision", headers=admin_headers,
                           json={"decision": "approved", "reason": "负责人确认"})
    assert decision.status_code == 200, decision.text
    assert decision.json()["state"] == "new_lead"

    current = client.get(f"/customer-states/{ctx['customer_id']}", headers=admin_headers)
    assert current.status_code == 200
    assert current.json()["state"] == "new_lead"


def test_generate_proposal_rejects_open_session(client, admin_headers):
    """还在澄清中的会话不得提前固化结论。"""
    ctx = _intake(client, admin_headers, "prop-004", "customer-prop-004")
    created = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal", headers=admin_headers)
    assert created.status_code == 409
    detail = created.json()["detail"]
    assert "ready_for_proposal" in detail and "needs_human_review" in detail


def test_human_review_session_still_gives_owner_something_to_decide(client, admin_headers):
    """转人工的会话也必须产出可判断对象——否则"转人工"是死胡同。

    规则推断出的推进结论会被强制标为 needs_review（不冒充自动结论），
    负责人确认后仍然正常写入状态事件。
    """
    ctx = _session_needs_human_review(client, admin_headers, "prop-006", "customer-prop-006")
    created = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal", headers=admin_headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["generated"] is True
    assert body["decision"] == "needs_review"          # 不把"没判出来"包装成"建议推进"
    assert body["confidence"] <= 0.5
    assert "low_confidence" in body["risk_flags"]

    pending = client.get("/customer-states/proposals/pending", headers=admin_headers).json()
    item = next(p for p in pending if p["proposal_id"] == body["proposal_id"])
    assert "转人工" in (item["reasoning_summary"] or "")
    assert item["decision"] == "needs_review"

    decision = client.post(f"/customer-states/proposals/{item['proposal_id']}/decision",
                           headers=admin_headers,
                           json={"decision": "approved", "reason": "负责人判断：确认为新线索"})
    assert decision.status_code == 200, decision.text
    assert client.get(f"/customer-states/{ctx['customer_id']}",
                      headers=admin_headers).json()["state"] == "new_lead"


def test_session_detail_exposes_pending_proposal(client, admin_headers):
    """会话详情报出"已交负责人"，让工作台能显示交接状态（否则用户以为点了没反应）。"""
    ctx = _session_needs_human_review(client, admin_headers, "prop-007", "customer-prop-007")
    before = client.get(f"/clarifications/sessions/{ctx['session_id']}", headers=admin_headers).json()
    assert before["pending_proposal"] is None

    created = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                          headers=admin_headers).json()
    after = client.get(f"/clarifications/sessions/{ctx['session_id']}", headers=admin_headers).json()
    assert after["pending_proposal"]["proposal_id"] == created["proposal_id"]
    assert after["pending_proposal"]["proposed_state"] == created["proposed_state"]


def test_customers_overview_shows_result_after_decision(client, admin_headers):
    """确认后的"下文"必须可见：客户总览里能看到该客户的新阶段。"""
    ctx = _session_ready_for_proposal(client, admin_headers, "prop-008", "customer-prop-008")
    proposal_id = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                              headers=admin_headers).json()["proposal_id"]
    client.post(f"/customer-states/proposals/{proposal_id}/decision", headers=admin_headers,
                json={"decision": "approved", "reason": "负责人确认"})

    rows = client.get("/customer-states/customers", headers=admin_headers).json()
    row = next(r for r in rows if r["customer_id"] == ctx["customer_id"])
    assert row["state"] == "new_lead"
    assert row["pending_proposals"] == 0          # 已确认的建议不再计入待确认


def test_customers_overview_requires_reviewer_role(client, admin_headers):
    """普通用户不得查看客户总览（与确认动作同一权限边界）。"""
    import db
    from api import auth as auth_mod
    with db.get_conn() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,'user')",
                     ("plain-user", auth_mod.hash_password("plain123")))
    token = client.post("/auth/login", json={"username": "plain-user", "password": "plain123"}).json()["access_token"]
    denied = client.get("/customer-states/customers", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403


def test_generate_proposal_requires_access(client, admin_headers):
    """非本人且非审核角色 → 403（与澄清会话的其他操作同一权限边界）。"""
    import db
    from api import auth as auth_mod
    with db.get_conn() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,'user')",
                     ("other-sales", auth_mod.hash_password("other123")))
    ctx = _session_ready_for_proposal(client, admin_headers, "prop-005", "customer-prop-005")
    token = client.post("/auth/login", json={"username": "other-sales", "password": "other123"}).json()["access_token"]
    denied = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                         headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403
