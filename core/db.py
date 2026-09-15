"""PostgreSQL 数据访问层（Phase 2 SP1：SQLite → psycopg3）。所有表操作唯一入口。

对外接口签名与 Phase 1 SQLite 版保持一致（36 个函数/工具被 app/ops/review/growth/upload 依赖），
仅内部实现切换为 PostgreSQL 16 + pgvector + psycopg_pool 连接池。"""
import os
import re
import uuid
import logging
from contextlib import contextmanager
from contextvars import ContextVar
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


def _kb_root() -> str:
    """当前租户的知识库根（L3.5 文件分区）。延迟 import paths：paths 反向 import db 取租户。"""
    import paths
    return str(paths.kb_root())

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
logger = logging.getLogger("llmwiki.db")

# ---- 租户上下文（L3）----
# 多租户隔离靠 Postgres RLS：策略用 current_setting('app.tenant_id') 判定，
# 而 contextvar 决定"当前这次操作用哪个租户"。HTTP 请求由中间件绑定（见 api/main.py），
# 后台 worker/脚本可用 bind_tenant() 显式指定；默认 'default'（单租户部署行为不变）。
_TENANT: ContextVar[str] = ContextVar("llmwiki_tenant", default="default")
DEFAULT_TENANT = "default"


def current_tenant() -> str:
    return _TENANT.get()


def bind_tenant(tenant_id: str | None):
    """绑定当前上下文的租户；返回 token 供复位。空值回落默认租户。"""
    return _TENANT.set((tenant_id or DEFAULT_TENANT).strip() or DEFAULT_TENANT)


def reset_tenant(token) -> None:
    try:
        _TENANT.reset(token)
    except (ValueError, LookupError):      # 跨上下文复位失败时忽略，不影响主流程
        pass


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

    供服务启动时调用——即使跳过 init.sh 也能安全运行；clone 后空目录/缺失目录在此补齐。

    **权限边界（L3 多租户）**：建表/建策略需要表属主权限。生产推荐的姿态是
    「以属主跑一次迁移，再以**受限角色** `llmwiki_app` 跑应用」——受限角色没有 DDL 权限，
    此时本函数不再尝试建表，而是**验证 schema 已就绪**（缺表就明确报错，不静默带病启动）。
    """
    for rel in _REQUIRED_DIRS:
        os.makedirs(os.path.join(str(_kb_root()), rel), exist_ok=True)
    with open(_SCHEMA, encoding="utf-8") as f:
        ddl = f.read()
    try:
        with get_conn() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(ddl)
    except psycopg.errors.InsufficientPrivilege:
        _assert_schema_ready()
        logger.warning("当前连接无 DDL 权限（受限角色）：跳过建表，已确认 schema 就绪")
    try:
        if not rls_enforced():
            logger.warning(
                "RLS 未生效：当前数据库角色是超级用户或 BYPASSRLS，租户隔离退化为应用层过滤"
                "（生产/验收请用受限角色 llmwiki_app，见 docs/WIKI-70 §5 L3.5）")
    except Exception:                     # 自检失败不影响启动（例如只读连接）
        logger.debug("RLS 自检跳过", exc_info=True)
    _ensure_admin()


def rls_enforced() -> bool:
    """当前连接的角色是否**真的**受 RLS 约束（诚实自检，绝不假装隔离生效）。

    超级用户与 `BYPASSRLS` 角色会绕过所有 RLS 策略——Docker 默认的 `POSTGRES_USER`
    就是超级用户，本地开发即处于这种状态：此时租户隔离**退化为应用层显式过滤**
    （`tenant_id = current_tenant()`，见 WIKI-70 §5 L3.5）。生产/验收须用受限角色
    `llmwiki_app`（`docker/initdb/20-app-role`），并把本函数作为验收项。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    return not (row and row[0])


def _assert_schema_ready() -> None:
    """受限角色路径下的自检：核心表必须已由属主迁移创建。"""
    with get_conn() as conn:
        for table in ("users", "knowledge_entries", "compile_tasks"):
            try:
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            except psycopg.errors.UndefinedTable as exc:
                raise RuntimeError(
                    f"schema 未就绪（缺表 {table}）且当前角色无 DDL 权限："
                    f"请先以属主执行一次迁移（docker compose up -d api，或手工跑 schema.sql）") from exc


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
    """PostgreSQL 连接上下文（连接池）+ **租户上下文注入**。

    psycopg_pool 的 pooled connection 上下文已管理事务生命周期：
    正常退出自动 commit，异常退出自动 rollback；归还池连接无需手动 close。

    每次取连接都把当前租户写进 `app.tenant_id`（会话级），供 RLS 策略
    `tenant_id = current_setting('app.tenant_id', true)` 判定——**连接是复用的，
    所以每次 checkout 都必须重设**，否则会串租户。默认租户 `default`（单租户部署无感）。
    """
    with _get_pool().connection() as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (current_tenant(),))
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


