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


def test_tasks_list_serializes_completed_at(client, headers):
    """**已完成任务必须能列出来**（回归锁）。

    L2 把 `compile_tasks.started_at/completed_at` 从 TEXT 改成 TIMESTAMPTZ 后，
    `TaskOut.completed_at` 仍声明为 `str`：只要列表里出现一个**有完成时间**的任务，
    FastAPI 响应校验就 500（实测：上传页第 6 条起就整页打不开——前 5 条恰好是 pending）。
    这里显式插入一条 done 任务，锁住"能序列化 + 是 ISO 时间字符串"。
    """
    import db
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status, started_at, completed_at) "
            "VALUES (%s,%s,'done', now() - interval '1 minute', now())",
            ("RAW/个人_notes/done.md", "done123"))

    r = client.get("/uploads/tasks", headers=headers)
    assert r.status_code == 200, r.text
    done = next(t for t in r.json() if t["status"] == "done")
    assert isinstance(done["completed_at"], str) and "T" in done["completed_at"]
    assert done["raw_path"] == "RAW/个人_notes/done.md"


def test_tasks_are_tenant_scoped(client, headers):
    """任务列表只看当前租户（L3.5：队列与看板都不能串租户）。"""
    import db
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status, tenant_id) "
            "VALUES ('RAW/别的租户.md','t-other','pending','tenant-x')")
    r = client.get("/uploads/tasks", headers=headers)
    assert r.status_code == 200
    assert all(t["raw_path"] != "RAW/别的租户.md" for t in r.json())


def test_upload_trigger_failure_compensates(client, headers, tmp_path, monkeypatch):
    """**CLI 引擎**下触发文件写入失败：已插任务补偿为 failed，不残留无触发的 pending。

    注意：应用内引擎（`COMPILE_ENGINE=api`，默认）由队列 worker 认领，**不写触发文件**，
    因此本用例显式切到 `claude_cli` 才验证这条补偿路径（对照用例见 test_compile_queue.py）。
    """
    import api.routers.upload_router as ur
    import ops as ops_mod

    monkeypatch.setenv("COMPILE_ENGINE", "claude_cli")

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


def test_upload_does_not_overwrite_existing_file(client, headers, tmp_path):
    from pathlib import Path
    target = tmp_path / "vault" / "RAW" / "项目" / "same.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original")
    r = client.post("/uploads", files=[("files", ("same.md", b"new", "text/markdown"))],
                    data={"category": "项目"}, headers=headers)
    assert r.status_code == 200 and r.json()["ok"] == 0
    assert target.read_bytes() == b"original"
