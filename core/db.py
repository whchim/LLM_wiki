"""PostgreSQL 数据访问层（Phase 2 SP1：SQLite → psycopg3）。所有表操作唯一入口。

对外接口签名与 Phase 1 SQLite 版保持一致（36 个函数/工具被 app/ops/review/growth/upload 依赖），
仅内部实现切换为 PostgreSQL 16 + pgvector + psycopg_pool 连接池。"""
import os
import re
import uuid
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

# PostgreSQL 连接（替代 Demo 的 DB_PATH）
# host 默认 127.0.0.1 而非 localhost：Windows 上 localhost 先解析 IPv6，而 Docker 只发布
# IPv4，每次连接多等 2-5 秒（实测 5202ms vs 49ms）。容器内由 compose 显式传 DB_HOST=db。
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
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
    _ensure_admin()


def _ensure_admin() -> None:
    """users 表为空时创建初始管理员（clone 即用）。

    凭据来自环境变量 ADMIN_INIT_USER / ADMIN_INIT_PASS（默认 admin/admin123），
    密码以 argon2 哈希落库。pwdlib 局部 import——数据层不为此引入顶层依赖。"""
    import pwdlib
    password_hash = pwdlib.PasswordHash.recommended()

    user = os.environ.get("ADMIN_INIT_USER", "admin")
    password = os.environ.get("ADMIN_INIT_PASS", "admin123")
    if os.environ.get("APP_ENV", "development").lower() in {"prod", "production"}:
        if password == "admin123" or len(password) < 12:
            raise RuntimeError("生产环境禁止使用默认或过短的 ADMIN_INIT_PASS（至少 12 个字符）")
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if n > 0:
            return
        conn.execute(
            "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s,%s,'admin',%s)",
            (user, password_hash.hash(password), "系统管理员"))


@contextmanager
def get_conn() -> Iterator["psycopg.Connection"]:
    """PostgreSQL 连接上下文（连接池）。

    psycopg_pool 的 pooled connection 上下文已管理事务生命周期：
    正常退出自动 commit，异常退出自动 rollback；归还池连接无需手动 close。"""
    with _get_pool().connection() as conn:
        yield conn


# ---- 销售事实澄清会话 ----
def _clarification_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def create_clarification_session(conversation_id: str, created_by: str, max_rounds: int = 2) -> dict:
    """为已有销售纪要创建幂等澄清会话；不复制或修改原始证据。"""
    if not conversation_id.strip() or not created_by.strip() or max_rounds not in (1, 2):
        raise ValueError("conversation_id、created_by 或 max_rounds 不合法")
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT session_id, conversation_id, status, round_count, max_rounds, created_by, created_at, updated_at "
            "FROM clarification_sessions WHERE conversation_id=%s", (conversation_id,)).fetchone()
        if existing:
            keys = ("session_id", "conversation_id", "status", "round_count", "max_rounds", "created_by", "created_at", "updated_at")
            return dict(zip(keys, existing))
        if conn.execute("SELECT 1 FROM conversations WHERE conversation_id=%s", (conversation_id,)).fetchone() is None:
            raise KeyError("conversation 不存在")
        session_id = _clarification_id("clar")
        row = conn.execute(
            "INSERT INTO clarification_sessions (session_id, conversation_id, max_rounds, created_by) "
            "VALUES (%s,%s,%s,%s) RETURNING session_id, conversation_id, status, round_count, max_rounds, created_by, created_at, updated_at",
            (session_id, conversation_id, max_rounds, created_by)).fetchone()
        keys = ("session_id", "conversation_id", "status", "round_count", "max_rounds", "created_by", "created_at", "updated_at")
        return dict(zip(keys, row))


def get_clarification_session_for_conversation(conversation_id: str) -> dict | None:
    """按纪要查会话，用于提交幂等检查，避免重复追加证据。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id, conversation_id, status, round_count, max_rounds, created_by, created_at, updated_at "
            "FROM clarification_sessions WHERE conversation_id=%s", (conversation_id,)).fetchone()
    if row is None:
        return None
    keys = ("session_id", "conversation_id", "status", "round_count", "max_rounds", "created_by", "created_at", "updated_at")
    return dict(zip(keys, row))


def get_conversation_by_idempotency_key(idempotency_key: str) -> dict | None:
    """读取幂等输入，不触发客户负责人或任何业务字段更新。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT conversation_id, customer_id, idempotency_key, source_type, occurred_at, submitted_by, "
            "source_ref, processing_status, created_at FROM conversations WHERE idempotency_key=%s",
            (idempotency_key,)).fetchone()
    if row is None:
        return None
    keys = ("conversation_id", "customer_id", "idempotency_key", "source_type", "occurred_at", "submitted_by",
            "source_ref", "processing_status", "created_at")
    return dict(zip(keys, row))