def find_duplicate_intake(customer_id: str, content_hash: str) -> dict | None:
    """内容级幂等判据：同一客户 + 相同正文指纹（sha256）的最近一次洽谈。

    背景（实测问题）：前端每次读文件都会换一个 `idempotency_key`，所以幂等键只能防"同一个键重放"，
    防不了"同一份纪要再提交一次"——结果同一个客户堆出多条看起来一模一样的会话。
    这里按内容指纹兜住重复提交；`archived` 标记已归档记录（归档的不复用，否则会把人引回已归档会话）。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.conversation_id, c.idempotency_key, c.source_ref, c.submitted_by, c.created_at, "
            "s.session_id, (s.deleted_at IS NOT NULL) "
            "FROM conversations c "
            "JOIN evidence e ON e.conversation_id=c.conversation_id "
            "LEFT JOIN clarification_sessions s ON s.conversation_id=c.conversation_id "
            "WHERE c.customer_id=%s AND e.content_hash=%s "
            "ORDER BY c.created_at DESC LIMIT 1", (customer_id, content_hash)).fetchone()
    if row is None:
        return None
    return {"conversation_id": row[0], "idempotency_key": row[1], "source_ref": row[2],
            "submitted_by": row[3], "submitted_at": row[4], "session_id": row[5],
            "archived": bool(row[6])}


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


def soft_delete_clarification_session(session_id: str, deleted_by: str) -> dict:
    """软删除（归档）澄清会话：标记会话与其产生的状态建议。

    - **不删状态事件/决策**：客户事实只能追加更正/撤回/过期（SA-02），归档不动事实链；
    - 幂等：已归档的会话重复删除返回 `already_deleted=True`；
    - 追问/回答不单独标记——它们只通过会话读取，会话归档即不可见。

    权限由调用方（API 层）限定为管理员。
    """
    if not (session_id or "").strip() or not (deleted_by or "").strip():
        raise ValueError("session_id / deleted_by 不能为空")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT conversation_id, deleted_at FROM clarification_sessions WHERE session_id=%s FOR UPDATE",
            (session_id,)).fetchone()
        if row is None:
            raise KeyError("澄清会话不存在")
        conversation_id, deleted_at = row
        if deleted_at is not None:
            return {"session_id": session_id, "conversation_id": conversation_id,
                    "already_deleted": True, "proposals_archived": 0}
        conn.execute("UPDATE clarification_sessions SET deleted_at=now(), deleted_by=%s WHERE session_id=%s",
                     (deleted_by, session_id))
        archived = conn.execute(
            "UPDATE state_proposals SET deleted_at=now(), deleted_by=%s "
            "WHERE conversation_id=%s AND deleted_at IS NULL",
            (deleted_by, conversation_id)).rowcount
    return {"session_id": session_id, "conversation_id": conversation_id,
            "already_deleted": False, "proposals_archived": archived}


def restore_clarification_session(session_id: str) -> dict:
    """恢复已归档的会话与其状态建议（归档可逆，数据一直在库里）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT conversation_id, deleted_at FROM clarification_sessions WHERE session_id=%s FOR UPDATE",
            (session_id,)).fetchone()
        if row is None:
            raise KeyError("澄清会话不存在")
        conversation_id, deleted_at = row
        if deleted_at is None:
            return {"session_id": session_id, "restored": False, "reason": "该会话未被归档"}
        conn.execute("UPDATE clarification_sessions SET deleted_at=NULL, deleted_by=NULL WHERE session_id=%s",
                     (session_id,))
        revived = conn.execute(
            "UPDATE state_proposals SET deleted_at=NULL, deleted_by=NULL "
            "WHERE conversation_id=%s AND deleted_at IS NOT NULL", (conversation_id,)).rowcount
    return {"session_id": session_id, "conversation_id": conversation_id,
            "restored": True, "proposals_restored": revived}


