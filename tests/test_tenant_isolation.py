"""L3 多租户隔离测试。

分两层验证，避免"看着配好了其实没生效"：

**A. 应用层上下文（任何连接都成立）**
1. `tenant_id` 默认值动态取当前租户 → INSERT 不写 tenant_id 也落在正确租户；
2. 不显式绑定就是默认租户（单租户部署无感）；连接池复用时租户上下文不串；
3. JWT 带 `tenant` claim；中间件在**端点之前**绑定租户与 trace_id（响应头 X-Trace-Id 可观测）。

**B. 数据库级 RLS（需受限角色，超级用户会绕过）**
4. 跨租户**读**不可见、跨租户**写**被数据库拒绝（WITH CHECK）；
5. 受限角色 `llmwiki_app` 由 `docker/initdb/20-app-role` 创建；缺角色时明确 skip——
   这正是实测踩到的坑：Docker 默认 POSTGRES_USER 是超级用户（`rolbypassrls=t`），
   只加策略不做角色分离等于没有隔离。

> 为什么租户绑定写在中间件而不是 FastAPI 依赖：同步依赖与同步端点各自在线程池执行，
> contextvars 的修改不会互相传递（用例 3 锁这条）。
"""
import os
import sys
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "api"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import db  # noqa: E402

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
APP_ROLE = "llmwiki_app"


def _insert_entry(path: str, title: str = "标题") -> None:
    """插入一条条目（**不写 tenant_id**——由列默认值取当前租户，这正是要验证的机制）。"""
    db.upsert_entry(path, "concept", title, "共享层", "active", "V1.0", "fp", "2026-09-14")


def _paths() -> list[str]:
    with db.get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT path FROM knowledge_entries ORDER BY path").fetchall()]


def _tenant_of(path: str) -> str:
    with db.get_conn() as conn:
        return conn.execute("SELECT tenant_id FROM knowledge_entries WHERE path=%s", (path,)).fetchone()[0]


# ---------- A. 应用层上下文 ----------

def test_insert_lands_in_current_tenant_via_column_default():
    """默认值动态取当前租户：应用代码不必逐个改写 INSERT。"""
    token = db.bind_tenant(TENANT_A)
    try:
        _insert_entry("NEXUS/概念/A条目.md")
        assert _tenant_of("NEXUS/概念/A条目.md") == TENANT_A
    finally:
        db.reset_tenant(token)
    assert db.current_tenant() == db.DEFAULT_TENANT          # 复位后回到默认租户


def test_default_tenant_when_unbound():
    """不绑定 → 默认租户（单租户部署行为不变）。"""
    assert db.current_tenant() == db.DEFAULT_TENANT
    _insert_entry("NEXUS/概念/默认条目.md")
    assert _tenant_of("NEXUS/概念/默认条目.md") == db.DEFAULT_TENANT


def test_connection_reuse_does_not_leak_tenant():
    """连接池复用不串租户：同一物理连接先后服务两个租户，各写各的。"""
    for i in range(3):
        for tenant in (TENANT_A, TENANT_B):
            token = db.bind_tenant(tenant)
            try:
                _insert_entry(f"NEXUS/概念/{tenant}-{i}.md")
            finally:
                db.reset_tenant(token)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT tenant_id, count(*) FROM knowledge_entries GROUP BY 1 ORDER BY 1").fetchall()
    assert dict(rows) == {TENANT_A: 3, TENANT_B: 3}


def test_login_token_carries_tenant_and_middleware_binds_it():
    """JWT 带 tenant；中间件绑定并回写 X-Trace-Id；请求结束后租户上下文复位。"""
    from api.main import app
    from api import auth as auth_mod

    client = TestClient(app)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role, tenant_id) "
                     "VALUES (%s,%s,'admin',%s)",
                     ("tenant-a-admin", auth_mod.hash_password("ta-admin-123"), TENANT_A))
    login = client.post("/auth/login", json={"username": "tenant-a-admin", "password": "ta-admin-123"})
    token = login.json()["access_token"]
    payload = auth_mod.decode_token(token)
    assert payload["tenant"] == TENANT_A

    response = client.get("/entries", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers.get("X-Trace-Id")
    assert db.current_tenant() == db.DEFAULT_TENANT           # 请求外的上下文未被污染

    # 该租户的数据进库后归属正确（登录→中间件绑定→端点内 db.get_conn 取到租户）
    assert client.post("/uploads", headers={"Authorization": f"Bearer {token}"},
                       files={"files": ("租户A上传.md", b"# A\n\n" + "客户沟通记录。".encode("utf-8") * 8,
                                        "text/markdown")},
                       data={"category": "会议"}).status_code == 200
    with db.get_conn() as conn:
        tenants = {r[0] for r in conn.execute("SELECT DISTINCT tenant_id FROM compile_tasks").fetchall()}
    assert tenants == {TENANT_A}


# ---------- B. 数据库级 RLS（受限角色） ----------

def _app_role_conn(tenant: str):
    """用**受限角色**（非超级用户、非属主）打开连接并设置租户——RLS 只对这类连接生效。"""
    import conftest as _c
    dsn = {**_c.TEST_DB, "user": APP_ROLE, "password": "llmwiki"}
    conn = psycopg.connect(**dsn)
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    return conn


def _app_role_available() -> bool:
    try:
        import conftest as _c
        with psycopg.connect(**_c.TEST_DB) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s AND NOT rolsuper", (APP_ROLE,)).fetchone())
    except Exception:
        return False


needs_app_role = pytest.mark.skipif(
    not _app_role_available(),
    reason=f"受限角色 {APP_ROLE} 不存在（执行 docker/initdb/20-app-role 创建）——"
           f"超级用户会绕过 RLS，故无法验证数据库级隔离")


@needs_app_role
def test_rls_hides_other_tenants_rows():
    """RLS 读隔离：A 的行 B 看不见、A 自己看得见。"""
    token = db.bind_tenant(TENANT_A)
    try:
        _insert_entry("NEXUS/概念/RLS-A.md", "RLS-A")
    finally:
        db.reset_tenant(token)

    with _app_role_conn(TENANT_A) as conn:
        a_paths = [r[0] for r in conn.execute("SELECT path FROM knowledge_entries").fetchall()]
    with _app_role_conn(TENANT_B) as conn:
        b_paths = [r[0] for r in conn.execute("SELECT path FROM knowledge_entries").fetchall()]

    assert a_paths == ["NEXUS/概念/RLS-A.md"]
    assert b_paths == []                                   # B 看不到 A 的行


@needs_app_role
def test_rls_blocks_cross_tenant_write():
    """RLS 写隔离（WITH CHECK）：往别的租户写被数据库拒绝，而不是靠应用自觉。"""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with _app_role_conn(TENANT_B) as conn:
            conn.execute(
                "INSERT INTO knowledge_entries (path, type, title, status, version, tenant_id) "
                "VALUES ('NEXUS/概念/偷写.md','concept','偷写','active','V1.0',%s)", (TENANT_A,))


@needs_app_role
def test_rls_allows_writing_own_tenant():
    """同一策略不得误伤本租户写入（WITH CHECK 允许 tenant_id = 当前租户）。"""
    with _app_role_conn(TENANT_B) as conn:
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, status, version, tenant_id) "
            "VALUES ('NEXUS/概念/B自写.md','concept','B自写','active','V1.0',%s)", (TENANT_B,))
        count = conn.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0]
    assert count == 1
