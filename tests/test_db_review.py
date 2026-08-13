import sys, os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import sqlite3
from db import (get_conn, insert_compile_task, update_compile_task,
                insert_review, set_human_decision, resubmit_review,
                list_pending_reviews, list_rejected_reviews)

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    monkeypatch.setenv("DB_PATH", str(db))
    import importlib, db as m
    yield importlib.reload(m)

def test_compile_task_lifecycle(fresh_db):
    m = fresh_db
    tid = m.insert_compile_task("RAW/项目/a.md", "sha256abc")
    m.update_compile_task(tid, "done", nexus_path="NEXUS/资源/a.md")
    with m.get_conn() as conn:
        row = conn.execute("SELECT status, nexus_path FROM compile_tasks WHERE id=?", (tid,)).fetchone()
    assert row == ("done", "NEXUS/资源/a.md")

def test_review_insert_and_list(fresh_db):
    m = fresh_db
    rid = m.insert_review("pending_review/应急哨兵.md", "demo_user", "产品", "approved",
                          '{"verdict":"approved"}')
    m.set_human_decision(rid, "approved")
    with m.get_conn() as conn:
        row = conn.execute("SELECT human_decision FROM pending_reviews WHERE id=?", (rid,)).fetchone()
    assert row == ("approved",)
    assert len(m.list_pending_reviews()) == 0  # 已处理不再出现在待审列表
    m.insert_review("pending_review/叫应体系.md", "demo_user", "售前", "rejected", "{}")
    assert len(m.list_pending_reviews()) == 1

def test_resubmit_clears_decision(fresh_db):
    m = fresh_db
    rid = m.insert_review("pending_review/x.md", "demo_user", "产品", "approved", "{}")
    m.set_human_decision(rid, "rejected", "内容不足")
    m.resubmit_review(rid)
    with m.get_conn() as conn:
        row = conn.execute("SELECT human_decision, reject_reason FROM pending_reviews WHERE id=?", (rid,)).fetchone()
    assert row == (None, None)

def test_rejected_resubmit_appears_in_pending(fresh_db):
    m = fresh_db
    rid = m.insert_review("pending_review/x.md", "demo_user", "产品", "approved", "{}")
    m.set_human_decision(rid, "rejected", "内容不足")
    assert m.list_pending_reviews() == []
    assert len(m.list_rejected_reviews()) == 1
    m.resubmit_review(rid)
    pend = m.list_pending_reviews()
    assert len(pend) == 1 and pend[0]["id"] == rid
    assert m.list_rejected_reviews() == []

def test_pending_review_carries_title_via_join(fresh_db):
    m = fresh_db
    m.upsert_entry("pending_review/应急哨兵.md", "concept", "应急哨兵", "产品",
                   "pending", "V1.0", None, "2026-08-13")
    m.insert_review("pending_review/应急哨兵.md", "demo_user", "产品", "approved", "{}")
    items = m.list_pending_reviews()
    assert len(items) == 1
    assert items[0]["title"] == "应急哨兵"  # LEFT JOIN 生效，dict 含 title 键