def get_clarification_session(session_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id, conversation_id, status, round_count, max_rounds, created_by, created_at, updated_at, "
            "resolution_note, resolved_by, resolved_at "
            "FROM clarification_sessions WHERE session_id=%s AND deleted_at IS NULL", (session_id,)).fetchone()
        if row is None:
            return None
        keys = ("session_id", "conversation_id", "status", "round_count", "max_rounds", "created_by", "created_at", "updated_at",
                "resolution_note", "resolved_by", "resolved_at")
        result = dict(zip(keys, row))
        result["turns"] = list_clarification_turns(session_id)
        result["answers"] = list_clarification_answers(session_id)
        return result


def resolve_clarification_session(session_id: str, status: str, resolved_by: str,
                                  note: str | None = None) -> dict:
    """人工处置澄清会话：关闭（cancelled）或补充事实后重开（open）。

    - 仅允许 open / needs_human_review 被处置；completed / cancelled 是终态（防重复处置）。
    - 重开要求还有可用的 Agent 运行预算：最多 `max_rounds` 轮追问 + 1 次收尾判定，
      即 `round_count <= max_rounds` 时可重开；**人工不得加轮次**（否则绕过 SA-10 的有限轮次约束）。
    """
    if status not in {"open", "cancelled"}:
        raise ValueError("resolution status 只能是 open 或 cancelled")
    if not (session_id or "").strip() or not (resolved_by or "").strip():
        raise ValueError("session_id / resolved_by 不能为空")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, round_count, max_rounds FROM clarification_sessions WHERE session_id=%s FOR UPDATE",
            (session_id,)).fetchone()
        if row is None:
            raise KeyError("澄清会话不存在")
        current, round_count, max_rounds = row
        if current in ("completed", "cancelled"):
            raise ValueError(f"澄清会话已结束（{current}），不可再处置")
        if status == "open" and round_count > max_rounds:
            raise ValueError("轮次已用尽（含收尾判定），不能重开；请关闭会话并改走状态建议流程")
        updated = conn.execute(
            "UPDATE clarification_sessions SET status=%s, resolution_note=%s, resolved_by=%s, "
            "resolved_at=now(), updated_at=now() WHERE session_id=%s "
            "RETURNING session_id, conversation_id, status, round_count, max_rounds, created_by, "
            "resolution_note, resolved_by, resolved_at, created_at, updated_at",
            (status, note, resolved_by, session_id)).fetchone()
    keys = ("session_id", "conversation_id", "status", "round_count", "max_rounds", "created_by",
            "resolution_note", "resolved_by", "resolved_at", "created_at", "updated_at")
    return dict(zip(keys, updated))


def list_clarification_turns(session_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT turn_id, session_id, turn_no, status, agent_output, question_count, input_tokens, output_tokens, latency_ms, created_at "
            "FROM clarification_turns WHERE session_id=%s ORDER BY turn_no", (session_id,)).fetchall()
    keys = ("turn_id", "session_id", "turn_no", "status", "agent_output", "question_count", "input_tokens", "output_tokens", "latency_ms", "created_at")
    return [dict(zip(keys, row)) for row in rows]


