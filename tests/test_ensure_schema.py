import sys, os, sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import importlib


def _fresh_db(tmp_path, monkeypatch):
    """全新 KB 环境（任意目录无需预建 meta.db），reload db 以读新 env。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "vault" / "meta.db"))
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    import db as m
    return importlib.reload(m)


def test_ensure_schema_creates_dirs_and_tables_in_fresh_clone(tmp_path, monkeypatch):
    """模拟 fresh clone：vault/ 无表、目录可能缺失 → ensure_schema 自愈。"""
    m = _fresh_db(tmp_path, monkeypatch)
    m.ensure_schema()
    # 目录树补齐
    for rel in ("RAW/个人_notes", "RAW/会议", "RAW/经验", "RAW/项目",
                "pending_review", "NEXUS/资源", "NEXUS/概念", "NEXUS/研究",
                "_triggers", "_triggers/done"):
        assert (tmp_path / "vault" / rel).is_dir(), f"缺失目录 {rel}"
    # 表已建
    with m.get_conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"knowledge_entries", "compile_tasks", "pending_reviews", "search_logs"} <= tables


def test_ensure_schema_is_idempotent(tmp_path, monkeypatch):
    """重复调用不报错、不产生重复业务表（排除 SQLite 内部表 sqlite_sequence）。"""
    m = _fresh_db(tmp_path, monkeypatch)
    m.ensure_schema()
    m.ensure_schema()
    with m.get_conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables == {"knowledge_entries", "compile_tasks", "pending_reviews", "search_logs"}
