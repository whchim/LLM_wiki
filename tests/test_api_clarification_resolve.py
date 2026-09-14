"""澄清会话人工处置（闭环）测试：转人工后能被关闭或补充事实后重开。

对应两个真实转人工场景：
- 模型/契约失败（轮次预算未用尽）→ 可 reopened 重试，也可 closed；
- 轮次耗尽仍有缺口 → 只能 closed（max_rounds ≤ 2 是产品约束，人工不加轮次）。
"""
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

from api.main import app  # noqa: E402

pytestmark = pytest.mark.usefixtures("_env")


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def admin_headers(client):
    response = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _intake(client, headers, key="resolve-001", customer="customer-resolve-001") -> dict:
    response = client.post("/clarifications/intake", headers=headers, json={
        "idempotency_key": key,
        "customer_id": customer,
        "content": "客户确认正在评估方案，销售将在下周跟进预算反馈。",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source_type": "meeting_note",
    })
    assert response.status_code == 200, response.text
    return response.json()["session"]


def _question_output() -> dict:
    return {
        "schema_version": "clarification.v1", "claims": [],
        "missing_facts": [{"id": "missing-1", "type": "next_step", "priority": "high",
                           "why_needed": "判断是否进入方案评估", "impact_states": ["solution_eval"]}],
        "questions": [{"id": "question-1", "missing_fact_id": "missing-1",
                       "question": "下一步是否已约定？", "answer_type": "yes_no"}],
        "stop_reason": "needs_clarification", "can_propose": False,
        "model_version": "test-model", "prompt_version": "sales-clarification-v1",
    }


def _fail_to_human_review(session_id: str) -> None:
    """构造"模型/契约失败"现场：轮次 1 落 human_review，会话转人工，预算尚有 1 轮。"""
    import db
    db.append_clarification_turn(session_id, "human_review", {"error": ["模型或契约失败"]}, 0)
    assert db.get_clarification_session(session_id)["status"] == "needs_human_review"


def _exhaust_rounds(session_id: str) -> None:
    """构造"追问 + 收尾判定均已完成"现场：round_count > max_rounds，会话转人工。"""
    import db
    db.append_clarification_turn(session_id, "needs_clarification", _question_output(), 1)
    db.append_clarification_turn(session_id, "needs_clarification", _question_output(), 1)
    db.append_clarification_turn(session_id, "insufficient_evidence",
                                 {"stop_reason": "insufficient_evidence"}, 0)
    session = db.get_clarification_session(session_id)
    assert session["status"] == "needs_human_review"
    assert session["round_count"] > session["max_rounds"]


