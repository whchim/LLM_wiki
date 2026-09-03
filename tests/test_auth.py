"""SP2 认证测试：登录/401/403/角色守卫。

依赖真实 PostgreSQL（llmwiki_test，由 conftest 每测试重置 + 注入初始 admin）。
"""
import os

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


def _login(client, username="admin", password="admin123") -> dict:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_healthz_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_success_returns_token_and_role(client):
    body = _login(client)
    assert body["access_token"]
    assert body["role"] == "admin"
    assert body["token_type"] == "bearer"


def test_login_wrong_password_401(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user_401(client):
    r = client.post("/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401  # 与密码错同码，防用户枚举


def test_me_requires_token(client):
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_me_returns_role(client):
    body = _login(client)
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_invalid_token_401(client):
    r = client.get("/auth/me", headers={"Authorization": "Bearer not.a.token"})
    assert r.status_code == 401


def test_admin_endpoint_requires_admin(client):
    """普通用户（非 admin 角色）调 /admin/rebuild-index → 403。"""
    # 注入一个普通用户
    import db
    from api import auth as auth_mod
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s,%s,'user')",
            ("alice", auth_mod.hash_password("alice123"), ))
    body = _login(client, "alice", "alice123")
    assert body["role"] == "user"
    r = client.post("/admin/rebuild-index",
                    headers={"Authorization": f"Bearer {body['access_token']}"})
    assert r.status_code == 403  # 越权被拒


def test_ensure_ready_raises_without_secret(monkeypatch):
    """JWT_SECRET 缺失时 fail-fast（运行时检查，不阻塞 import 本身）。"""
    from api import auth as auth_mod
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        auth_mod.ensure_ready()