"""PostgreSQL 数据访问层（Phase 2 SP1：SQLite → psycopg3）。所有表操作唯一入口。

对外接口签名与 Phase 1 SQLite 版保持一致（36 个函数/工具被 app/ops/review/growth/upload 依赖），
仅内部实现切换为 PostgreSQL 16 + pgvector + psycopg_pool 连接池。"""
import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

# PostgreSQL 连接（替代 Demo 的 DB_PATH）
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5432"),
    "dbname": os.environ.get("DB_NAME", "llmwiki"),
    "user": os.environ.get("DB_USER", "llmwiki"),
    "password": os.environ.get("DB_PASS", "llmwiki"),
}
KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))

# schema.sql 所在目录（仓库根 = 本文件 ../）
_SCHEMA = os.path.join(os.path.dirname(__file__), "..", "schema.sql")

# KB 启动必须存在的目录（与 init.sh 一致，clone 后部分目录不入库，需自愈）
_REQUIRED_DIRS = [
    "RAW/个人_notes", "RAW/会议", "RAW/经验", "RAW/项目",
    "pending_review",
    "NEXUS/资源", "NEXUS/概念", "NEXUS/研究",
    "_triggers", "_triggers/done",
]

_pool: ConnectionPool | None = None


def _dsn() -> str:
    return " ".join(f"{k}={v}" for k, v in DB_CONFIG.items())


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo=_dsn(), min_size=1, max_size=5, open=True)
    return _pool


def close_pool() -> None:
    """测试/退出时释放连接池。"""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ensure_schema() -> None:
    """自愈初始化：确保 Vault 目录树存在 + PostgreSQL 建表（幂等）。

    供 Streamlit 启动时调用——即使跳过 init.sh 也能安全运行；
    clone 后空目录/缺失目录在此补齐。"""
    for rel in _REQUIRED_DIRS:
        os.makedirs(os.path.join(KB_ROOT, rel), exist_ok=True)
    with open(_SCHEMA, encoding="utf-8") as f:
        ddl = f.read()
    with get_conn() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(ddl)


@contextmanager
def get_conn() -> Iterator["psycopg.Connection"]:
    """PostgreSQL 连接上下文（连接池）。

    psycopg_pool 的 pooled connection 上下文已管理事务生命周期：
    正常退出自动 commit，异常退出自动 rollback；归还池连接无需手动 close。"""
    with _get_pool().connection() as conn:
        yield conn


def _colnames(conn, table: str) -> list[str]:
    """取表全列名（供 list_* 组装 dict）。psycopg3 description.name。"""
    cur = conn.execute(f"SELECT * FROM {table} LIMIT 0")
    return [d.name for d in cur.description]


# ---- 知识条目 ----
def upsert_entry(path: str, type_: str, title: str,
                 department: str | None, status: str,
                 version: str, fingerprint: str | None,
                 updated_at: str) -> None:
    """UPSERT INTO knowledge_entries（path 冲突则更新全字段）。"""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO knowledge_entries
               (path, type, title, department, status, version, fingerprint, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (path) DO UPDATE SET
                 type=EXCLUDED.type, title=EXCLUDED.title, department=EXCLUDED.department,
                 status=EXCLUDED.status, version=EXCLUDED.version,
                 fingerprint=EXCLUDED.fingerprint, updated_at=EXCLUDED.updated_at""",
            (path, type_, title, department, status, version, fingerprint, updated_at))


def update_status(path: str, status: str) -> None:
    """更新 knowledge_entries.status（不触碰文件，文件由调用方改）。"""
    with get_conn() as conn:
        conn.execute("UPDATE knowledge_entries SET status=%s WHERE path=%s", (status, path))


def move_entry(old_path: str, new_path: str, status: str) -> None:
    """DELETE 旧 path 行 + INSERT 新 path 行（保留原字段）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type, title, department, version, fingerprint, updated_at "
            "FROM knowledge_entries WHERE path=%s", (old_path,)).fetchone()
        if row is None:
            raise KeyError(f"knowledge_entries 无此路径: {old_path}")
        conn.execute("DELETE FROM knowledge_entries WHERE path=%s", (old_path,))
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, department, status, version, fingerprint, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (new_path, row[0], row[1], row[2], status, row[3], row[4], row[5]))


# ---- 编译任务 ----
def insert_compile_task(raw_path: str, fingerprint: str) -> int:
    """插入 status='pending' 任务，返回 id。"""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status, started_at) "
            "VALUES (%s,%s,'pending',now()) RETURNING id",
            (raw_path, fingerprint)).fetchone()[0]


