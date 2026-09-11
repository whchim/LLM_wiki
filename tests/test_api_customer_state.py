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
    r = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _seed_proposal():
    import customer_state

    conversation = customer_state.create_conversation(
        "cust-api", "api-idem-1", "meeting_note", datetime.now(timezone.utc), "sales-1"
    )
    proposal_id = customer_state.create_proposal(
        conversation["conversation_id"], "new_lead", 0.9, [{"evidence_id": "ev-1"}]
    )
    return proposal_id


def test_pending_proposals_require_reviewer_role(client):
    assert client.get("/customer-states/proposals/pending").status_code == 401


def test_decision_updates_current_state_and_is_audited(client, admin_headers):
    proposal_id = _seed_proposal()
    pending = client.get("/customer-states/proposals/pending", headers=admin_headers)
    assert pending.status_code == 200
    assert any(item["proposal_id"] == proposal_id for item in pending.json())

    response = client.post(
        f"/customer-states/proposals/{proposal_id}/decision",
        headers=admin_headers,
        json={"decision": "approved", "reason": "负责人确认"},
    )
    assert response.status_code == 200, response.text
    current = client.get("/customer-states/cust-api", headers=admin_headers)
    assert current.status_code == 200
    assert current.json()["state"] == "new_lead"
    events = client.get("/customer-states/cust-api/events", headers=admin_headers)
    assert events.status_code == 200 and events.json()[0]["event_type"] == "state_confirmed"

    import db
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='customer_state_decision'"
        ).fetchone()[0] >= 1


def test_withdraw_requires_reason_and_preserves_event(client, admin_headers):
    proposal_id = _seed_proposal()
    assert client.post(
        f"/customer-states/proposals/{proposal_id}/decision", headers=admin_headers,
        json={"decision": "approved"},
    ).status_code == 200
    assert client.post(
        "/customer-states/cust-api/withdraw", headers=admin_headers,
        json={"reason": ""},
    ).status_code == 400
    response = client.post(
        "/customer-states/cust-api/withdraw", headers=admin_headers,
        json={"reason": "证据被负责人撤回"},
    )
    assert response.status_code == 200
    events = client.get("/customer-states/cust-api/events", headers=admin_headers).json()
    assert [event["event_type"] for event in events[:2]] == ["state_withdrawn", "state_confirmed"]


def test_modified_decision_requires_target_state_and_reason(client, admin_headers):
    proposal_id = _seed_proposal()
    response = client.post(
        f"/customer-states/proposals/{proposal_id}/decision", headers=admin_headers,
        json={"decision": "modified"},
    )
    assert response.status_code == 400


def test_correction_requires_evidence_and_preserves_history(client, admin_headers):
    proposal_id = _seed_proposal()
    assert client.post(
        f"/customer-states/proposals/{proposal_id}/decision", headers=admin_headers,
        json={"decision": "approved"},
    ).status_code == 200
    response = client.post(
        "/customer-states/cust-api/correct", headers=admin_headers,
        json={"final_state": "contacted", "reason": "新证据确认已接触",
              "evidence_refs": [{"evidence_id": "ev-new"}]},
    )
    assert response.status_code == 200, response.text
    events = client.get("/customer-states/cust-api/events", headers=admin_headers).json()
    assert [event["event_type"] for event in events[:2]] == ["state_corrected", "state_confirmed"]


def test_expiration_listing_and_processing_are_idempotent(client, admin_headers):
    import customer_state
    from datetime import timedelta
    conversation = customer_state.create_conversation(
        "cust-expire", "api-idem-expire", "meeting_note", datetime.now(timezone.utc), "sales-1"
    )
    proposal_id = customer_state.create_proposal(
        conversation["conversation_id"], "new_lead", 0.9, [{"evidence_id": "ev-1"}],
        valid_until=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    assert client.post(
        f"/customer-states/proposals/{proposal_id}/decision", headers=admin_headers,
        json={"decision": "approved"},
    ).status_code == 200
    due = client.get("/customer-states/ops/due-expirations", headers=admin_headers)
    assert due.status_code == 200 and any(item["customer_id"] == "cust-expire" for item in due.json())
    first = client.post("/customer-states/ops/expire", headers=admin_headers)
    second = client.post("/customer-states/ops/expire", headers=admin_headers)
    assert first.status_code == 200 and len(first.json()) >= 1
    assert second.status_code == 200 and not any(item["customer_id"] == "cust-expire" for item in second.json())