def latest_evidence(conversation_id: str) -> dict | None:
    """返回最近一条脱敏证据，供幂等提交响应展示。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT evidence_id, conversation_id, content_hash, source_ref, created_at "
            "FROM evidence WHERE conversation_id=%s ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
    if row is None:
        return None
    keys = ("evidence_id", "conversation_id", "content_hash", "source_ref", "created_at")
    return dict(zip(keys, row))


def can_access_conversation(conversation_id: str, username: str) -> bool:
    """普通用户仅能访问自己提交或负责的销售纪要。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM conversations c LEFT JOIN customers cu ON cu.customer_id=c.customer_id "
            "WHERE c.conversation_id=%s AND (c.submitted_by=%s OR cu.owner_user_id=%s)",
            (conversation_id, username, username)).fetchone() is not None


def can_access_clarification_session(session_id: str, username: str) -> bool:
    """普通用户仅能访问自己创建的澄清会话。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM clarification_sessions WHERE session_id=%s AND created_by=%s",
            (session_id, username)).fetchone() is not None


def get_clarification_session(session_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id, conversation_id, status, round_count, max_rounds, created_by, created_at, updated_at "
            "FROM clarification_sessions WHERE session_id=%s", (session_id,)).fetchone()
        if row is None:
            return None
        keys = ("session_id", "conversation_id", "status", "round_count", "max_rounds", "created_by", "created_at", "updated_at")
        result = dict(zip(keys, row))
        result["turns"] = list_clarification_turns(session_id)
        result["answers"] = list_clarification_answers(session_id)
        return result


def list_clarification_turns(session_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT turn_id, session_id, turn_no, status, agent_output, question_count, input_tokens, output_tokens, latency_ms, created_at "
            "FROM clarification_turns WHERE session_id=%s ORDER BY turn_no", (session_id,)).fetchall()
    keys = ("turn_id", "session_id", "turn_no", "status", "agent_output", "question_count", "input_tokens", "output_tokens", "latency_ms", "created_at")
    return [dict(zip(keys, row)) for row in rows]


def append_clarification_turn(session_id: str, status: str, agent_output: dict,
                              question_count: int, input_tokens: int | None = None,
                              output_tokens: int | None = None, latency_ms: int | None = None) -> dict:
    """追加一轮 Agent 输出；轮次达到上限后自动关闭会话，不允许无限追问。"""
    allowed = {"needs_clarification", "ready_for_proposal", "insufficient_evidence", "human_review"}
    if status not in allowed or not isinstance(agent_output, dict) or not 0 <= question_count <= 2:
        raise ValueError("turn 参数不合法")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT round_count, max_rounds, status FROM clarification_sessions WHERE session_id=%s FOR UPDATE",
            (session_id,)).fetchone()
        if row is None:
            raise KeyError("澄清会话不存在")
        round_count, max_rounds, session_status = row
        if session_status != "open":
            raise ValueError("澄清会话已关闭")
        if round_count >= max_rounds:
            raise ValueError("已达到最大澄清轮次")
        turn_no = round_count + 1
        turn_id = _clarification_id("turn")
        turn = conn.execute(
            "INSERT INTO clarification_turns (turn_id, session_id, turn_no, status, agent_output, question_count, input_tokens, output_tokens, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING turn_id, session_id, turn_no, status, agent_output, question_count, input_tokens, output_tokens, latency_ms, created_at",
            (turn_id, session_id, turn_no, status, Jsonb(agent_output), question_count, input_tokens, output_tokens, latency_ms)).fetchone()
        # 最后一轮仍有缺口时明确转人工，避免无限追问。
        # turn 与 session 的状态词表不同（turn 用 human_review，session 用 needs_human_review），
        # 必须显式映射，否则会话侧会撞 CHECK 约束。
        next_status = (
            "open" if status == "needs_clarification" and turn_no < max_rounds
            else "needs_human_review" if status in ("needs_clarification", "human_review")
            else status
        )
        conn.execute("UPDATE clarification_sessions SET round_count=%s, status=%s, updated_at=now() WHERE session_id=%s",
                     (turn_no, next_status, session_id))
    keys = ("turn_id", "session_id", "turn_no", "status", "agent_output", "question_count", "input_tokens", "output_tokens", "latency_ms", "created_at")
    return dict(zip(keys, turn))


def add_clarification_answer(session_id: str, turn_id: str, question_id: str,
                             answer_text_redacted: str, submitted_by: str) -> dict:
    """追加脱敏回答；同一问题幂等，回答不能为空且长度受限。"""
    if not all(isinstance(value, str) and value.strip() for value in (session_id, turn_id, question_id, answer_text_redacted, submitted_by)):
        raise ValueError("回答字段不能为空")
    if len(answer_text_redacted) > 2_000:
        raise ValueError("回答超过 2000 字符")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM clarification_sessions WHERE session_id=%s AND status='open'", (session_id,)).fetchone() is None:
            raise ValueError("澄清会话不存在或已关闭")
        turn = conn.execute("SELECT agent_output FROM clarification_turns WHERE turn_id=%s AND session_id=%s AND status='needs_clarification'",
                            (turn_id, session_id)).fetchone()
        if turn is None:
            raise ValueError("问题轮次不存在或不可回答")
        questions = turn[0].get("questions", []) if isinstance(turn[0], dict) else []
        if question_id not in {item.get("id") for item in questions if isinstance(item, dict)}:
            raise ValueError("question_id 不属于该轮澄清问题")
        existing = conn.execute(
            "SELECT answer_id, session_id, turn_id, question_id, answer_text_redacted, submitted_by, created_at "
            "FROM clarification_answers WHERE turn_id=%s AND question_id=%s", (turn_id, question_id)).fetchone()
        if existing:
            keys = ("answer_id", "session_id", "turn_id", "question_id", "answer_text_redacted", "submitted_by", "created_at")
            return dict(zip(keys, existing))
        answer_id = _clarification_id("answer")
        row = conn.execute(
            "INSERT INTO clarification_answers (answer_id, session_id, turn_id, question_id, answer_text_redacted, submitted_by) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING answer_id, session_id, turn_id, question_id, answer_text_redacted, submitted_by, created_at",
            (answer_id, session_id, turn_id, question_id, answer_text_redacted, submitted_by)).fetchone()
    keys = ("answer_id", "session_id", "turn_id", "question_id", "answer_text_redacted", "submitted_by", "created_at")
    return dict(zip(keys, row))


def list_clarification_answers(session_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT answer_id, session_id, turn_id, question_id, answer_text_redacted, submitted_by, created_at "
            "FROM clarification_answers WHERE session_id=%s ORDER BY created_at", (session_id,)).fetchall()
    keys = ("answer_id", "session_id", "turn_id", "question_id", "answer_text_redacted", "submitted_by", "created_at")
    return [dict(zip(keys, row)) for row in rows]


def list_user_clarification_sessions(username: str, limit: int = 100) -> list[dict]:
    """销售工作台只读取自己创建的会话，返回最新一轮摘要。"""
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.session_id, s.conversation_id, c.customer_id, s.status, s.round_count, "
            "s.max_rounds, s.created_by, s.created_at, s.updated_at "
            "FROM clarification_sessions s JOIN conversations c ON c.conversation_id=s.conversation_id "
            "WHERE s.created_by=%s ORDER BY s.updated_at DESC LIMIT %s", (username, limit)).fetchall()
    keys = ("session_id", "conversation_id", "customer_id", "status", "round_count", "max_rounds",
            "created_by", "created_at", "updated_at")
    return [dict(zip(keys, row)) for row in rows]


def clarification_context(session_id: str) -> dict:
    """返回运行时所需的最小脱敏上下文，不返回受限敏感数值。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.conversation_id, c.customer_id, cs.state, e.content_redacted "
            "FROM clarification_sessions s JOIN conversations c ON c.conversation_id=s.conversation_id "
            "LEFT JOIN current_states cs ON cs.customer_id=c.customer_id "
            "JOIN evidence e ON e.conversation_id=c.conversation_id "
            "WHERE s.session_id=%s ORDER BY e.created_at DESC LIMIT 1", (session_id,)).fetchone()
        if row is None:
            raise KeyError("澄清会话缺少销售证据")
    return {"conversation_id": row[0], "customer_id": row[1], "current_state": row[2], "content_redacted": row[3]}


