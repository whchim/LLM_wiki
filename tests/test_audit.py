"""SP2 审计与搜索端点测试。"""
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


@pytest.fixture()
def headers(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_writes_audit(client):
    import db
    client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='login'").fetchone()[0]
    assert n >= 1


def test_rebuild_index_audit_and_role(client, headers, tmp_path):
    # 预置一个知识文件（KB_ROOT 指向 tmp_path/vault，conftest 已设置）
    nexus = tmp_path / "vault" / "NEXUS" / "资源"
    nexus.mkdir(parents=True, exist_ok=True)
    (nexus / "测试资源.md").write_text(
        "---\ntype: resource\ntitle: 测试资源\nstatus: active\nsource: RAW/测试\n---\n\n## 摘要\n测试内容",
        encoding="utf-8")
    r = client.post("/admin/rebuild-index", headers=headers)
    assert r.status_code == 200
    assert r.json()["entries"] >= 1
    import db
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='rebuild_index'").fetchone()[0]
    assert n >= 1


def test_search_writes_search_log(client, headers):
    r = client.get("/search", params={"query": "示例监测产品"}, headers=headers)
    assert r.status_code == 200
    assert "matches" in r.json()
    import db
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM search_logs WHERE source='api'").fetchone()[0]
    assert n >= 1


def test_search_missed_and_stats(client, headers):
    client.get("/search", params={"query": "definitely_missing_zzz"}, headers=headers)
    r = client.get("/search/missed", headers=headers)
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)
    r = client.get("/search/stats", headers=headers)
    assert r.status_code == 200
    assert "total" in r.json() and "miss_rate" in r.json()


def test_entries_list(client, headers, tmp_path):
    nexus = tmp_path / "vault" / "NEXUS" / "资源"
    nexus.mkdir(parents=True, exist_ok=True)
    (nexus / "测试资源.md").write_text(
        "---\ntype: resource\ntitle: 测试资源\nstatus: active\nsource: RAW/测试\n---\n\n## 摘要\n测试内容",
        encoding="utf-8")
    import db
    db.rebuild_index()
    r = client.get("/entries", headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] >= 1
    assert isinstance(r.json()["items"], list)


def test_unknown_action_not_logged(client, headers):
    """非法 action 不落库（audit_log 的 action 枚举守卫）。"""
    from api import audit
    audit.audit_log("admin", "not_a_real_action", "x")
    import db
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='not_a_real_action'").fetchone()[0]
    assert n == 0