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


def _body(**changes):
    body = {
        "idempotency_key": "intake-api-001",
        "customer_id": "customer-intake-001",
        "content": "客户确认正在评估方案，销售将在下周跟进预算反馈。",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source_type": "meeting_note",
    }
    body.update(changes)
    return body


def test_sales_intake_creates_redacted_evidence_and_session(client, admin_headers):
    response = client.post("/clarifications/intake", headers=admin_headers, json=_body())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session"]["status"] == "open"
    assert payload["evidence"]["content_hash"]
    assert payload["session"]["conversation_id"] == payload["conversation"]["conversation_id"]

    duplicate = client.post("/clarifications/intake", headers=admin_headers, json=_body())
    assert duplicate.status_code == 200
    assert duplicate.json()["session"]["session_id"] == payload["session"]["session_id"]
    import db
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE conversation_id=%s",
                            (payload["conversation"]["conversation_id"],)).fetchone()[0] == 1

    mine = client.get("/clarifications/mine", headers=admin_headers)
    assert mine.status_code == 200
    assert any(item["session_id"] == payload["session"]["session_id"] for item in mine.json())


def test_sales_intake_rejects_unprotected_numeric_content(client, admin_headers):
    response = client.post("/clarifications/intake", headers=admin_headers,
                           json=_body(idempotency_key="intake-api-002", customer_id="customer-intake-002",
                                      content="客户预算为 120 万元，计划下周确认。"))
    assert response.status_code == 400
    assert "numeric_protection_missing" in response.json()["detail"]["risk_flags"]