def append_clarification_turn(session_id: str, status: str, agent_output: dict,
                              question_count: int, input_tokens: int | None = None,
                              output_tokens: int | None = None, latency_ms: int | None = None,
                              expected_round_count: int | None = None) -> dict:
    """追加一轮 Agent 输出；轮次达到上限后自动关闭会话，不允许无限追问。

    expected_round_count：调用方**在调用模型之前**读到的轮次。传入即启用乐观锁——
    模型调用耗时数秒，期间会话可能已被并发/重复请求推进；此时基于旧轮次的写入必须
    失败，否则会写出内容重复的一轮并提前耗尽轮次预算（前端重复打开会话即可触发）。
    """
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
        # 方案 A 语义：max_rounds = 最多**追问**轮数；预算用尽后仍允许**一次收尾判定**
        # （结论轮），因此上限是 max_rounds + 1 次 Agent 运行。
        if round_count > max_rounds:
            raise ValueError("已达到最大澄清轮次（含收尾判定）")
        if round_count == max_rounds and status == "needs_clarification":
            raise ValueError("追问轮次已用尽：收尾轮不得再提出追问")
        if expected_round_count is not None and round_count != expected_round_count:
            raise ValueError("并发推进冲突：会话轮次已被其他请求推进")
        turn_no = round_count + 1
        # 会话状态映射（会话词表没有 insufficient_evidence，必须显式归一，否则撞 CHECK）：
        # - 追问轮一律保持 open：包括"最后一轮追问"，让这一轮的问题能被回答（不再问了不给答）
        # - 证据不足 / 模型失败 → 转人工
        # - 资源就绪 → 可生成建议
        if status == "needs_clarification":
            next_status = "open"
        elif status in ("insufficient_evidence", "human_review"):
            next_status = "needs_human_review"
        else:
            next_status = "ready_for_proposal"
        # 先抢占轮次（乐观锁）：并发重复触发时只有一个请求能推进，另一个 UPDATE 命中 0 行即失败
        claimed = conn.execute(
            "UPDATE clarification_sessions SET round_count=%s, status=%s, updated_at=now() "
            "WHERE session_id=%s AND round_count=%s AND status='open'",
            (turn_no, next_status, session_id, round_count))
        if claimed.rowcount == 0:
            raise ValueError("并发推进冲突：会话轮次已被其他请求推进")
        turn_id = _clarification_id("turn")
        turn = conn.execute(
            "INSERT INTO clarification_turns (turn_id, session_id, turn_no, status, agent_output, question_count, input_tokens, output_tokens, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING turn_id, session_id, turn_no, status, agent_output, question_count, input_tokens, output_tokens, latency_ms, created_at",
            (turn_id, session_id, turn_no, status, Jsonb(agent_output), question_count, input_tokens, output_tokens, latency_ms)).fetchone()
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


def list_user_clarification_sessions(username: str, limit: int = 100,
                                     include_deleted: bool = False) -> list[dict]:
    """销售工作台只读取自己创建的会话；归档默认不列出（管理员可显式包含）。"""
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.session_id, s.conversation_id, c.customer_id, s.status, s.round_count, "
            "s.max_rounds, s.created_by, s.created_at, s.updated_at, s.resolution_note, s.resolved_by, "
            "s.deleted_at, s.deleted_by, "
            # 列表要能分辨"同一客户的多次洽谈"：给一行摘要（正文前 400 字，Python 侧清洗成可读版）
            # + 来源文件名。
            "(SELECT left(e.content_redacted, 400) FROM evidence e "
            " WHERE e.conversation_id=s.conversation_id ORDER BY e.created_at DESC LIMIT 1), "
            "(SELECT e.source_ref FROM evidence e WHERE e.conversation_id=s.conversation_id "
            " ORDER BY e.created_at DESC LIMIT 1) "
            "FROM clarification_sessions s JOIN conversations c ON c.conversation_id=s.conversation_id "
            "WHERE s.created_by=%s AND (%s OR s.deleted_at IS NULL) "
            "ORDER BY s.updated_at DESC LIMIT %s", (username, include_deleted, limit)).fetchall()
    keys = ("session_id", "conversation_id", "customer_id", "status", "round_count", "max_rounds",
            "created_by", "created_at", "updated_at", "resolution_note", "resolved_by",
            "deleted_at", "deleted_by", "content_preview", "source_ref")
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item["content_preview"] = _preview_text(item.get("content_preview"))
        out.append(item)
    return out


_PREVIEW_MARKERS = re.compile(r"^\s*[#*>\-]+\s*|\*\*")
_PREVIEW_REF = re.compile(r"\[[A-Z_]+_REF:nv-[0-9a-f]+\]")


