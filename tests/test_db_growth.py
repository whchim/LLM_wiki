import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import db


def test_search_logs_aggregation(tmp_path):
    db.insert_search_log("区块链", 0, "streamlit")
    db.insert_search_log("区块链", 0, "claude_code")
    db.insert_search_log("示例监测产品", 3, "streamlit")
    top = db.top_missed_queries(10)
    assert top[0]["query"] == "区块链" and top[0]["cnt"] == 2
    stats = db.search_stats()
    assert stats["total"] == 3 and stats["miss_count"] == 2


def test_rebuild_index_from_files(tmp_path, monkeypatch):
    # conftest 已把 KB_ROOT 指到临时 vault；在此构造目录与文件
    kb = tmp_path / "vault"
    (kb / "NEXUS" / "概念").mkdir(parents=True, exist_ok=True)
    (kb / "NEXUS" / "资源").mkdir(parents=True, exist_ok=True)
    (kb / "pending_review").mkdir(exist_ok=True)
    (kb / "NEXUS" / "概念" / "示例监测产品.md").write_text(
        "---\ntype: concept\ntitle: 示例监测产品\nstatus: active\ndepartment: 产品\n---\n正文", encoding="utf-8")
    (kb / "NEXUS" / "资源" / "白皮书.md").write_text(
        "---\ntype: resource\ntitle: 白皮书\nstatus: active\n---\n正文", encoding="utf-8")
    (kb / "pending_review" / "叫应体系.md").write_text(
        "---\ntype: concept\ntitle: 叫应体系\nstatus: pending\n---\n正文", encoding="utf-8")
    n = db.rebuild_index()
    assert n == 3
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0] == 3
