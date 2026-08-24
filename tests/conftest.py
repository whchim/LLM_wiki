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
from pathlib import Path

import pytest

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

ISOLATED = ROOT / "tests" / "_isolated"

# 测试库连接参数（与生产 llmwiki 区分，避免污染）
TEST_DB = {
    "host": os.environ.get("DB_HOST", "localhost"),
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
    if ISOLATED.exists():
        shutil.rmtree(ISOLATED)
    ISOLATED.mkdir(parents=True)
    yield ISOLATED


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
    _reset_schema()
