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
