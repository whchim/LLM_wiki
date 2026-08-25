"""SP2 审核端点测试：role 守卫、approve/reject/resubmit 流程、审计落库。"""
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


def _make_pending_review(tmp_path):
    """在测试库生成一条 pending_review 记录 + 文件。"""
    import db
    from pathlib import Path
    p = Path(tmp_path) / "vault"
    (p / "pending_review").mkdir(parents=True, exist_ok=True)
    doc = p / "pending_review" / "冒烟概念.md"
    doc.write_text("---\ntype: concept\ntitle: 冒烟概念\nstatus: pending\nsource: RAW/测试\n---\n\n" +
                   "## 定义\n\n测试概念内容，" + "内容" * 60, encoding="utf-8")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, status) "
            "VALUES (%s,'concept','冒烟概念','pending')", ("pending_review/冒烟概念.md",))
        rid = conn.execute(
            "INSERT INTO pending_reviews (nexus_path, submitter, department, ai_verdict, ai_scores) "
            "VALUES (%s,'tester','产品','approved','{}') RETURNING id",
            ("pending_review/冒烟概念.md",)).fetchone()[0]
    return rid, doc


def test_reviews_require_reviewer_role(client):
    """普通用户看审核列表 → 403。"""
    import db
    from api import auth as auth_mod
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s,%s,'user')",
            ("bob", auth_mod.hash_password("bob123")))
    r = client.post("/auth/login", json={"username": "bob", "password": "bob123"})
    tok = r.json()["access_token"]
    r = client.get("/reviews/pending", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_pending_list_and_approve(client, headers, tmp_path):
    rid, doc = _make_pending_review(tmp_path)
    r = client.get("/reviews/pending", headers=headers)
    assert r.status_code == 200
    assert any(x["id"] == rid for x in r.json())

    r = client.post(f"/reviews/{rid}/approve", headers=headers)
    assert r.status_code == 200, r.text
    assert "NEXUS/概念/冒烟概念.md" in r.json()["target_path"]
    # 文件已移动 + YAML status=active
    from pathlib import Path
    assert (Path(tmp_path) / "vault" / "NEXUS" / "概念" / "冒烟概念.md").exists()
    txt = (Path(tmp_path) / "vault" / "NEXUS" / "概念" / "冒烟概念.md").read_text(encoding="utf-8")
    assert "status: active" in txt
    # 审计
    import db
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='review_approve'").fetchone()[0]
    assert n >= 1


def test_reject_requires_reason(client, headers, tmp_path):
    rid, _ = _make_pending_review(tmp_path)
    r = client.post(f"/reviews/{rid}/reject", headers=headers, json={"reason": ""})
    assert r.status_code == 400


def test_reject_and_resubmit(client, headers, tmp_path):
    rid, doc = _make_pending_review(tmp_path)
    r = client.post(f"/reviews/{rid}/reject", headers=headers, json={"reason": "内容不足"})
    assert r.status_code == 200
    # YAML status=draft + pending_reviews 有原因
    txt = doc.read_text(encoding="utf-8")
    assert "status: draft" in txt
    r = client.get("/reviews/rejected", headers=headers)
    assert any(x["id"] == rid and x["reject_reason"] == "内容不足" for x in r.json())

    r = client.post(f"/reviews/{rid}/resubmit", headers=headers)
    assert r.status_code == 200
    assert "status: pending" in doc.read_text(encoding="utf-8")


def test_approve_missing_review_404(client, headers):
    r = client.post("/reviews/99999/approve", headers=headers)
    assert r.status_code == 404