def _preview_text(text: str | None, limit: int = 80) -> str | None:
    """列表摘要清洗：正文是 Markdown 纪要（`# **标题**`），列表里只留可读文字。

    敏感数值占位符（`[AMOUNT_REF:nv-xxx]`）对销售是技术噪音——精确值本来就不该出现在列表，
    这里统一换成「【数值已隐藏】」，与工作台"技术细节按需展开"的口径一致。
    """
    if not text:
        return text
    cleaned = _PREVIEW_REF.sub("【数值已隐藏】", text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = _PREVIEW_MARKERS.sub("", cleaned).strip()
    return cleaned[:limit] or None


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
                 updated_at: str, tenant_id: str | None = None) -> None:
    """UPSERT INTO knowledge_entries（同租户内 path 冲突则更新全字段）。

    冲突目标必须是 **(tenant_id, path)**（L3.5）：文件按租户分区后不同租户会有同名条目，
    只按 path 去重会跨租户覆盖——tenant_id 显式写入，不依赖列默认值（默认值只在
    连接设过 `app.tenant_id` 时才正确）。
    """
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO knowledge_entries
               (path, type, title, department, status, version, fingerprint, updated_at, tenant_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, path) DO UPDATE SET
                 type=EXCLUDED.type, title=EXCLUDED.title, department=EXCLUDED.department,
                 status=EXCLUDED.status, version=EXCLUDED.version,
                 fingerprint=EXCLUDED.fingerprint, updated_at=EXCLUDED.updated_at""",
            (path, type_, title, department, status, version, fingerprint, updated_at, tenant))


def update_status(path: str, status: str, tenant_id: str | None = None) -> None:
    """更新 knowledge_entries.status（不触碰文件，文件由调用方改）；只动本租户的行。"""
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        conn.execute("UPDATE knowledge_entries SET status=%s WHERE path=%s AND tenant_id=%s",
                     (status, path, tenant))


def move_entry(old_path: str, new_path: str, status: str, tenant_id: str | None = None) -> None:
    """DELETE 旧 path 行 + INSERT 新 path 行（保留原字段）。租户内操作，不跨租户动数据。"""
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type, title, department, version, fingerprint, updated_at "
            "FROM knowledge_entries WHERE path=%s AND tenant_id=%s", (old_path, tenant)).fetchone()
        if row is None:
            raise KeyError(f"knowledge_entries 无此路径: {old_path}")
        conn.execute("DELETE FROM knowledge_entries WHERE path=%s AND tenant_id=%s", (old_path, tenant))
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, department, status, version, fingerprint, updated_at, tenant_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (new_path, row[0], row[1], row[2], status, row[3], row[4], row[5], tenant))


# ---- 编译任务（L2：可并发认领的工作队列）----
TASK_TERMINAL = ("done", "failed", "cached")
DEFAULT_LEASE_SECONDS = 600
DEFAULT_MAX_ATTEMPTS = 3


def insert_compile_task(raw_path: str, fingerprint: str, *,
                        tenant_id: str | None = None, priority: int = 100) -> int:
    """入队一个 status='pending' 编译任务，返回 id（等价于 enqueue_compile_task）。"""
    return enqueue_compile_task(raw_path, fingerprint, tenant_id=tenant_id, priority=priority)


def enqueue_compile_task(raw_path: str, fingerprint: str, *,
                         tenant_id: str | None = None, priority: int = 100) -> int:
    """入队：priority 越小越先被认领（交互式上传用 10，批量扫描用 100）。

    `tenant_id` 不给时取**当前租户上下文**（不写死 'default'，否则多租户下上传的任务
    会全部落进默认租户——这正是单测抓到的 bug）。
    """
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status, tenant_id, priority, started_at) "
            "VALUES (%s,%s,'pending',%s,%s,now()) RETURNING id",
            (raw_path, fingerprint, tenant, priority)).fetchone()[0]


def claim_compile_task(worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                       tenant_id: str | None = None,
                       max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict | None:
    """认领下一个可执行任务（**并发安全**：FOR UPDATE SKIP LOCKED，多 worker 不会抢同一行）。

    可认领 = `pending` 且退避已到（`next_retry_at` 为空或已过）**或** `processing` 但租约已过期
    （worker 崩溃自愈），且 `attempts < max_attempts`。认领即 attempts+1 并设置租约。
    返回任务行（含 raw_path/fingerprint/attempts/tenant_id），无可认领任务返回 None。
    """
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE compile_tasks SET status='processing', "
            "       lease_until = now() + make_interval(secs => %s), leased_by = %s, "
            "       started_at = now(), attempts = attempts + 1, error_msg = NULL "
            "WHERE id = ("
            "  SELECT id FROM compile_tasks "
            "  WHERE attempts < %s AND (%s::text IS NULL OR tenant_id = %s::text) "
            "    AND ((status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= now())) "
            "         OR (status = 'processing' AND (lease_until IS NULL OR lease_until < now()))) "
            "  ORDER BY priority ASC, id ASC FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING id, raw_path, fingerprint, attempts, tenant_id, priority",
            (lease_seconds, worker_id, max_attempts, tenant_id, tenant_id)).fetchone()
    if row is None:
        return None
    keys = ("id", "raw_path", "fingerprint", "attempts", "tenant_id", "priority")
    return dict(zip(keys, row))


def finish_compile_task(task_id: int, status: str, *, nexus_path: str | None = None,
                        error_msg: str | None = None,
                        retry_backoff_seconds: int | None = None) -> None:
    """收尾：终态（done/cached/failed）或**退避重排**（回 pending 等下次认领）。

    `retry_backoff_seconds` 给定时表示"这次失败但还要再试"→ status=pending + next_retry_at；
    不给定时按 `status` 落终态。认领时 attempts 已自增，故 attempts 用尽的任务不会被再认领。
    """
    with get_conn() as conn:
        if retry_backoff_seconds is not None:
            conn.execute(
                "UPDATE compile_tasks SET status='pending', error_msg=%s, lease_until=NULL, leased_by=NULL, "
                "       next_retry_at = now() + make_interval(secs => %s) WHERE id=%s",
                (error_msg, max(1, retry_backoff_seconds), task_id))
            return
        conn.execute(
            "UPDATE compile_tasks SET status=%s, nexus_path=COALESCE(%s,nexus_path), error_msg=%s, "
            "       completed_at=now(), lease_until=NULL, leased_by=NULL, next_retry_at=NULL WHERE id=%s",
            (status, nexus_path, error_msg, task_id))


def requeue_compile_task(task_id: int) -> None:
    """人工重试：把 failed 任务重置回 pending（清空退避与尝试次数），供 UI「重试」按钮用。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE compile_tasks SET status='pending', attempts=0, next_retry_at=NULL, "
            "       lease_until=NULL, leased_by=NULL, error_msg=NULL WHERE id=%s", (task_id,))


