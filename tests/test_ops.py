import sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import sqlite3
from ops import write_trigger, validate_upload, approve_entry, reject_entry, resubmit, sha256_file

def test_write_trigger_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    (tmp_path / "_triggers").mkdir()
    import importlib, ops
    ops = importlib.reload(ops)  # 重新求值 KB_ROOT，指向 monkeypatch 后的 tmp_path
    p = ops.write_trigger("compile", ["RAW/a.md", "RAW/b.md"], "streamlit")
    assert p.name.startswith("compile_") and p.name.endswith(".md")
    assert not list((Path(ops.KB_ROOT) / "_triggers").glob(".tmp_*"))  # 无残留临时文件
    text = p.read_text(encoding="utf-8")
    assert "kind: compile" in text and "RAW/a.md" in text
    # 清理修复前旧版本可能写入真实 vault/_triggers/ 的残留（若有）
    for stale in (ROOT / "vault" / "_triggers").glob("compile_*.md"):
        stale.unlink()

def test_validate_upload():
    assert validate_upload("a.md", 1024) is None
    assert validate_upload("a.jpg", 1024) is not None
    assert validate_upload("a.pdf", 11 * 1024 * 1024) is not None

def test_approve_entry_moves_and_double_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sqlite3.connect(tmp_path / "t.db").executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    (tmp_path / "pending_review").mkdir(); (tmp_path / "NEXUS" / "概念").mkdir(parents=True)
    src = tmp_path / "pending_review" / "示例监测产品.md"
    src.write_text("---\ntype: concept\ntitle: 示例监测产品\nstatus: pending\n---\n正文", encoding="utf-8")
    import importlib, db, ops
    db = importlib.reload(db); ops = importlib.reload(ops)
    db.upsert_entry("pending_review/示例监测产品.md", "concept", "示例监测产品", "产品", "pending", "V1.0", None, "2026-08-13")
    rid = db.insert_review("pending_review/示例监测产品.md", "demo_user", "产品", "approved", "{}")
    ops.approve_entry(rid, "pending_review/示例监测产品.md", "NEXUS/概念/示例监测产品.md")
    assert not src.exists()
    assert (tmp_path / "NEXUS" / "概念" / "示例监测产品.md").exists()
    text = (tmp_path / "NEXUS" / "概念" / "示例监测产品.md").read_text(encoding="utf-8")
    assert "status: active" in text
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM knowledge_entries WHERE path='NEXUS/概念/示例监测产品.md'").fetchone()[0] == "active"
        assert conn.execute("SELECT human_decision FROM pending_reviews WHERE id=?", (rid,)).fetchone()[0] == "approved"

def test_reject_entry_drafts_and_records_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sqlite3.connect(tmp_path / "t.db").executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    (tmp_path / "pending_review").mkdir()
    src = tmp_path / "pending_review" / "示例监测产品.md"
    src.write_text("---\ntype: concept\ntitle: 示例监测产品\nstatus: pending\n---\n正文", encoding="utf-8")
    import importlib, db, ops
    db = importlib.reload(db); ops = importlib.reload(ops)
    db.upsert_entry("pending_review/示例监测产品.md", "concept", "示例监测产品", "产品", "pending", "V1.0", None, "2026-08-13")
    rid = db.insert_review("pending_review/示例监测产品.md", "demo_user", "产品", "approved", "{}")
    ops.reject_entry(rid, "pending_review/示例监测产品.md", "内容与已有条目重复")
    assert src.exists()  # 驳回不移动文件
    assert "status: draft" in src.read_text(encoding="utf-8")
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM knowledge_entries WHERE path='pending_review/示例监测产品.md'").fetchone()[0] == "draft"
        row = conn.execute("SELECT human_decision, reject_reason FROM pending_reviews WHERE id=?", (rid,)).fetchone()
        assert row == ("rejected", "内容与已有条目重复")

def test_resubmit_returns_to_pending_and_clears_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sqlite3.connect(tmp_path / "t.db").executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    (tmp_path / "pending_review").mkdir()
    src = tmp_path / "pending_review" / "示例监测产品.md"
    src.write_text("---\ntype: concept\ntitle: 示例监测产品\nstatus: draft\n---\n正文", encoding="utf-8")
    import importlib, db, ops
    db = importlib.reload(db); ops = importlib.reload(ops)
    db.upsert_entry("pending_review/示例监测产品.md", "concept", "示例监测产品", "产品", "draft", "V1.0", None, "2026-08-13")
    rid = db.insert_review("pending_review/示例监测产品.md", "demo_user", "产品", "approved", "{}")
    db.set_human_decision(rid, "rejected", "原因")
    ops.resubmit(rid, "pending_review/示例监测产品.md")
    assert src.exists()  # 重新提交不移动文件
    assert "status: pending" in src.read_text(encoding="utf-8")
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM knowledge_entries WHERE path='pending_review/示例监测产品.md'").fetchone()[0] == "pending"
        row = conn.execute("SELECT human_decision, reject_reason FROM pending_reviews WHERE id=?", (rid,)).fetchone()
        assert row == (None, None)  # human_decision 清空、reject_reason 清空
