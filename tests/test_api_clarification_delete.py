"""澄清会话归档（软删除）测试：**仅管理员**可删/可恢复，且不动客户事实。

契约要点：
- 归档只影响会话与其状态建议的可见性；
- **状态事件与当前阶段不受影响**（SA-02：事实只能追加更正/撤回/过期，不可删除）；
- 归档可逆（恢复后会话与建议回到列表）。
"""
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

from api.main import app  # noqa: E402

CONTENT = "客户确认正在评估方案，销售将在下周跟进预算反馈。"


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


def _session_with_proposal(client, headers, key: str, customer: str) -> dict:
    """建会话 → 生成并**确认**一条建议（这样客户有真实事实可验证归档不影响它）。"""
    import db
    response = client.post("/clarifications/intake", headers=headers, json={
        "idempotency_key": key, "customer_id": customer, "content": CONTENT,
        "occurred_at": datetime.now(timezone.utc).isoformat(), "source_type": "meeting_note",
    })
    assert response.status_code == 200, response.text
    session_id = response.json()["session"]["session_id"]
    db.append_clarification_turn(session_id, "ready_for_proposal", _ready_output(), 0)
    proposal_id = client.post(f"/clarifications/sessions/{session_id}/proposal",
                              headers=headers).json()["proposal_id"]
    decided = client.post(f"/customer-states/proposals/{proposal_id}/decision", headers=headers,
                          json={"decision": "approved", "reason": "负责人确认"})
    assert decided.status_code == 200, decided.text
    return {"session_id": session_id, "customer_id": customer, "proposal_id": proposal_id}


def _user_headers(client, username: str, role: str) -> dict:
    import db
    from api import auth as auth_mod
    with db.get_conn() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s)",
                     (username, auth_mod.hash_password("pw123456"), role))
    token = client.post("/auth/login", json={"username": username, "password": "pw123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_delete_requires_admin(client, admin_headers):
    """销售本人与审核者都不能删——删除是管理员专属。"""
    ctx = _session_with_proposal(client, admin_headers, "del-001", "customer-del-001")
    for username, role in (("del-sales", "user"), ("del-reviewer", "reviewer")):
        headers = _user_headers(client, username, role)
        denied = client.delete(f"/clarifications/sessions/{ctx['session_id']}", headers=headers)
        assert denied.status_code == 403, f"{role} 不应能删除"


def test_admin_delete_archives_session_and_proposals(client, admin_headers):
    ctx = _session_with_proposal(client, admin_headers, "del-002", "customer-del-002")

    deleted = client.delete(f"/clarifications/sessions/{ctx['session_id']}", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["already_deleted"] is False
    assert deleted.json()["proposals_archived"] == 1

    mine_ids = [s["session_id"] for s in client.get("/clarifications/mine", headers=admin_headers).json()]
    assert ctx["session_id"] not in mine_ids
    archived = client.get("/clarifications/mine?include_deleted=true", headers=admin_headers).json()
    row = next(s for s in archived if s["session_id"] == ctx["session_id"])
    assert row["deleted_at"] and row["deleted_by"] == "admin"

    assert client.get(f"/clarifications/sessions/{ctx['session_id']}",
                      headers=admin_headers).status_code == 404
    pending = client.get("/customer-states/proposals/pending", headers=admin_headers).json()
    assert ctx["proposal_id"] not in [p["proposal_id"] for p in pending]


def test_delete_does_not_touch_customer_facts(client, admin_headers):
    """归档会话不得影响客户事实：当前阶段与状态事件都必须还在。"""
    ctx = _session_with_proposal(client, admin_headers, "del-003", "customer-del-003")
    before = client.get(f"/customer-states/{ctx['customer_id']}", headers=admin_headers).json()
    events_before = client.get(f"/customer-states/{ctx['customer_id']}/events",
                               headers=admin_headers).json()

    client.delete(f"/clarifications/sessions/{ctx['session_id']}", headers=admin_headers)

    after = client.get(f"/customer-states/{ctx['customer_id']}", headers=admin_headers).json()
    events_after = client.get(f"/customer-states/{ctx['customer_id']}/events",
                              headers=admin_headers).json()
    assert after["state"] == before["state"] == "new_lead"
    assert len(events_after) == len(events_before) == 1

    rows = client.get("/customer-states/customers", headers=admin_headers).json()
    row = next(r for r in rows if r["customer_id"] == ctx["customer_id"])
    assert row["state"] == "new_lead"


def test_restore_brings_session_back(client, admin_headers):
    ctx = _session_with_proposal(client, admin_headers, "del-004", "customer-del-004")
    client.delete(f"/clarifications/sessions/{ctx['session_id']}", headers=admin_headers)

    restored = client.post(f"/clarifications/sessions/{ctx['session_id']}/restore", headers=admin_headers)
    assert restored.status_code == 200
    assert restored.json()["restored"] is True

    mine_ids = [s["session_id"] for s in client.get("/clarifications/mine", headers=admin_headers).json()]
    assert ctx["session_id"] in mine_ids
    assert client.get(f"/clarifications/sessions/{ctx['session_id']}",
                      headers=admin_headers).status_code == 200


def test_delete_is_idempotent(client, admin_headers):
    ctx = _session_with_proposal(client, admin_headers, "del-005", "customer-del-005")
    first = client.delete(f"/clarifications/sessions/{ctx['session_id']}", headers=admin_headers).json()
    second = client.delete(f"/clarifications/sessions/{ctx['session_id']}", headers=admin_headers).json()
    assert first["already_deleted"] is False
    assert second["already_deleted"] is True


def test_non_admin_cannot_list_archived(client, admin_headers):
    headers = _user_headers(client, "del-sales-2", "user")
    denied = client.get("/clarifications/mine?include_deleted=true", headers=headers)
    assert denied.status_code == 403