def queue_stats(tenant_id: str | None = None, *, all_tenants: bool = False) -> dict:
    """队列观测：各状态任务数与最老待处理任务年龄（秒）。

    默认只看**当前租户**（HTTP 看板/管理接口）；后台 worker 要跨租户巡检时显式传
    `all_tenants=True`——不能靠"不传就是全部"，那会让租户看板在超级用户连接下串数据。
    """
    tenant = None if all_tenants else (tenant_id or current_tenant())
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, count(*) FROM compile_tasks "
            "WHERE (%s::text IS NULL OR tenant_id = %s::text) GROUP BY status",
            (tenant, tenant)).fetchall()
        oldest = conn.execute(
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(started_at))), 0) FROM compile_tasks "
            "WHERE status IN ('pending','processing') AND (%s::text IS NULL OR tenant_id = %s::text)",
            (tenant, tenant)).fetchone()[0]
    stats = {status: count for status, count in rows}
    stats["oldest_pending_seconds"] = int(float(oldest or 0))
    return stats


def update_compile_task(task_id: int, status: str,
                        nexus_path: str | None = None,
                        error_msg: str | None = None) -> None:
    """兼容旧调用方的状态更新（终态走 finish_compile_task 语义）。"""
    if status in TASK_TERMINAL:
        finish_compile_task(task_id, status, nexus_path=nexus_path, error_msg=error_msg)
        return
    with get_conn() as conn:
        conn.execute("UPDATE compile_tasks SET status=%s WHERE id=%s", (status, task_id))


def find_done_compile_task(raw_path: str, fingerprint: str,
                           exclude_id: int | None = None,
                           tenant_id: str | None = None) -> dict | None:
    """同租户 + 同路径 + 同指纹的已完成（done/cached）任务——**队列模式下的去重判据**。

    为什么需要它：队列里同一个文件可能被重复入队（上传 / 批量扫描 / 人工重试），
    每个任务行的 `latest_compile_task` 只看到"自己是最新的 pending"，会漏掉历史 done 记录，
    于是重复编译并产出 `xxx-2.md` 这类重复条目（实测问题）。
    `exclude_id` 用于排除"当前正在处理的这一行"。

    **必须按租户过滤**（L3.5 实测 bug）：文件按租户分区后，两个租户会有**完全同名同内容**
    的 `RAW/会议/周会.md`——只按 (path, fingerprint) 去重会让 B 租户的文件命中 A 租户的
    done 记录、直接判 `cached` 而**永不编译**。RLS 只在受限角色下生效，本地开发/测试用
    超级用户连接时会绕过，所以这里必须显式带上 tenant_id。
    """
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, raw_path, nexus_path, status, fingerprint FROM compile_tasks "
            "WHERE raw_path=%s AND fingerprint=%s AND tenant_id=%s AND status IN ('done','cached') "
            "  AND (%s::int IS NULL OR id <> %s::int) "
            "ORDER BY id DESC LIMIT 1",
            (raw_path, fingerprint, tenant, exclude_id, exclude_id)).fetchone()
    if row is None:
        return None
    return dict(zip(("id", "raw_path", "nexus_path", "status", "fingerprint"), row))


