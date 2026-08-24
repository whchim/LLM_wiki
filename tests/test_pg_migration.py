"""SP1 专项测试：PostgreSQL 迁移一致性（pgvector / upsert 幂等 / RETURNING id / 重建 / 导入）。

依赖 conftest 的 autouse _env：已指向测试库、重置 schema 并重载 db。"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import importlib
import psycopg

import db

TEST_DB = dict(host="localhost", port=5432, dbname="llmwiki_test",
               user="llmwiki", password="llmwiki")


def test_pgvector_extension_present():
    with psycopg.connect(**TEST_DB) as conn:
        ext = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
    assert ext is not None and ext[0]  # vector 扩展已安装


def test_upsert_is_idempotent_on_path_conflict():
    db.upsert_entry("NEXUS/概念/x.md", "concept", "X", "共享层", "pending", "V1.0", "fp", "2026-08-21")
    db.upsert_entry("NEXUS/概念/x.md", "concept", "X", "共享层", "active", "V1.1", "fp2", "2026-08-21")
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM knowledge_entries WHERE path='NEXUS/概念/x.md'").fetchone()[0]
        status, version = conn.execute(
            "SELECT status, version FROM knowledge_entries WHERE path='NEXUS/概念/x.md'").fetchone()
    assert n == 1            # ON CONFLICT 不产生重复行
    assert (status, version) == ("active", "V1.1")


def test_insert_compile_task_returns_id_and_identity():
    # GENERATED ALWAYS AS IDENTITY：id 自增且返回
    a = db.insert_compile_task("RAW/a.md", "fpa")
    b = db.insert_compile_task("RAW/b.md", "fpb")
    assert isinstance(a, int) and isinstance(b, int)
    assert b > a
    with db.get_conn() as conn:
        row = conn.execute("SELECT id, raw_path FROM compile_tasks WHERE id=%s", (b,)).fetchone()
    assert row == (b, "RAW/b.md")


def test_rebuild_index_consistent_with_yaml(tmp_path):
    """YAML 是权威：rebuild_index 后表字段与 frontmatter 全等。"""
    kb = tmp_path / "vault"
    (kb / "NEXUS" / "概念").mkdir(parents=True, exist_ok=True)
    (kb / "NEXUS" / "资源").mkdir(parents=True, exist_ok=True)
    sample = "---\ntype: concept\ntitle: 示例监测产品\nstatus: active\ndepartment: 产品\nversion: V2.1\nupdated: 2026-08-20\n---\n正文"
    (kb / "NEXUS" / "概念" / "示例监测产品.md").write_text(sample, encoding="utf-8")
    (kb / "NEXUS" / "资源" / "白皮书.md").write_text(
        "---\ntype: resource\ntitle: 白皮书\nstatus: active\n---\n正文", encoding="utf-8")
    n = db.rebuild_index()
    assert n == 2
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT type, title, department, status, version, updated_at "
            "FROM knowledge_entries WHERE path='NEXUS/概念/示例监测产品.md'").fetchone()
    assert row == ("concept", "示例监测产品", "产品", "active", "V2.1", "2026-08-20")


def test_ai_scores_jsonb_roundtrip():
    rid = db.insert_review("pending_review/x.md", "u1", "产品", "approved",
                           '{"维度1":5,"维度2":4}')
    with db.get_conn() as conn:
        verdict = conn.execute(
            "SELECT ai_scores->>'维度1', ai_scores->>'维度2' FROM pending_reviews WHERE id=%s",
            (rid,)).fetchone()
    assert verdict == ("5", "4")


def test_history_import_roundtrip(tmp_path, monkeypatch):
    """一次性导入脚本：旧 SQLite 历史表 → PG 往返一致（保留时间戳语义）。"""
    # 组装一个带样例数据的旧 SQLite
    src = tmp_path / "meta.db"
    sconn = sqlite3.connect(str(src))
    sconn.executescript(
        "CREATE TABLE search_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT, match_count INTEGER DEFAULT 0, source TEXT DEFAULT 'streamlit', timestamp TEXT);"
        "CREATE TABLE pending_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, nexus_path TEXT, submitter TEXT, department TEXT, ai_verdict TEXT, ai_scores TEXT, human_decision TEXT, reject_reason TEXT, created_at TEXT);"
        "CREATE TABLE compile_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, raw_path TEXT, nexus_path TEXT, fingerprint TEXT, status TEXT, error_msg TEXT, started_at TEXT, completed_at TEXT);")
    sconn.execute("INSERT INTO search_logs(query, match_count, source, timestamp) VALUES ('未命中词',0,'streamlit','2026-08-19 10:00:00')")
    sconn.execute("INSERT INTO pending_reviews(nexus_path, submitter, department, ai_verdict, ai_scores, human_decision, created_at) VALUES ('x.md','u1','产品','approved','{}',NULL,'2026-08-19 10:00:00')")
    sconn.execute("INSERT INTO compile_tasks(raw_path, fingerprint, status, started_at) VALUES ('RAW/a.md','fp1','done','2026-08-19 10:00:00')")
    sconn.commit(); sconn.close()

    # 调用导入函数（monkeypatch 让脚本走测试库连接参数）
    sys.path.insert(0, str(ROOT / "tools"))
    import migrate_to_pg
    importlib.reload(migrate_to_pg)
    counts = migrate_to_pg._import_history(src)

    assert counts == {"compile_tasks": 1, "pending_reviews": 1, "search_logs": 1}
    with db.get_conn() as conn:
        row = conn.execute("SELECT query, match_count, timestamp FROM search_logs").fetchone()
    assert row == ("未命中词", 0, "2026-08-19 10:00:00")
