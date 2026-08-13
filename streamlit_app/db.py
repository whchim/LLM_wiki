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


# ---- 编译任务 ----
def insert_compile_task(raw_path: str, fingerprint: str) -> int:
    """插入 status='pending' 任务，返回 id。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status, started_at) VALUES (?,?,'pending',datetime('now','localtime'))",
            (raw_path, fingerprint))
        return cur.lastrowid


def update_compile_task(task_id: int, status: str,
                        nexus_path: str | None = None,
                        error_msg: str | None = None) -> None:
    """更新任务状态与完成时间。"""
    with get_conn() as conn:
        if status in ("done", "failed", "cached"):
            conn.execute(
                "UPDATE compile_tasks SET status=?, nexus_path=COALESCE(?,nexus_path), error_msg=?, completed_at=datetime('now','localtime') WHERE id=?",
                (status, nexus_path, error_msg, task_id))
        else:
            conn.execute("UPDATE compile_tasks SET status=? WHERE id=?", (status, task_id))


# ---- 审核 ----
def insert_review(nexus_path: str, submitter: str, department: str,
                  ai_verdict: str, ai_scores: str) -> int:
    """插入 AI 审核结果，返回 id。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO pending_reviews (nexus_path, submitter, department, ai_verdict, ai_scores, created_at) "
            "VALUES (?,?,?,?,?,datetime('now','localtime'))",
            (nexus_path, submitter, department, ai_verdict, ai_scores))
        return cur.lastrowid


def set_human_decision(review_id: int, decision: str,
                       reject_reason: str | None = None) -> None:
    """人工通过/驳回。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pending_reviews SET human_decision=?, reject_reason=? WHERE id=?",
            (decision, reject_reason, review_id))


def resubmit_review(review_id: int) -> None:
    """重新提交审核：human_decision 置 NULL、清空 reject_reason。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pending_reviews SET human_decision=NULL, reject_reason=NULL WHERE id=?",
            (review_id,))


def list_pending_reviews() -> list[dict]:
    """human_decision IS NULL 的审核记录（含条目标题）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pr.*, e.title FROM pending_reviews pr "
            "LEFT JOIN knowledge_entries e ON e.path=pr.nexus_path "
            "WHERE pr.human_decision IS NULL ORDER BY pr.created_at DESC").fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM pending_reviews LIMIT 0").description]
        return [dict(zip(cols + ["title"], r)) for r in rows]


def list_rejected_reviews() -> list[dict]:
    """human_decision='rejected' 的记录（可重新提交）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pr.*, e.title FROM pending_reviews pr "
            "LEFT JOIN knowledge_entries e ON e.path=pr.nexus_path "
            "WHERE pr.human_decision='rejected' ORDER BY pr.created_at DESC").fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM pending_reviews LIMIT 0").description]
        return [dict(zip(cols + ["title"], r)) for r in rows]


# ---- 搜索日志与看板 ----
def insert_search_log(query: str, match_count: int, source: str) -> None:
    """写入搜索日志（timestamp 本地时间）。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO search_logs (query, match_count, source, timestamp) VALUES (?,?,?,datetime('now','localtime'))",
            (query, match_count, source))


def top_missed_queries(limit: int = 20) -> list[dict]:
    """match_count=0 的 query 按次数降序。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT query, COUNT(*) AS cnt, MAX(timestamp) AS last_seen "
            "FROM search_logs WHERE match_count=0 GROUP BY query ORDER BY cnt DESC, last_seen DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(zip(["query", "cnt", "last_seen"], r)) for r in rows]


def search_stats() -> dict:
    """{total, miss_count, miss_rate}。"""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
        miss = conn.execute("SELECT COUNT(*) FROM search_logs WHERE match_count=0").fetchone()[0]
        return {"total": total, "miss_count": miss, "miss_rate": round(miss / total, 2) if total else 0.0}


# ---- 重建索引 ----
def rebuild_index() -> int:
    """扫描 KB_ROOT 下 NEXUS/**/*.md 与 pending_review/*.md，解析 YAML 重建表。返回条目数。

    单文件损坏（无完整 frontmatter / YAML 非法 / frontmatter 非映射）仅跳过该文件，
    不影响其余条目重建。"""
    import yaml
    count = 0
    with get_conn() as conn:
        conn.execute("DELETE FROM knowledge_entries")
        for base in ("NEXUS", "pending_review"):
            base_dir = os.path.join(KB_ROOT, base)
            for dirpath, _, files in os.walk(base_dir):
                for fn in files:
                    if not fn.endswith(".md"):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, KB_ROOT).replace("\\", "/")
                    with open(full, encoding="utf-8") as f:
                        text = f.read()
                    if not text.startswith("---"):
                        continue
                    parts = text.split("---", 2)
                    if len(parts) < 3:  # 以 --- 开头但无第二个 ---：frontmatter 不完整
                        continue
                    try:
                        meta = yaml.safe_load(parts[1])
                    except yaml.YAMLError:
                        continue
                    if not isinstance(meta, dict):  # frontmatter 解析为列表/字符串等非映射
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO knowledge_entries "
                        "(path, type, title, department, status, version, fingerprint, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (rel, meta.get("type", "concept"), meta.get("title", fn[:-3]),
                         meta.get("department"), meta.get("status", "active"),
                         meta.get("version", "V1.0"), meta.get("fingerprint"),
                         meta.get("updated", meta.get("created"))))
                    count += 1
    return count
