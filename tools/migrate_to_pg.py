#!/usr/bin/env python3
"""Phase 2 SP1 一次性旧数据导入脚本：SQLite meta.db → PostgreSQL。

策略（设计文档 8 节）：
- knowledge_entries：走 db.rebuild_index() 从 YAML 重建（YAML 为权威，丢弃缓存层陈旧字段）
- search_logs / pending_reviews / compile_tasks：从旧 SQLite dump 导入 PG（不可重建历史，保留时间戳）
- 新表 audit_logs / contributors / conflicts：Demo 无数据，不迁移

用法：
    python tools/migrate_to_pg.py [--sqlite vault/meta.db]
要求：PostgreSQL 已启动且 schema 已建（ensure_schema），生产库已可连。
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import psycopg

import db


def _pg_conninfo() -> str:
    cfg = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("DB_PORT", "5432"),
        "dbname": os.environ.get("DB_NAME", "llmwiki"),
        "user": os.environ.get("DB_USER", "llmwiki"),
        "password": os.environ.get("DB_PASS", "llmwiki"),
    }
    return " ".join(f"{k}={v}" for k, v in cfg.items())


def _import_table(conn, cur, table: str, cols: list[str]) -> int:
    rows = conn.execute(f"SELECT {','.join(cols)} FROM {table}").fetchall()
    colsql = ",".join(cols)
    placeholders = ",".join(["%s"] * len(cols))
    n = 0
    for row in rows:
        cur.execute(f"INSERT INTO {table} ({colsql}) VALUES ({placeholders})", tuple(row))
        n += 1
    return n


def _import_history(sqlite_path: Path) -> dict:
    """导入不可重建的三张历史表，返回 {表名: 行数}。"""
    sconn = sqlite3.connect(str(sqlite_path))
    sconn.row_factory = None
    counts = {}
    with psycopg.connect(_pg_conninfo()) as pconn:
        with pconn.cursor() as cur:
            counts["compile_tasks"] = _import_table(
                sconn, cur, "compile_tasks",
                ["raw_path", "nexus_path", "fingerprint", "status", "error_msg",
                 "started_at", "completed_at"])
            counts["pending_reviews"] = _import_table(
                sconn, cur, "pending_reviews",
                ["nexus_path", "submitter", "department", "ai_verdict", "ai_scores",
                 "human_decision", "reject_reason", "created_at"])
            counts["search_logs"] = _import_table(
                sconn, cur, "search_logs",
                ["query", "match_count", "source", "timestamp"])
    sconn.close()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite → PostgreSQL 一次性迁移")
    ap.add_argument("--sqlite", default=str(ROOT / "vault" / "meta.db"))
    args = ap.parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"[skip] 未找到旧 SQLite：{sqlite_path}")
        return 0

    db.ensure_schema()  # 确保生产库表已建

    print("[1/2] 从 YAML 重建 knowledge_entries（rebuild_index）...")
    n_entries = db.rebuild_index()
    print(f"      → 重建 {n_entries} 条目")

    print("[2/2] 导入不可重建历史表（search_logs/pending_reviews/compile_tasks）...")
    counts = _import_history(sqlite_path)
    for table, n in counts.items():
        print(f"      → {table}: {n} 行")

    print("完成。SQLite 元数据已迁移至 PostgreSQL；vault/meta.db 可退役。")
    db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
