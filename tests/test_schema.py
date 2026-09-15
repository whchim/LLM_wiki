import psycopg
from pathlib import Path

from conftest import TEST_DB  # 复用同一份连接参数（含 127.0.0.1 默认值，避免 IPv6 解析拖慢）

ROOT = Path(__file__).resolve().parent.parent


def _conn():
    return psycopg.connect(**TEST_DB)


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")}


def test_schema_creates_expected_tables(tmp_path):
    # conftest 的 autouse _env 已对测试库建好 schema
    with _conn() as conn:
        tables = _tables(conn)
    assert {"knowledge_entries", "compile_tasks", "pending_reviews", "search_logs",
            "audit_logs", "contributors", "conflicts", "users", "customers",
            "conversations", "evidence", "state_proposals", "state_decisions",
            "state_events", "current_states", "sensitive_numeric_values", "clarification_sessions",
            "clarification_turns", "clarification_answers"} <= tables


def test_schema_is_idempotent(tmp_path):
    # 幂等：二次执行 schema.sql 不报错、不产生重复表
    with open(ROOT / "schema.sql", encoding="utf-8") as f:
        ddl = f.read()
    with _conn() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(ddl)
        conn.execute(ddl)
        tables = _tables(conn)
    assert tables == {"knowledge_entries", "compile_tasks", "pending_reviews", "search_logs",
                      "audit_logs", "contributors", "conflicts", "users", "trace_events",
                      "health_reports", "customers", "conversations", "evidence",
                      "state_proposals", "state_decisions", "state_events", "current_states",
                      "sensitive_numeric_values", "clarification_sessions", "clarification_turns",
                      "clarification_answers", "customer_aliases",
                      "tenant_model_configs", "llm_usage"}


def test_entry_identity_is_per_tenant(tmp_path):
    """L3.5：条目身份 = (tenant_id, path)，贡献记录外键也必须是复合的（跨租户引用被数据库挡住）。

    锁的是这条：文件按租户分区后不同租户会有**同名条目**（`NEXUS/概念/产品.md`），
    身份仍是单列 path 的话，B 租户写入会覆盖 A 租户的行。
    """
    with _conn() as conn:
        pk = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='knowledge_entries'::regclass AND contype='p'").fetchone()[0]
        fk_cols = conn.execute(
            "SELECT (SELECT array_agg(a.attname ORDER BY x.ord) "
            "        FROM unnest(c.conkey) WITH ORDINALITY AS x(attnum, ord) "
            "        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = x.attnum) "
            "FROM pg_constraint c "
            "WHERE c.conrelid='contributors'::regclass AND c.contype='f' "
            "  AND c.confrelid='knowledge_entries'::regclass").fetchone()
    assert pk == "PRIMARY KEY (tenant_id, path)"
    assert fk_cols and fk_cols[0] is not None and sorted(fk_cols[0]) == ["entry_path", "tenant_id"]