def update_compile_task(task_id: int, status: str,
                        nexus_path: str | None = None,
                        error_msg: str | None = None) -> None:
    """更新任务状态与完成时间。"""
    with get_conn() as conn:
        if status in ("done", "failed", "cached"):
            conn.execute(
                "UPDATE compile_tasks SET status=%s, nexus_path=COALESCE(%s,nexus_path), error_msg=%s, completed_at=now() WHERE id=%s",
                (status, nexus_path, error_msg, task_id))
        else:
            conn.execute("UPDATE compile_tasks SET status=%s WHERE id=%s", (status, task_id))


def list_recent_compile_tasks(limit: int = 50) -> list[dict]:
    """最近的编译任务（upload 页状态表）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, raw_path, status, fingerprint, error_msg, completed_at "
            "FROM compile_tasks ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
        return [{"id": r[0], "raw_path": r[1], "status": r[2], "fingerprint": r[3],
                 "error_msg": r[4], "completed_at": r[5]} for r in rows]


# ---- 审核 ----
def insert_review(nexus_path: str, submitter: str, department: str,
                  ai_verdict: str, ai_scores: str) -> int:
    """插入 AI 审核结果，返回 id。ai_scores 为六维度 JSON 字符串，DB 列 JSONB 自动转换。"""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO pending_reviews (nexus_path, submitter, department, ai_verdict, ai_scores, created_at) "
            "VALUES (%s,%s,%s,%s,%s,now()) RETURNING id",
            (nexus_path, submitter, department, ai_verdict, ai_scores)).fetchone()[0]


def set_human_decision(review_id: int, decision: str,
                       reject_reason: str | None = None) -> None:
    """人工通过/驳回。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pending_reviews SET human_decision=%s, reject_reason=%s WHERE id=%s",
            (decision, reject_reason, review_id))


def resubmit_review(review_id: int) -> None:
    """重新提交审核：human_decision 置 NULL、清空 reject_reason。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pending_reviews SET human_decision=NULL, reject_reason=NULL WHERE id=%s",
            (review_id,))


def list_pending_reviews() -> list[dict]:
    """human_decision IS NULL 的审核记录（含条目标题）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pr.*, e.title FROM pending_reviews pr "
            "LEFT JOIN knowledge_entries e ON e.path=pr.nexus_path "
            "WHERE pr.human_decision IS NULL ORDER BY pr.created_at DESC").fetchall()
        cols = _colnames(conn, "pending_reviews") + ["title"]
        return [dict(zip(cols, r)) for r in rows]


def list_rejected_reviews() -> list[dict]:
    """human_decision='rejected' 的记录（可重新提交）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pr.*, e.title FROM pending_reviews pr "
            "LEFT JOIN knowledge_entries e ON e.path=pr.nexus_path "
            "WHERE pr.human_decision='rejected' ORDER BY pr.created_at DESC").fetchall()
        cols = _colnames(conn, "pending_reviews") + ["title"]
        return [dict(zip(cols, r)) for r in rows]


# ---- 搜索日志与看板 ----
def insert_search_log(query: str, match_count: int, source: str) -> None:
    """写入搜索日志（timestamp 本地时间）。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO search_logs (query, match_count, source, timestamp) VALUES (%s,%s,%s,now())",
            (query, match_count, source))


def top_missed_queries(limit: int = 20) -> list[dict]:
    """match_count=0 的 query 按次数降序。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT query, COUNT(*) AS cnt, MAX(timestamp) AS last_seen "
            "FROM search_logs WHERE match_count=0 GROUP BY query ORDER BY cnt DESC, last_seen DESC LIMIT %s",
            (limit,)).fetchall()
        return [{"query": r[0], "cnt": r[1], "last_seen": r[2]} for r in rows]


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
                        "INSERT INTO knowledge_entries "
                        "(path, type, title, department, status, version, fingerprint, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (path) DO UPDATE SET "
                        "type=EXCLUDED.type, title=EXCLUDED.title, department=EXCLUDED.department, "
                        "status=EXCLUDED.status, version=EXCLUDED.version, "
                        "fingerprint=EXCLUDED.fingerprint, updated_at=EXCLUDED.updated_at",
                        (rel, meta.get("type", "concept"), meta.get("title", fn[:-3]),
                         meta.get("department"), meta.get("status", "active"),
                         meta.get("version", "V1.0"), meta.get("fingerprint"),
                         meta.get("updated", meta.get("created"))))
                    count += 1
    return count
