import psycopg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _conn():
    return psycopg.connect(
        host="localhost", port=5432, dbname="llmwiki_test",
        user="llmwiki", password="llmwiki")


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")}


def test_schema_creates_expected_tables(tmp_path):
    # conftest 的 autouse _env 已对测试库建好 schema
    with _conn() as conn:
        tables = _tables(conn)
    assert {"knowledge_entries", "compile_tasks", "pending_reviews", "search_logs",
            "audit_logs", "contributors", "conflicts", "users"} <= tables


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
                      "audit_logs", "contributors", "conflicts", "users"}
