import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import psycopg

TEST_DB = dict(host="localhost", port=5432, dbname="llmwiki_test",
               user="llmwiki", password="llmwiki")


def _tables():
    with psycopg.connect(**TEST_DB) as conn:
        return {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}


def test_ensure_schema_creates_dirs_and_tables_in_fresh_clone(tmp_path, monkeypatch):
    """模拟 fresh clone：KB_ROOT 无表、目录可能缺失 → ensure_schema 自愈建目录 + 建表。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    import db as m
    m = importlib.reload(m)
    m.ensure_schema()
    # 目录树补齐
    for rel in ("RAW/个人_notes", "RAW/会议", "RAW/经验", "RAW/项目",
                "pending_review", "NEXUS/资源", "NEXUS/概念", "NEXUS/研究",
                "_triggers", "_triggers/done"):
        assert (tmp_path / "vault" / rel).is_dir(), f"缺失目录 {rel}"
    # 表已建
    assert {"knowledge_entries", "compile_tasks", "pending_reviews", "search_logs",
            "users"} <= _tables()


def test_ensure_schema_is_idempotent(tmp_path, monkeypatch):
    """重复调用不报错、不产生重复业务表。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    import db as m
    m = importlib.reload(m)
    m.ensure_schema()
    m.ensure_schema()
    assert _tables() == {"knowledge_entries", "compile_tasks", "pending_reviews",
                         "search_logs", "audit_logs", "contributors", "conflicts", "users"}
