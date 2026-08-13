import sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import sqlite3
from db import insert_search_log, top_missed_queries, search_stats, get_conn

def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    monkeypatch.setenv("DB_PATH", str(db))
    import importlib, db as m
    return importlib.reload(m)

def test_search_logs_aggregation(tmp_path, monkeypatch):
    m = _setup(tmp_path, monkeypatch)
    m.insert_search_log("区块链", 0, "streamlit")
    m.insert_search_log("区块链", 0, "claude_code")
    m.insert_search_log("示例监测产品", 3, "streamlit")
    top = m.top_missed_queries(10)
    assert top[0]["query"] == "区块链" and top[0]["cnt"] == 2
    stats = m.search_stats()
    assert stats["total"] == 3 and stats["miss_count"] == 2

def test_rebuild_index_from_files(tmp_path, monkeypatch):
    m = _setup(tmp_path, monkeypatch)
    kb = tmp_path / "vault"
    (kb / "NEXUS" / "概念").mkdir(parents=True)
    (kb / "NEXUS" / "资源").mkdir(parents=True)
    (kb / "pending_review").mkdir()
    (kb / "NEXUS" / "概念" / "示例监测产品.md").write_text(
        "---\ntype: concept\ntitle: 示例监测产品\nstatus: active\ndepartment: 产品\n---\n正文", encoding="utf-8")
    (kb / "NEXUS" / "资源" / "白皮书.md").write_text(
        "---\ntype: resource\ntitle: 白皮书\nstatus: active\n---\n正文", encoding="utf-8")
    (kb / "pending_review" / "叫应体系.md").write_text(
        "---\ntype: concept\ntitle: 叫应体系\nstatus: pending\n---\n正文", encoding="utf-8")
    monkeypatch.setenv("KB_ROOT", str(kb))
    import importlib, db as m2
    m2 = importlib.reload(m2)
    n = m2.rebuild_index()
    assert n == 3
    with m2.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0] == 3
