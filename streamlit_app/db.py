"""SQLite 数据访问层（设计文档 9.2）。所有表操作唯一入口。"""
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "vault", "meta.db"))
KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """SQLite 连接上下文。WAL 模式，busy_timeout=5s。"""
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_entry(path: str, type_: str, title: str,
                 department: str | None, status: str,
                 version: str, fingerprint: str | None,
                 updated_at: str) -> None:
    """INSERT OR REPLACE INTO knowledge_entries。"""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO knowledge_entries
               (path, type, title, department, status, version, fingerprint, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (path, type_, title, department, status, version, fingerprint, updated_at))


def update_status(path: str, status: str) -> None:
    """更新 knowledge_entries.status（不触碰文件，文件由调用方改）。"""
    with get_conn() as conn:
        conn.execute("UPDATE knowledge_entries SET status=? WHERE path=?", (status, path))


def move_entry(old_path: str, new_path: str, status: str) -> None:
    """DELETE 旧 path 行 + INSERT 新 path 行（保留原字段）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type, title, department, version, fingerprint, updated_at "
            "FROM knowledge_entries WHERE path=?", (old_path,)).fetchone()
        if row is None:
            raise KeyError(f"knowledge_entries 无此路径: {old_path}")
        conn.execute("DELETE FROM knowledge_entries WHERE path=?", (old_path,))
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, department, status, version, fingerprint, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (new_path, row[0], row[1], row[2], status, row[3], row[4], row[5]))
