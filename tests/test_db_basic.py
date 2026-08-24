import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

# conftest 的 autouse _env 已在每个测试前重载 db 并重置测试库 schema
import db


def test_upsert_entry_inserts_and_updates(tmp_path):
    db.upsert_entry("NEXUS/概念/示例监测产品.md", "concept", "示例监测产品", "产品", "pending", "V1.0", "abc", "2026-08-13")
    db.upsert_entry("NEXUS/概念/示例监测产品.md", "concept", "示例监测产品", "产品", "active", "V1.0", "abc", "2026-08-13")
    with db.get_conn() as conn:
        rows = conn.execute("SELECT status FROM knowledge_entries WHERE path=%s", ("NEXUS/概念/示例监测产品.md",)).fetchall()
    assert rows == [("active",)]  # upsert 不产生重复行


def test_update_status(tmp_path):
    db.upsert_entry("p.md", "concept", "x", None, "pending", "V1.0", None, "2026-08-13")
    db.update_status("p.md", "active")
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM knowledge_entries WHERE path='p.md'").fetchone()[0] == "active"


def test_move_entry_changes_path_keeps_row_count(tmp_path):
    db.upsert_entry("pending_review/示例监测产品.md", "concept", "示例监测产品", "产品", "pending", "V2.1", "fp123", "2026-08-13")
    db.move_entry("pending_review/示例监测产品.md", "NEXUS/概念/示例监测产品.md", "active")
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
        assert n == 1
        row = conn.execute("SELECT path, type, title, department, status, version, fingerprint, updated_at FROM knowledge_entries").fetchone()
        assert row == ("NEXUS/概念/示例监测产品.md", "concept", "示例监测产品", "产品", "active", "V2.1", "fp123", "2026-08-13")


def test_move_entry_missing_path_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        db.move_entry("不存在.md", "NEXUS/概念/新位置.md", "active")
