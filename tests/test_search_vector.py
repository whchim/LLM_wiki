"""SP4 混合检索测试：embedding 客户端 / 双通道融合 / 降级 / backfill。

不依赖真实 DashScope API——HTTP 全部 mock，语义端到端用真实 key 做 smoke（本地跑）。
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

from api.main import app  # noqa: E402

pytestmark = pytest.mark.usefixtures("_env")

FAKE_VEC = [0.01] * 1024   # 维度占位（pgvector 需 1024 维匹配列定义）


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def headers(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def seeded(tmp_path):
    """预置两个条目（一个会被向量命中）。"""
    import db
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, status, description) "
            "VALUES ('NEXUS/概念/叫应体系.md','concept','叫应体系','active',"
                    "'预警发布后的责任人与响应联动机制')")
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, status, description) "
            "VALUES ('NEXUS/概念/无关条目.md','concept','无关条目','active','财务报销流程')")


# ---------- embedding 客户端 ----------

def test_is_available_requires_key(monkeypatch):
    from api import embedding
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert embedding.is_available() is False
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    assert embedding.is_available() is True


def test_embed_texts_success(monkeypatch):
    from api import embedding
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")

    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data.decode())
        ctx = json.dumps({"data": [
            {"index": 0, "embedding": [0.1] * 1024},
            {"index": 1, "embedding": [0.2] * 1024}]})
        return open(os.devnull) if False else _FakeResp(ctx)

    monkeypatch.setattr(embedding.urllib.request, "urlopen", fake_urlopen)
    vecs = embedding.embed_texts(["甲", "乙"])
    assert len(vecs) == 2 and len(vecs[0]) == 1024
    assert captured["body"]["model"] == "text-embedding-v4"


def test_embed_texts_retry_on_429(monkeypatch):
    from api import embedding
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    calls = {"n": 0}

    def flaky(req, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise embedding.urllib.error.HTTPError(
                "url", 429, "rate", None, None)  # 第一次限流
        return _FakeResp(json.dumps({"data": [{"index": 0, "embedding": [0.3] * 1024}]}))

    monkeypatch.setattr(embedding.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(embedding.time, "sleep", lambda s: None)  # 跳过退避等待
    vecs = embedding.embed_texts(["甲"])
    assert len(vecs) == 1 and calls["n"] == 2


class _FakeResp:
    """urlopen 的最小上下文管理器替身。"""
    def __init__(self, text):
        self._text = text
    def read(self):
        return self._text.encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


# ---------- 检索融合与降级 ----------

def _seed_vector(path: str, conn):
    conn.execute("UPDATE knowledge_entries SET embedding = %s::vector "
                 "WHERE path=%s", (json.dumps([0.5] * 1024), path))


def test_search_vector_channel_hits(client, headers, seeded, monkeypatch):
    """配置 key + 预置向量后，向量通道可用（响应 channels.vector>=1）。"""
    import db
    with db.get_conn() as conn:
        _seed_vector("NEXUS/概念/叫应体系.md", conn)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    from api import embedding
    monkeypatch.setattr(embedding, "embed_query",
                        lambda q: [0.5] * 1024)  # mock：与预置向量同向
    r = client.get("/search", params={"query": "如何通知到人"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["channels"]["vector"] >= 1
    assert any(e["path"] == "NEXUS/概念/叫应体系.md" for e in body["entries"])


def test_search_degrades_to_grep_without_key(client, headers, seeded, monkeypatch):
    """key 未配置 → 自动 grep-only，服务不崩。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    r = client.get("/search", params={"query": "叫应"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["channels"] == {"grep": body["matches"], "vector": 0}


def test_search_degrades_on_embedding_error(client, headers, seeded, monkeypatch):
    """embedding 服务故障 → 降级 grep-only，不崩。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    from api import embedding
    def boom(q):
        raise embedding.EmbeddingError("网络故障")
    monkeypatch.setattr(embedding, "embed_query", boom)
    r = client.get("/search", params={"query": "叫应"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["channels"]["vector"] == 0


def test_fuse_ranks_dual_channel_first(client, headers, seeded, tmp_path, monkeypatch):
    """双通道都命中的条目应排在单通道之前。"""
    import db
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    from api import embedding
    monkeypatch.setattr(embedding, "embed_query", lambda q: [0.5] * 1024)
    # 造一个真实文件（正文含"叫应"→grep 命中）且向量匹配（vector 命中）的条目
    doc = tmp_path / "vault" / "NEXUS" / "概念" / "叫应体系双通道.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: concept\ntitle: 叫应体系双通道\nstatus: active\n---\n\n"
                   "预警叫应机制说明", encoding="utf-8")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, status, description) "
            "VALUES ('NEXUS/概念/叫应体系双通道.md','concept','叫应体系双通道','active','叫应相关')")
        _seed_vector("NEXUS/概念/叫应体系双通道.md", conn)
    r = client.get("/search", params={"query": "叫应"}, headers=headers)
    entries = r.json()["entries"]
    assert entries[0]["path"] == "NEXUS/概念/叫应体系双通道.md"  # 双通道得分最高
    ch = entries[0]["channels"]
    assert ch["grep"] == 1 and ch["vector"] == 1


def test_mode_grep_keeps_legacy_behavior(client, headers, seeded, monkeypatch):
    """mode=grep 与旧版行为一致（即使配置了 key 也不走向量）。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    r = client.get("/search", params={"query": "叫应", "mode": "grep"}, headers=headers)
    assert r.json()["channels"]["vector"] == 0


# ---------- backfill ----------

def test_backfill_fills_and_idempotent(client, headers, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    from api import embedding
    monkeypatch.setattr(embedding, "embed_texts",
                        lambda texts: [[0.7] * 1024 for _ in texts])

    # 预置一条无向量条目
    import db
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, status, description) "
            "VALUES ('NEXUS/概念/待向量化.md','concept','待向量化','active','描述')")

    # 第一次回填 filled>=1，第二次 remaining=0（幂等）
    r1 = client.post("/admin/backfill-embeddings", params={"batch": 10}, headers=headers)
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["filled"] >= 1
    r2 = client.post("/admin/backfill-embeddings", params={"batch": 10}, headers=headers)
    assert r2.json()["remaining"] == 0
    assert r2.json()["filled"] == 0


def test_backfill_requires_admin(client):
    """非 admin 调回填 → 403。"""
    import db
    from api import auth as auth_mod
    with db.get_conn() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role) "
                     "VALUES (%s,%s,'user')", ("carol", auth_mod.hash_password("carol123")))
    tok = client.post("/auth/login", json={"username": "carol", "password": "carol123"}).json()["access_token"]
    r = client.post("/admin/backfill-embeddings",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


# ---------- 缺口判据（SP4 v0.1.1 勘误落地）----------

def _last_search_log(conn):
    row = conn.execute(
        "SELECT query, match_count FROM search_logs ORDER BY timestamp DESC, query LIMIT 1").fetchone()
    return row


def test_gap_true_when_grep_miss_and_low_similarity(client, headers, seeded, monkeypatch):
    """grep 零命中 + 向量最高相似度 < τ（正交向量 → sim≈0）→ gap=true，match_count 记 0。"""
    import db
    from api.routers import search_router as sr
    with db.get_conn() as conn:
        _seed_vector("NEXUS/概念/叫应体系.md", conn)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    from api import embedding
    # [0.5]*512 + [-0.5]*512：与预置 [0.5]*1024 点积=0 → cosine=0 → similarity≈0 < τ
    monkeypatch.setattr(embedding, "embed_query", lambda q: [0.5] * 512 + [-0.5] * 512)

    r = client.get("/search", params={"query": "员工年会的照片在哪里"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["gap"] is True
    assert body["channels"]["grep"] == 0
    with db.get_conn() as conn:
        assert _last_search_log(conn)[1] == 0  # 缺口 → match_count=0（看板零改动可聚合）


def test_gap_false_when_high_similarity(client, headers, seeded, monkeypatch):
    """grep 零命中但向量相似度高（同向 → sim≈1 ≥ τ）→ gap=false，match_count=融合命中数。"""
    import db
    with db.get_conn() as conn:
        _seed_vector("NEXUS/概念/叫应体系.md", conn)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    from api import embedding
    monkeypatch.setattr(embedding, "embed_query", lambda q: [0.5] * 1024)

    r = client.get("/search", params={"query": "预警发布后如何通知到人"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["gap"] is False
    assert body["matches"] >= 1
    with db.get_conn() as conn:
        q, mc = _last_search_log(conn)
        assert mc >= 1  # 非缺口 → 正常记命中数


def test_gap_legacy_when_vector_unavailable(client, headers, seeded, tmp_path, monkeypatch):
    """向量不可用（无 key）→ 缺口退化为旧语义：grep 零命中即缺口。"""
    doc = tmp_path / "vault" / "NEXUS" / "概念" / "叫应体系.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: concept\ntitle: 叫应体系\nstatus: active\n---\n\n预警叫应机制说明",
                   encoding="utf-8")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    r_miss = client.get("/search", params={"query": "脑机接口芯片进度如何"}, headers=headers)
    assert r_miss.json()["gap"] is True
    r_hit = client.get("/search", params={"query": "叫应"}, headers=headers)
    assert r_hit.json()["gap"] is False


def test_gap_not_triggered_when_grep_hits(client, headers, seeded, tmp_path, monkeypatch):
    """grep 有命中 → 不算缺口（即使向量相似度低）。"""
    import db
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    from api import embedding
    monkeypatch.setattr(embedding, "embed_query", lambda q: [0.5] * 512 + [-0.5] * 512)
    doc = tmp_path / "vault" / "NEXUS" / "概念" / "叫应体系低向量.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: concept\ntitle: 叫应体系\nstatus: active\n---\n\n预警叫应机制说明",
                   encoding="utf-8")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, status, description) "
            "VALUES ('NEXUS/概念/叫应体系低向量.md','concept','叫应体系低向量','active','叫应')")
        _seed_vector("NEXUS/概念/叫应体系低向量.md", conn)

    r = client.get("/search", params={"query": "叫应体系"}, headers=headers)
    body = r.json()
    assert body["channels"]["grep"] >= 1
    assert body["gap"] is False  # grep 命中 → 缺口判据短路