def latest_compile_task(raw_path: str, tenant_id: str | None = None) -> dict | None:
    """某 RAW 路径的最新编译任务（断点续跑/指纹幂等的判据）。**按租户过滤**（同 find_done_compile_task）。"""
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, raw_path, nexus_path, status, fingerprint, error_msg, started_at, completed_at "
            "FROM compile_tasks WHERE raw_path=%s AND tenant_id=%s ORDER BY id DESC LIMIT 1",
            (raw_path, tenant)).fetchone()
    if row is None:
        return None
    keys = ("id", "raw_path", "nexus_path", "status", "fingerprint", "error_msg",
            "started_at", "completed_at")
    return dict(zip(keys, row))


# ---- L4：租户模型配置与用量记账 ----
CONFIG_FIELDS = ("tenant_id", "purpose", "provider", "base_url", "model",
                 "api_key_ciphertext", "api_key_key_version", "max_tokens",
                 "temperature", "daily_token_quota", "enabled", "updated_by", "updated_at")


def upsert_tenant_model_config(*, tenant_id: str, purpose: str = "default", model: str,
                               provider: str = "openai_compatible", base_url: str | None = None,
                               api_key_ciphertext: str | None = None,
                               api_key_key_version: str | None = None,
                               max_tokens: int = 4000, temperature: float = 0.0,
                               daily_token_quota: int | None = None,
                               enabled: bool = True, updated_by: str | None = None) -> int:
    """写入/更新租户模型配置（密钥只存密文）。

    `api_key_ciphertext=None` 表示"不动已有密钥"（改模型名不必重传 key）；
    传空串则清空密钥（回落环境变量）。
    """
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO tenant_model_configs
               (tenant_id, purpose, provider, base_url, model, api_key_ciphertext,
                api_key_key_version, max_tokens, temperature, daily_token_quota, enabled, updated_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, purpose) DO UPDATE SET
                 provider=EXCLUDED.provider, base_url=EXCLUDED.base_url, model=EXCLUDED.model,
                 api_key_ciphertext=CASE WHEN %s THEN tenant_model_configs.api_key_ciphertext
                                         ELSE EXCLUDED.api_key_ciphertext END,
                 api_key_key_version=CASE WHEN %s THEN tenant_model_configs.api_key_key_version
                                          ELSE EXCLUDED.api_key_key_version END,
                 max_tokens=EXCLUDED.max_tokens, temperature=EXCLUDED.temperature,
                 daily_token_quota=EXCLUDED.daily_token_quota, enabled=EXCLUDED.enabled,
                 updated_by=EXCLUDED.updated_by, updated_at=now()
               RETURNING id""",
            (tenant, purpose, provider, base_url, model, api_key_ciphertext,
             api_key_key_version, max_tokens, temperature, daily_token_quota, enabled, updated_by,
             api_key_ciphertext is None, api_key_ciphertext is None)).fetchone()
    return row[0]


def list_tenant_model_configs(tenant_id: str | None = None) -> list[dict]:
    """列出该租户的模型配置（**只返回密文与是否已配密钥，绝不返回明文**）。"""
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT tenant_id, purpose, provider, base_url, model, api_key_key_version, "
            "       (api_key_ciphertext IS NOT NULL) AS has_key, max_tokens, temperature, "
            "       daily_token_quota, enabled, updated_by, updated_at "
            "FROM tenant_model_configs WHERE tenant_id=%s ORDER BY purpose", (tenant,)).fetchall()
    keys = ("tenant_id", "purpose", "provider", "base_url", "model", "api_key_key_version",
            "has_key", "max_tokens", "temperature", "daily_token_quota", "enabled",
            "updated_by", "updated_at")
    return [dict(zip(keys, r)) for r in rows]


def delete_tenant_model_config(tenant_id: str, purpose: str = "default") -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tenant_model_configs WHERE tenant_id=%s AND purpose=%s",
                     (tenant_id or current_tenant(), purpose))


def record_llm_usage(*, tenant_id: str, purpose: str, model: str | None,
                     input_tokens: int = 0, output_tokens: int = 0, latency_ms: int = 0,
                     ok: bool = True, trace_id: str | None = None) -> None:
    """记一次模型调用（token/延迟/成败）。调用方负责静默失败，本函数不吞异常。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO llm_usage (tenant_id, purpose, model, input_tokens, output_tokens, "
            "latency_ms, ok, trace_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant_id or current_tenant(), purpose, model, input_tokens or 0, output_tokens or 0,
             latency_ms or 0, ok, trace_id))


