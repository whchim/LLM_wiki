import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def test_schema_creates_expected_tables(tmp_path):
    db = tmp_path / "meta.db"
    subprocess.run(["sqlite3", str(db), f".read {ROOT / 'schema.sql'}"], check=True,
                   capture_output=True, text=True)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"knowledge_entries", "compile_tasks", "pending_reviews", "search_logs"} <= tables

def test_schema_is_idempotent(tmp_path):
    db = tmp_path / "meta.db"
    for _ in range(2):
        subprocess.run(["sqlite3", str(db), f".read {ROOT / 'schema.sql'}"], check=True,
                       capture_output=True, text=True)
    conn = sqlite3.connect(db)
    # 排除 SQLite 因 AUTOINCREMENT 自动创建的内部表 sqlite_sequence
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
    ).fetchone()[0]
    assert n == 4