def test_closed_cancels_session_and_records_reason(client, admin_headers):
    session = _intake(client, admin_headers)
    _fail_to_human_review(session["session_id"])

    response = client.post(f"/clarifications/sessions/{session['session_id']}/resolve",
                           headers=admin_headers, json={"decision": "closed", "reason": "事实不足，转线下处理"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["status"] == "cancelled"
    assert body["session"]["resolution_note"] == "事实不足，转线下处理"
    assert body["session"]["resolved_by"] == "admin"
    assert body["session"]["resolved_at"]

    # 关闭后不可再作答（终态，不再卡在"看得见但动不了"）
    answered = client.post(f"/clarifications/sessions/{session['session_id']}/answers",
                           headers=admin_headers,
                           json={"turn_id": "turn-x", "question_id": "question-1", "answer_text_redacted": "是"})
    assert answered.status_code == 409


def test_closed_requires_reason(client, admin_headers):
    session = _intake(client, admin_headers, key="resolve-002", customer="customer-resolve-002")
    _fail_to_human_review(session["session_id"])
    response = client.post(f"/clarifications/sessions/{session['session_id']}/resolve",
                           headers=admin_headers, json={"decision": "closed"})
    assert response.status_code == 400
    assert "原因" in response.json()["detail"]


def test_reopened_when_budget_remains(client, admin_headers):
    """失败转人工且预算未用尽 → 可重开重试。"""
    session = _intake(client, admin_headers, key="resolve-003", customer="customer-resolve-003")
    _fail_to_human_review(session["session_id"])
    response = client.post(f"/clarifications/sessions/{session['session_id']}/resolve",
                           headers=admin_headers, json={"decision": "reopened", "reason": "瞬时模型故障，人工确认后重试"})
    assert response.status_code == 200, response.text
    assert response.json()["session"]["status"] == "open"


def test_reopened_rejected_when_rounds_exhausted(client, admin_headers):
    """追问与收尾判定均已完成（round_count > max_rounds）→ 人工不得加轮次，只能关闭。"""
    session = _intake(client, admin_headers, key="resolve-004", customer="customer-resolve-004")
    _exhaust_rounds(session["session_id"])
    response = client.post(f"/clarifications/sessions/{session['session_id']}/resolve",
                           headers=admin_headers, json={"decision": "reopened"})
    assert response.status_code == 409
    assert "轮次已用尽" in response.json()["detail"]
    closed = client.post(f"/clarifications/sessions/{session['session_id']}/resolve",
                         headers=admin_headers, json={"decision": "closed", "reason": "两轮仍不足，转人工判断"})
    assert closed.status_code == 200 and closed.json()["session"]["status"] == "cancelled"


def test_legacy_session_with_unanswered_last_round_can_be_reopened(client, admin_headers):
    """历史数据可恢复：旧逻辑把"最后一轮追问"连带关闭过，重开后必须能继续回答。"""
    import db
    session = _intake(client, admin_headers, key="resolve-008", customer="customer-resolve-008")
    sid = session["session_id"]
    db.append_clarification_turn(sid, "needs_clarification", _question_output(), 1)
    last_turn = db.append_clarification_turn(sid, "needs_clarification", _question_output(), 1)
    # 复现旧逻辑留下的状态：轮次已到上限、会话被关闭、但该轮问题从未被回答
    with db.get_conn() as conn:
        conn.execute("UPDATE clarification_sessions SET status='needs_human_review' WHERE session_id=%s", (sid,))

    reopened = client.post(f"/clarifications/sessions/{sid}/resolve", headers=admin_headers,
                           json={"decision": "reopened", "reason": "恢复历史会话以补答最后一轮"})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["session"]["status"] == "open"

    answered = client.post(f"/clarifications/sessions/{sid}/answers", headers=admin_headers,
                           json={"turn_id": last_turn["turn_id"], "question_id": "question-1",
                                 "answer_text_redacted": "是，下周二演示"})
    assert answered.status_code == 200, answered.text


def test_resolve_requires_reviewer_role(client, admin_headers):
    """普通销售不得处置会话（与状态确认保持同一权限边界）。"""
    import db
    from api import auth as auth_mod
    with db.get_conn() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,'user')",
                     ("sales-resolve", auth_mod.hash_password("sales123")))
    session = _intake(client, admin_headers, key="resolve-005", customer="customer-resolve-005")
    token = client.post("/auth/login", json={"username": "sales-resolve", "password": "sales123"}).json()["access_token"]
    response = client.post(f"/clarifications/sessions/{session['session_id']}/resolve",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"decision": "closed", "reason": "尝试越权"})
    assert response.status_code == 403


def test_reopened_rejects_undesensitized_answer(client, admin_headers):
    """补充事实必须已脱敏（与回答问题同一道门禁）。"""
    session = _intake(client, admin_headers, key="resolve-006", customer="customer-resolve-006")
    _fail_to_human_review(session["session_id"])
    response = client.post(f"/clarifications/sessions/{session['session_id']}/resolve",
                           headers=admin_headers,
                           json={"decision": "reopened", "answer_text_redacted": "联系电话 13800138000"})
    assert response.status_code == 400
    assert "脱敏" in response.json()["detail"]


def test_reopened_rejects_answer_without_pending_question(client, admin_headers):
    """失败转人工时没有待答问题 → 补充事实无处可挂，应明确拒绝而不是静默丢弃。"""
    session = _intake(client, admin_headers, key="resolve-007", customer="customer-resolve-007")
    _fail_to_human_review(session["session_id"])
    response = client.post(f"/clarifications/sessions/{session['session_id']}/resolve",
                           headers=admin_headers,
                           json={"decision": "reopened", "answer_text_redacted": "客户已确认下周演示"})
    assert response.status_code == 409
    assert "没有待回答的问题" in response.json()["detail"]
