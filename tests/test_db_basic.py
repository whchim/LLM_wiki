import sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))
os.environ.setdefault("DB_PATH", str(ROOT / "vault" / "meta.db"))
os.environ.setdefault("KB_ROOT", str(ROOT / "vault"))

from db import get_conn, upsert_entry, update_status, move_entry
from db import DB_PATH  # 模块内基于 DB_PATH 初始化

def _fresh_db(tmp_path):
    import sqlite3
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript(Path(ROOT / "schema.sql").read_text(encoding="utf-8"))
    os.environ["DB_PATH"] = str(db)
    import importlib, db as m
    return importlib.reload(m), db

def test_upsert_entry_inserts_and_updates(tmp_path):
    m, db = _fresh_db(tmp_path)
    m.upsert_entry("NEXUS/概念/示例监测产品.md", "concept", "示例监测产品", "产品", "pending", "V1.0", "abc", "2026-08-13")
    m.upsert_entry("NEXUS/概念/示例监测产品.md", "concept", "示例监测产品", "产品", "active", "V1.0", "abc", "2026-08-13")
    with m.get_conn() as conn:
        rows = conn.execute("SELECT status FROM knowledge_entries WHERE path=?", ("NEXUS/概念/示例监测产品.md",)).fetchall()
    assert rows == [("active",)]  # upsert 不产生重复行

def test_update_status(tmp_path):
    m, db = _fresh_db(tmp_path)
    m.upsert_entry("p.md", "concept", "x", None, "pending", "V1.0", None, "2026-08-13")
    m.update_status("p.md", "active")
    with m.get_conn() as conn:
        assert conn.execute("SELECT status FROM knowledge_entries WHERE path='p.md'").fetchone()[0] == "active"

def test_move_entry_changes_path_keeps_row_count(tmp_path):
    m, db = _fresh_db(tmp_path)
    m.upsert_entry("pending_review/示例监测产品.md", "concept", "示例监测产品", "产品", "pending", "V1.0", None, "2026-08-13")
    m.move_entry("pending_review/示例监测产品.md", "NEXUS/概念/示例监测产品.md", "active")
    with m.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
        assert n == 1
        row = conn.execute("SELECT path, status FROM knowledge_entries").fetchone()
        assert row == ("NEXUS/概念/示例监测产品.md", "active")