def close_clarification_session(session_id: str, status: str) -> None:
    if status not in {"needs_human_review", "completed", "cancelled"}:
        raise ValueError("澄清会话关闭状态不合法")
    with get_conn() as conn:
        changed = conn.execute(
            "UPDATE clarification_sessions SET status=%s, updated_at=now() WHERE session_id=%s AND status='open'",
            (status, session_id)).rowcount
        if changed != 1:
            raise ValueError("澄清会话不存在或已关闭")


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


def get_review(review_id: int) -> dict | None:
    """按 id 获取审核记录及条目当前状态，供状态机校验。"""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT pr.*, e.title, e.status AS entry_status FROM pending_reviews pr "
            "LEFT JOIN knowledge_entries e ON e.path=pr.nexus_path WHERE pr.id=%s",
            (review_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d.name for d in cur.description], row))


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
                        "(path, type, title, department, status, version, fingerprint, updated_at, description) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (path) DO UPDATE SET "
                        "type=EXCLUDED.type, title=EXCLUDED.title, department=EXCLUDED.department, "
                        "status=EXCLUDED.status, version=EXCLUDED.version, "
                        "fingerprint=EXCLUDED.fingerprint, updated_at=EXCLUDED.updated_at, "
                        "description=EXCLUDED.description",
                        (rel, meta.get("type", "concept"), meta.get("title", fn[:-3]),
                         meta.get("department"), meta.get("status", "active"),
                         meta.get("version", "V1.0"), meta.get("fingerprint"),
                         meta.get("updated", meta.get("created")),
                         meta.get("description")))
                    count += 1
    return count


