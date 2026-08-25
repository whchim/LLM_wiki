"""SP2 上传端点测试：上传成功（文件+任务+触发+审计）、校验失败、鉴权。"""
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
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_upload_happy_path(client, headers, tmp_path):
    content = "上传测试文档\nSP2 smoke\n".encode("utf-8")
    files = [("files", ("sp2_测试.md", content, "text/markdown"))]
    r = client.post("/uploads", files=files, data={"category": "个人_notes"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] == 1
    assert not body["errors"]
    # 文件落盘 RAW + 任务入库
    raw = (tmp_path / "vault" / "RAW" / "个人_notes" / "sp2_测试.md")
    assert raw.read_bytes() == content


def test_upload_invalid_ext(client, headers):
    files = [("files", ("photo.jpg", b"xx", "image/jpeg"))]
    r = client.post("/uploads", files=files, data={"category": "个人_notes"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] == 0
    assert any("不支持的文件格式" in e for e in r.json()["errors"])


def test_upload_requires_login(client):
    files = [("files", ("a.md", b"x", "text/markdown"))]
    r = client.post("/uploads", files=files, data={"category": "个人_notes"})
    assert r.status_code == 401


def test_upload_writes_audit(client, headers):
    files = [("files", ("audit_测试.md", b"audit content", "text/markdown"))]
    client.post("/uploads", files=files, data={"category": "会议"}, headers=headers)
    import db
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT operator, action, target_path FROM audit_logs WHERE action='upload'").fetchall()
    assert any(r[0] == "admin" and "audit_测试.md" in (r[2] or "") for r in rows)


def test_tasks_and_retry_flow(client, headers):
    """任务列表可读；failed 任务可重试（注入一条 failed 任务）。"""
    import db
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status, error_msg) "
            "VALUES (%s,%s,'failed','测试错误')", ("RAW/个人_notes/x.md", "abc123"))
    r = client.get("/uploads/tasks", headers=headers)
    assert r.status_code == 200
    assert any(t["status"] == "failed" for t in r.json())

    failed_id = next(t["id"] for t in r.json() if t["status"] == "failed")
    rr = client.post(f"/uploads/tasks/{failed_id}/retry", headers=headers)
    assert rr.status_code == 200
    assert rr.json()["task_id"] == failed_id


def test_upload_trigger_failure_compensates(client, headers, tmp_path, monkeypatch):
    """触发文件写入失败：已插任务补偿为 failed，不残留无触发的 pending。"""
    import api.routers.upload_router as ur
    import ops as ops_mod

    def boom(kind, paths, source):
        raise OSError("模拟 _triggers 目录不可写")

    monkeypatch.setattr(ops_mod, "write_trigger", boom)
    monkeypatch.setattr(ur, "ops", ops_mod)  # upload_router 引用同一个 ops 模块

    files = [("files", ("a.md", b"# a\n", "text/markdown"))]
    r = client.post("/uploads", files=files, data={"category": "项目"}, headers=headers)
    assert r.status_code == 500
    import db
    with db.get_conn() as conn:
        rows = conn.execute("SELECT status, error_msg FROM compile_tasks").fetchall()
    assert rows and all(x[0] == "failed" for x in rows)
    assert all("触发文件写入失败" in (x[1] or "") for x in rows)