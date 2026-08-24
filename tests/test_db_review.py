import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import db


def test_compile_task_lifecycle(tmp_path):
    tid = db.insert_compile_task("RAW/项目/a.md", "sha256abc")
    db.update_compile_task(tid, "done", nexus_path="NEXUS/资源/a.md")
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, nexus_path FROM compile_tasks WHERE id=%s", (tid,)).fetchone()
    assert row == ("done", "NEXUS/资源/a.md")


def test_review_insert_and_list(tmp_path):
    rid = db.insert_review("pending_review/示例监测产品.md", "demo_user", "产品", "approved",
                           '{"verdict":"approved"}')
    db.set_human_decision(rid, "approved")
    with db.get_conn() as conn:
        row = conn.execute("SELECT human_decision FROM pending_reviews WHERE id=%s", (rid,)).fetchone()
    assert row == ("approved",)
    assert len(db.list_pending_reviews()) == 0  # 已处理不再出现在待审列表
    db.insert_review("pending_review/叫应体系.md", "demo_user", "售前", "rejected", "{}")
    assert len(db.list_pending_reviews()) == 1


def test_resubmit_clears_decision(tmp_path):
    rid = db.insert_review("pending_review/x.md", "demo_user", "产品", "approved", "{}")
    db.set_human_decision(rid, "rejected", "内容不足")
    db.resubmit_review(rid)
    with db.get_conn() as conn:
        row = conn.execute("SELECT human_decision, reject_reason FROM pending_reviews WHERE id=%s", (rid,)).fetchone()
    assert row == (None, None)


def test_rejected_resubmit_appears_in_pending(tmp_path):
    rid = db.insert_review("pending_review/x.md", "demo_user", "产品", "approved", "{}")
    db.set_human_decision(rid, "rejected", "内容不足")
    assert db.list_pending_reviews() == []
    assert len(db.list_rejected_reviews()) == 1
    db.resubmit_review(rid)
    pend = db.list_pending_reviews()
    assert len(pend) == 1 and pend[0]["id"] == rid
    assert db.list_rejected_reviews() == []


def test_pending_review_carries_title_via_join(tmp_path):
    db.upsert_entry("pending_review/示例监测产品.md", "concept", "示例监测产品", "产品",
                    "pending", "V1.0", None, "2026-08-13")
    db.insert_review("pending_review/示例监测产品.md", "demo_user", "产品", "approved", "{}")
    items = db.list_pending_reviews()
    assert len(items) == 1
    assert items[0]["title"] == "示例监测产品"  # LEFT JOIN 生效，dict 含 title 键