# ---- 客户别名（应用层：让销售用中文简称选客户，系统内部仍只存脱敏代号） ----
MAX_ALIAS_CHARS = 40


def _normalize_alias(alias: str) -> str:
    return " ".join(str(alias or "").split())


def create_customer_alias(alias: str, customer_id: str, created_by: str) -> dict:
    """建立"中文别名 → 稳脱敏代号"映射。

    校验在服务端：别名不得为空/过长/含控制字符，且必须通过敏感信息检查
    （别名是用户自填的自由文本，这条闸不能省）；customer_id 必须是合规代号。
    重名冲突显式报错（ValueError），绝不静默合并到别的客户。
    """
    import rules  # 局部导入：避免数据层顶层依赖业务规则
    from sales_preprocess import CUSTOMER_ID_RE  # 复用同一份代号正则，避免两处定义漂移

    alias = _normalize_alias(alias)
    customer_id = str(customer_id or "").strip()
    if not alias:
        raise ValueError("别名不能为空")
    if len(alias) > MAX_ALIAS_CHARS:
        raise ValueError(f"别名过长（{len(alias)} 字符，上限 {MAX_ALIAS_CHARS}）")
    if any(ord(ch) < 32 for ch in alias):
        raise ValueError("别名不能包含控制字符")
    if rules.check_sensitive(alias) != "pass":
        raise ValueError("别名疑似包含未脱敏敏感信息（如手机号、身份证号），请改用不含敏感信息的简称")
    if not customer_id or len(customer_id) > 128 or not CUSTOMER_ID_RE.fullmatch(customer_id):
        raise ValueError("customer_id 必须是合规的脱敏代号")
    with get_conn() as conn:
        # 同一别名若已绑定到别的客户，明确拒绝并指出冲突对象
        existing = conn.execute(
            "SELECT customer_id FROM customer_aliases WHERE alias=%s", (alias,)).fetchall()
        others = [r[0] for r in existing if r[0] != customer_id]
        if others:
            raise KeyError(f"别名「{alias}」已绑定到其他客户：{', '.join(others)}；请换一个别名或先删除旧绑定")
        row = conn.execute(
            "INSERT INTO customer_aliases (alias, customer_id, created_by) VALUES (%s,%s,%s) "
            "ON CONFLICT (alias, customer_id) DO UPDATE SET created_by=EXCLUDED.created_by "
            "RETURNING alias, customer_id, created_by, created_at",
            (alias, customer_id, created_by)).fetchone()
    keys = ("alias", "customer_id", "created_by", "created_at")
    return dict(zip(keys, row))


def list_customer_aliases() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT alias, customer_id, created_by, created_at FROM customer_aliases "
            "ORDER BY created_at DESC, alias").fetchall()
    keys = ("alias", "customer_id", "created_by", "created_at")
    return [dict(zip(keys, row)) for row in rows]


def resolve_customer_alias(alias: str) -> str | None:
    """别名 → customer_id；未登记返回 None；同别名对应多个客户时视为冲突（KeyError）。"""
    alias = _normalize_alias(alias)
    if not alias:
        return None
    with get_conn() as conn:
        rows = [r[0] for r in conn.execute(
            "SELECT customer_id FROM customer_aliases WHERE alias=%s", (alias,)).fetchall()]
    if not rows:
        return None
    if len(rows) > 1:
        raise KeyError(f"别名「{alias}」对应多个客户（{', '.join(sorted(rows))}），请改用 customer_id 提交")
    return rows[0]


def delete_customer_alias(alias: str, customer_id: str, *, requester: str, is_admin: bool) -> bool:
    """删除别名绑定；仅创建者或管理员可删。返回是否删除了记录。"""
    alias = _normalize_alias(alias)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT created_by FROM customer_aliases WHERE alias=%s AND customer_id=%s",
            (alias, customer_id)).fetchone()
        if row is None:
            return False
        if not is_admin and row[0] != requester:
            raise PermissionError("仅别名的创建者或管理员可以删除该绑定")
        conn.execute("DELETE FROM customer_aliases WHERE alias=%s AND customer_id=%s", (alias, customer_id))
    return True
