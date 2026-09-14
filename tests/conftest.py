"""pytest 公共配置：PostgreSQL 隔离测试（Phase 2 SP1）。

背景：Demo 期 DB 为 SQLite 文件，测试通过 DB_PATH 指向临时目录隔离。
SP1 起数据层迁至真实 PostgreSQL（本地 / Docker PG，如 `docker compose up -d db`），
测试连一个专用测试库（默认 llmwiki_test），并在每个测试前重置 schema 保证隔离。

使用方式：
    1. 启动 PostgreSQL（推荐：`docker compose up -d db`，发布 5432）。
    2. `python -m pytest tests -q`
若无可用 PG，可令测试跳过：PYTEST_SKIP_NO_DB=1 python -m pytest tests -q
"""
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))


# 测试库连接参数（与生产 llmwiki 区分，避免污染）
# host 默认 127.0.0.1 而非 localhost：Windows 上 localhost 会先解析到 IPv6，
# 而 Docker 只发布 IPv4，导致每次连接多等 2-5 秒（实测 5202ms vs 49ms）。
TEST_DB = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": os.environ.get("DB_PORT", "5432"),
    "dbname": os.environ.get("TEST_DB_NAME", "llmwiki_test"),
    "user": os.environ.get("DB_USER", "llmwiki"),
    "password": os.environ.get("DB_PASS", "llmwiki"),
}

_DB_UP = None


def _pg_available() -> bool:
    """探测 PostgreSQL 是否可达（含专用测试库是否被创建过）。"""
    global _DB_UP
    if _DB_UP is not None:
        return _DB_UP
    try:
        conn = psycopg.connect(**TEST_DB)
        conn.close()
        _DB_UP = True
    except Exception:
        _DB_UP = False
    return _DB_UP


def _reset_schema() -> None:
    """重置测试库：DROP SCHEMA public CASCADE 后重建（ensure_schema 幂等）。"""
    with psycopg.connect(**TEST_DB) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    import db
    db.ensure_schema()
    _grant_to_app_role()


def _grant_to_app_role() -> None:
    """把新表的增删改查授给受限应用角色（L3 多租户 RLS 验收需要）。

    受限角色（`docker/initdb/20-app-role` 创建）不能是超级用户——超级用户会绕过 RLS
    （实测踩坑：Docker 默认 POSTGRES_USER 就是超级用户，只加策略不做角色分离等于没隔离）。
    它也不是表属主，所以每次 schema 重建后都要重新授权；角色不存在时静默跳过，
    RLS 用例会用明确的 skip 原因提示，而不是假绿。
    """
    try:
        with psycopg.connect(**TEST_DB) as conn:
            conn.execute(
                "DO $$ BEGIN "
                "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='llmwiki_app') THEN "
                "    GRANT USAGE ON SCHEMA public TO llmwiki_app; "
                "    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO llmwiki_app; "
                "    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO llmwiki_app; "
                "  END IF; "
                "END $$;")
    except Exception:               # 授权失败不影响其它用例（RLS 用例会自行 skip 并说明原因）
        pass


@pytest.fixture(scope="session", autouse=True)
def _close_pool_at_exit():
    """session 结束后关闭最后遗留的连接池，避免后台线程退出时挂起。"""
    yield
    try:
        import db
        db.close_pool()
    except Exception:
        pass


@pytest.fixture(scope="session")
def _isolated_root() -> Path:
    # 使用每次运行独立的系统临时目录，避免 Windows 下仓库内残留文件
    # 被编辑器/杀毒软件占用，导致整个测试集在 setup 阶段失败。
    isolated = Path(tempfile.mkdtemp(prefix="llmwiki-tests-"))
    yield isolated
    shutil.rmtree(isolated, ignore_errors=True)


@pytest.fixture
def tmp_path(_isolated_root: Path, request) -> Path:
    name = request.node.name.replace("/", "_").replace("\\", "_")
    p = _isolated_root / name
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)
    return p


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, request):
    """每个测试：指向测试库 + 重置 schema + 刷新 db 模块。

    无可用 PostgreSQL 时测试跳过（除非 PYTEST_SKIP_NO_DB 已设）。"""
    # 纯函数契约测试不需要重置共享 PostgreSQL，避免无关的数据库锁竞争。
    if request.node.get_closest_marker("no_db") is not None:
        return
    if not _pg_available():
        if os.environ.get("PYTEST_SKIP_NO_DB"):
            pytest.skip("未检测到可用 PostgreSQL（docker compose up -d db）")
        pytest.fail(
            "需要真实 PostgreSQL 运行测试：请 `docker compose up -d db`，"
            "并确认测试库 llmwiki_test 存在（或设 TEST_DB_NAME）。")
    monkeypatch.setenv("DB_HOST", TEST_DB["host"])
    monkeypatch.setenv("DB_PORT", str(TEST_DB["port"]))
    monkeypatch.setenv("DB_NAME", TEST_DB["dbname"])
    monkeypatch.setenv("DB_USER", TEST_DB["user"])
    monkeypatch.setenv("DB_PASS", TEST_DB["password"])
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    import db
    db.close_pool()          # 关闭上一测试遗留的池（避免后台线程泄漏）
    importlib.reload(db)     # 重载，令 DB_CONFIG 读到测试库 env
    import ops
    importlib.reload(ops)    # ops.KB_ROOT 随 env 重载（approve/reject/write_trigger 用）
    _reset_schema()
