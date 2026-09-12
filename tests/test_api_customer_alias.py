"""客户别名绑定的 API 测试。

别名的作用是让销售用内部中文简称选客户，而系统内部只存脱敏代号。
这里锁住的四条边界：
1. 别名可以建、可以列、可以删（仅创建者或管理员）
2. 别名必须过敏感信息检查（用户自填自由文本）
3. 同一别名绑到不同客户 → 显式 409，绝不静默合并
4. intake 走别名时用的是解析出的代号，别名本身不入业务数据
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


def _body(**changes):
    body = {
        "idempotency_key": "alias-api-001",
        "customer_id": "cust-alias-001",
        "content": "客户确认正在评估方案，销售将在下周跟进预算反馈。",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source_type": "meeting_note",
    }
    body.update(changes)
    return body


# ---- 建 / 列 / 删 ----

def test_create_list_and_delete_alias(client, admin_headers):
    created = client.post("/customers/aliases", headers=admin_headers,
                          json={"alias": "某某项目", "customer_id": "cust-x1"})
    assert created.status_code == 200, created.text
    assert created.json()["alias"] == "某某项目"
    assert created.json()["customer_id"] == "cust-x1"

    listed = client.get("/customers/aliases", headers=admin_headers).json()
    assert any(a["alias"] == "某某项目" and a["customer_id"] == "cust-x1" for a in listed)

    deleted = client.delete("/customers/aliases", headers=admin_headers,
                            params={"alias": "某某项目", "customer_id": "cust-x1"})
    assert deleted.status_code == 200
    assert not any(a["alias"] == "某某项目" for a in client.get("/customers/aliases", headers=admin_headers).json())


def test_alias_is_trimmed_and_internal_spaces_collapsed(client, admin_headers):
    created = client.post("/customers/aliases", headers=admin_headers,
                          json={"alias": "  某某   项目  ", "customer_id": "cust-x2"})
    assert created.status_code == 200
    assert created.json()["alias"] == "某某 项目"


def test_alias_requires_auth(client):
    assert client.get("/customers/aliases").status_code == 401
    assert client.post("/customers/aliases", json={"alias": "a", "customer_id": "c"}).status_code == 401


# ---- 敏感信息与格式 ----

def test_alias_with_phone_number_is_rejected(client, admin_headers):
    response = client.post("/customers/aliases", headers=admin_headers,
                           json={"alias": "13800138000", "customer_id": "cust-x3"})
    assert response.status_code == 400
    assert "敏感" in response.json()["detail"]


def test_alias_must_be_non_empty_and_bounded(client, admin_headers):
    assert client.post("/customers/aliases", headers=admin_headers,
                       json={"alias": "   ", "customer_id": "cust-x4"}).status_code == 400
    assert client.post("/customers/aliases", headers=admin_headers,
                       json={"alias": "长" * 41, "customer_id": "cust-x5"}).status_code == 400


def test_alias_requires_valid_customer_id(client, admin_headers):
    """别名不能把不合规的代号带进系统。"""
    response = client.post("/customers/aliases", headers=admin_headers,
                           json={"alias": "某某项目", "customer_id": "贵阳某某公司"})
    assert response.status_code == 400
    assert "customer_id" in response.json()["detail"]


# ---- 冲突：绝不静默合并 ----

def test_same_alias_for_different_customer_is_rejected(client, admin_headers):
    first = client.post("/customers/aliases", headers=admin_headers,
                        json={"alias": "同名项目", "customer_id": "cust-a"})
    assert first.status_code == 200
    conflict = client.post("/customers/aliases", headers=admin_headers,
                           json={"alias": "同名项目", "customer_id": "cust-b"})
    assert conflict.status_code == 409, conflict.text
    assert "cust-a" in conflict.json()["detail"], "冲突提示要指出已占用的客户，便于人工处理"
    # 原绑定不受影响
    listed = client.get("/customers/aliases", headers=admin_headers).json()
    assert [a["customer_id"] for a in listed if a["alias"] == "同名项目"] == ["cust-a"]


def test_same_customer_can_have_multiple_aliases(client, admin_headers):
    for alias in ("某某项目", "某某科技", "贵阳客户"):
        assert client.post("/customers/aliases", headers=admin_headers,
                           json={"alias": alias, "customer_id": "cust-multi"}).status_code == 200
    listed = [a["alias"] for a in client.get("/customers/aliases", headers=admin_headers).json()
              if a["customer_id"] == "cust-multi"]
    assert set(listed) == {"某某项目", "某某科技", "贵阳客户"}


# ---- intake 走别名 ----

def test_intake_resolves_alias_to_customer_id(client, admin_headers):
    client.post("/customers/aliases", headers=admin_headers,
                json={"alias": "别名项目", "customer_id": "cust-via-alias"})
    response = client.post("/clarifications/intake", headers=admin_headers,
                           json=_body(customer_alias="别名项目", customer_id=""))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["conversation"]["customer_id"] == "cust-via-alias"
    assert payload["resolved_customer_id"] == "cust-via-alias"
    assert payload["alias"] == "别名项目"
    # 别名不进业务数据：会话里只有代号
    assert "别名项目" not in str(payload["conversation"])


def test_intake_alias_wins_over_customer_id(client, admin_headers):
    """同时给了别名和 customer_id 时，以别名为准（前端选别名后不应被残留值覆盖）。"""
    client.post("/customers/aliases", headers=admin_headers,
                json={"alias": "优先别名", "customer_id": "cust-preferred"})
    response = client.post("/clarifications/intake", headers=admin_headers,
                           json=_body(customer_alias="优先别名", customer_id="cust-stale"))
    assert response.status_code == 200
    assert response.json()["conversation"]["customer_id"] == "cust-preferred"


def test_intake_with_unregistered_alias_is_rejected(client, admin_headers):
    response = client.post("/clarifications/intake", headers=admin_headers,
                           json=_body(customer_alias="没登记过的名字", customer_id=""))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "尚未登记" in detail
    assert "customer_id" in detail, "提示要告诉用户可以改用代号"


def test_intake_empty_alias_falls_back_to_customer_id(client, admin_headers):
    response = client.post("/clarifications/intake", headers=admin_headers,
                           json=_body(customer_alias="  ", customer_id="cust-direct"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["conversation"]["customer_id"] == "cust-direct"
    assert payload["resolved_customer_id"] is None, "未走别名时不该报告解析结果"
    assert payload["alias"] is None