def llm_usage_today(tenant_id: str | None = None) -> int:
    """该租户当日已用 token（输入+输出），配额拦截与看板用。"""
    with get_conn() as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_usage "
            "WHERE tenant_id=%s AND created_at >= date_trunc('day', now())",
            (tenant_id or current_tenant(),)).fetchone()[0])


def llm_usage_summary(tenant_id: str | None = None, days: int = 7) -> list[dict]:
    """按 purpose 汇总最近 N 天用量（看板）。"""
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT purpose, count(*) AS calls, COALESCE(SUM(input_tokens),0), "
            "       COALESCE(SUM(output_tokens),0), COALESCE(SUM(latency_ms),0), "
            "       count(*) FILTER (WHERE NOT ok) "
            "FROM llm_usage WHERE tenant_id=%s AND created_at >= now() - make_interval(days => %s) "
            "GROUP BY purpose ORDER BY 4 DESC", (tenant, days)).fetchall()
    return [{"purpose": r[0], "calls": r[1], "input_tokens": int(r[2]), "output_tokens": int(r[3]),
             "latency_ms": int(r[4]), "errors": r[5]} for r in rows]


def list_recent_compile_tasks(limit: int = 50, tenant_id: str | None = None) -> list[dict]:
    """最近的编译任务（upload 页状态表）。**只看当前租户**（同 queue_stats 的理由）。"""
    tenant = tenant_id or current_tenant()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, raw_path, status, fingerprint, error_msg, completed_at "
            "FROM compile_tasks WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
            (tenant, limit)).fetchall()
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


def find_review(nexus_path: str) -> dict | None:
    """某条目路径的最新审核记录（审核幂等判据：已有 ai_scores 就不重审）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, nexus_path, submitter, department, ai_verdict, ai_scores, "
            "human_decision, reject_reason, created_at FROM pending_reviews "
            "WHERE nexus_path=%s ORDER BY id DESC LIMIT 1", (nexus_path,)).fetchone()
    if row is None:
        return None
    keys = ("id", "nexus_path", "submitter", "department", "ai_verdict", "ai_scores",
            "human_decision", "reject_reason", "created_at")
    return dict(zip(keys, row))


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
def rebuild_index(tenant_id: str | None = None) -> int:
    """扫描**当前租户**知识库下 NEXUS/**/*.md 与 pending_review/*.md，解析 YAML 重建表。返回条目数。

    单文件损坏（无完整 frontmatter / YAML 非法 / frontmatter 非映射）仅跳过该文件，
    不影响其余条目重建。**只重建本租户**（L3.5）：扫本租户的根（`paths.kb_root()`），
    也只删本租户的行——否则一个租户重建会把别的租户索引清空。
    """
    import yaml
    import paths
    tenant = tenant_id or current_tenant()
    root = str(paths.kb_root(tenant))
    count = 0
    with get_conn() as conn:
        conn.execute("DELETE FROM knowledge_entries WHERE tenant_id=%s", (tenant,))
        for base in ("NEXUS", "pending_review"):
            base_dir = os.path.join(root, base)
            for dirpath, _, files in os.walk(base_dir):
                for fn in files:
                    if not fn.endswith(".md"):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root).replace("\\", "/")
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
                        "(path, type, title, department, status, version, fingerprint, updated_at, description, tenant_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (tenant_id, path) DO UPDATE SET "
                        "type=EXCLUDED.type, title=EXCLUDED.title, department=EXCLUDED.department, "
                        "status=EXCLUDED.status, version=EXCLUDED.version, "
                        "fingerprint=EXCLUDED.fingerprint, updated_at=EXCLUDED.updated_at, "
                        "description=EXCLUDED.description",
                        (rel, meta.get("type", "concept"), meta.get("title", fn[:-3]),
                         meta.get("department"), meta.get("status", "active"),
                         meta.get("version", "V1.0"), meta.get("fingerprint"),
                         meta.get("updated", meta.get("created")),
                         meta.get("description"), tenant))
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
