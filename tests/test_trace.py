"""SP2.5 可观测性测试：编译采集工具 / FastAPI 埋点（ok+error 全记录）/ trace_id 分组。"""
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


def _trace_rows(span_type: str) -> list[dict]:
    import db
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT span_type, trace_id, status, latency_ms, detail, operator "
            "FROM trace_events WHERE span_type=%s ORDER BY id", (span_type,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------- 编译采集工具（过程 trace） ----------

def test_record_compile_trace_writes_session():
    import subprocess, sys
    script = os.path.join(os.path.dirname(__file__), "..", "tools", "record_compile_trace.py")
    r = subprocess.run(
        [sys.executable, script,
         "--trace-id", "t-abc-123", "--compiled", "5", "--cached", "3", "--failed", "1",
         "--files", '["NEXUS/资源/a.md", "pending_review/b.md"]', "--latency-ms", "12000"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    rows = _trace_rows("compile_session")
    assert any(x["trace_id"] == "t-abc-123" for x in rows)
    row = next(x for x in rows if x["trace_id"] == "t-abc-123")
    assert row["status"] == "ok"
    assert row["detail"]["compiled"] == 5
    assert row["detail"]["cached"] == 3
    assert row["detail"]["failed"] == 1
    assert "NEXUS/资源/a.md" in row["detail"]["files"]
    assert row["latency_ms"] == 12000


def test_record_compile_trace_rejects_bad_files_json():
    import subprocess, sys
    script = os.path.join(os.path.dirname(__file__), "..", "tools", "record_compile_trace.py")
    r = subprocess.run(
        [sys.executable, script, "--trace-id", "t-bad", "--files", "not-json"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 1
    assert all(x["trace_id"] != "t-bad" for x in _trace_rows("compile_session"))


# ---------- FastAPI 埋点 ----------

def test_login_trace_ok_and_error(client):
    client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    rows = _trace_rows("login")
    statuses = [x["status"] for x in rows]
    assert "ok" in statuses and "error" in statuses
    err = next(x for x in rows if x["status"] == "error")
    assert err["detail"]["error"] == "认证失败"  # 统一文案，不泄漏枚举


def test_search_trace_records_hit_count(client, headers, tmp_path):
    nexus = tmp_path / "vault" / "NEXUS" / "资源"
    nexus.mkdir(parents=True, exist_ok=True)
    (nexus / "t.md").write_text(
        "---\ntype: resource\ntitle: t\nstatus: active\nsource: RAW/t\n---\n\n锚点词xyz",
        encoding="utf-8")
    r = client.get("/search", params={"query": "锚点词xyz"}, headers=headers)
    assert r.status_code == 200
    rows = _trace_rows("search")
    assert rows, "search 必须落 trace"
    row = rows[-1]
    assert row["status"] == "ok"
    assert row["detail"]["hit_count"] >= 1
    assert row["detail"]["query"] == "锚点词xyz"


def test_search_trace_error_path(client, headers):
    """检索异常（模拟 grep 崩溃）也要落 error trace。

    TestClient 需 raise_server_exceptions=False 才能拿到 500 响应而非让异常冒泡。"""
    from fastapi.testclient import TestClient as _TC
    from api.main import app as _app
    import api.routers.search_router as sr

    def boom(q):
        raise RuntimeError("模拟搜索崩溃")

    orig = sr._grep
    sr._grep = boom
    try:
        c = _TC(_app, raise_server_exceptions=False)
        r = c.get("/search", params={"query": "whatever"}, headers=headers)
        assert r.status_code >= 500
    finally:
        sr._grep = orig
    rows = _trace_rows("search")
    assert any(x["status"] == "error" for x in rows)


def test_review_approve_trace(client, headers, tmp_path):
    from pathlib import Path
    import db
    p = Path(tmp_path) / "vault"
    (p / "pending_review").mkdir(parents=True, exist_ok=True)
    doc = p / "pending_review" / "trace概念.md"
    doc.write_text("---\ntype: concept\ntitle: trace概念\nstatus: pending\nsource: RAW/t\n---\n\n"
                   "## 定义\n\n" + "内容" * 60, encoding="utf-8")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO knowledge_entries (path, type, title, status) "
                     "VALUES (%s,'concept','trace概念','pending')", ("pending_review/trace概念.md",))
        rid = conn.execute(
            "INSERT INTO pending_reviews (nexus_path, submitter, department, ai_verdict, ai_scores) "
            "VALUES (%s,'t','产品','approved','{}') RETURNING id",
            ("pending_review/trace概念.md",)).fetchone()[0]

    r = client.post(f"/reviews/{rid}/approve", headers=headers)
    assert r.status_code == 200
    rows = _trace_rows("review_approve")
    assert any(x["detail"].get("target_path") == "NEXUS/概念/trace概念.md" for x in rows)


def test_rebuild_index_trace(client, headers, tmp_path):
    nexus = tmp_path / "vault" / "NEXUS" / "资源"
    nexus.mkdir(parents=True, exist_ok=True)
    (nexus / "r.md").write_text(
        "---\ntype: resource\ntitle: r\nstatus: active\nsource: RAW/r\n---\n\nx", encoding="utf-8")
    r = client.post("/admin/rebuild-index", headers=headers)
    assert r.status_code == 200
    rows = _trace_rows("rebuild_index")
    assert any(x["detail"].get("entries") >= 1 for x in rows)


def test_trace_id_groups_compile_session():
    """同一编译会话（同 trace_id）可聚合成一条全链路。"""
    import subprocess, sys
    script = os.path.join(os.path.dirname(__file__), "..", "tools", "record_compile_trace.py")
    for files, cached in (('["a.md"]', "0"), ('["b.md"]', "2")):
        subprocess.run(
            [sys.executable, script, "--trace-id", "t-group", "--compiled", "1",
             "--cached", cached, "--files", files],
            capture_output=True, text=True, encoding="utf-8")
    rows = [x for x in _trace_rows("compile_session") if x["trace_id"] == "t-group"]
    assert len(rows) >= 2
    assert all(x["trace_id"] == "t-group" for x in rows)