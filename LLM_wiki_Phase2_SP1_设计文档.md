# LLM Wiki 知识库平台 Phase 2 SP1「数据地基」设计文档

> **版本**：v0.1 ｜ **日期**：2026-08-19 ｜ **状态**：草案（待评审）
>
> **定位**：Phase 2 子项目 SP1 的详细设计（可直接编码）。依据 [LLM_wiki_Phase2_路线图.md](LLM_wiki_Phase2_路线图.md) SP1 子项目；需求冲突时以 [LLM_wiki_PRD.md](LLM_wiki_PRD.md) v1.8 为准。
>
> **范围**：SQLite → PostgreSQL 16 + pgvector 的全量迁移 + 数据层改写 + 部署改造。为 Phase 2 后续 SP2-SP5 铺设统一数据底座。

---

## 1. 目标与范围

**目标**：把 Demo 的数据层（SQLite 文件 + `streamlit_app/db.py`）整体迁移到 PostgreSQL 16 + pgvector 扩展，保持「YAML 为规范数据源、数据库为缓存」铁律，为多用户、向量检索、审计奠定基础。

**范围内**：
- 4 张现有表（knowledge_entries / compile_tasks / pending_reviews / search_logs）迁至 PG
- 提前建 4 张 Phase 2 新增表中的 3 张（audit_logs / contributors / conflicts；health_reports 留待 SP5 用到时按需建）——建表即可，行为逻辑在对应 SP 实现
- pgvector 扩展 + 预留向量列（SP4 用）
- `streamlit_app/db.py` 全部函数改写为 psycopg3，SQL 语义等价迁移
- Docker Compose 新增 PostgreSQL 服务；Dockerfile/Compose 接线
- 测试从 SQLite 全面迁到真实 PostgreSQL
- **一次性旧数据导入脚本**（第 8 节：知识条目 YAML 重建 + 操作历史 dump→import）

**范围外（后续 SP）**：FastAPI 后端（SP2）、增量编译 watcher（SP3）、混合检索算法（SP4）、巡检/演进/涌现逻辑（SP5）、health_reports 行为（SP5）。

## 2. 技术选型与架构决策

**决策（已拍板）**：
1. **PostgreSQL 16 + pgvector**——随迁移直接用 pgvector 扩展同库承载向量，不引入独立向量库（路线图决策 2）。
2. **测试连真 PG**——测试彻底从 SQLite 迁走，杜绝「迁移后被 SQLite 测试假绿」。
3. **部署**——docker-compose 新增 `db` 服务，Streamlit 连它（决策 1）。

**本设计补充决定**：
- **驱动**：`psycopg` v3（`psycopg[binary]`）。psycopg2 已进入维护期；psycopg3 支持 `%s` 占位、`RETURNING`、原生连接池。采用 **同步 psycopg3**（与当前全同步 Streamlit 保持一致；FastAPI 若需异步在 SP2 另行评估，不为未知需求预支复杂度）。
- **连接管理**：当前 `get_conn()` 是每次新建 SQLite 连接的 contextmanager。PG 应使用**连接池**（`psycopg_pool`，或 SP1 先用简单每调用短连接 + 进程内 SingleConnectionPool）。设计选 `psycopg_pool.ConnectionPool`（Streamlit 单进程单线程场景够用），对外接口保持 `get_conn()` 语义不变。
- **ai_scores 升级为 JSONB**：由 Demo 的 `TEXT('六维度 JSON')` 升为 `JSONB`。理由：SP5 健康巡检/冲突检测需按维度异步查询，JSONB 的 `->`/`@>` 运算符直接可写；TEXT 存 JSON 只能整块读出再解析。迁移本身就要动这条插值，早改成本最低，数据本就是合法 JSON 字符串，PG 自动转换，测试兜底。

## 3. 目录与文件变更

```
├── schema.sql                 # ⚠ 改为 PostgreSQL DDL（重写，中文注释保留）
├── docker-compose.yml         # 🔄 新增 db 服务（postgres:16 镜像 + pgvector 扩展初始化脚本）
├── Dockerfile                 # 🔄 requirements.txt 增加 psycopg；仍 COPY schema.sql
├── init.sh                    # 🔄 sqlite3 CLI 建表 → 保留目录自愈 + 说明/可选 PG 初始化入口
├── streamlit_app/
│   ├── db.py                  # 🔄 SQLite → psycopg3 全量改写（接口签名不变）
│   └── upload.py              # 🔄 97-100 行直接 SQL 改走 db API（或 psycopg 等价）
├── vault/                     # ⚠ meta.db 退役；保留 vault 目录（Obsidian 内容）
├── requirements.txt           # 🔄 +psycopg[binary] +psycopg_pool
├── .env.example               # 🔄 DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS（替代 DB_PATH）
└── tests/
    ├── conftest.py            # 🔄 DB_PATH monkeypatch → PG 库 amp + 连接参数
    ├── test_schema.py         # 🔄 sqlite3 CLI → psycopg/information_schema 断言
    ├── test_ensure_schema.py  # 🔄 sqlite_master → pg 系统表断言
    └── *.py                   # 🔄 各测试文件的建表方式与断言对齐 PG
```

## 4. PostgreSQL Schema（schema.sql 重写）

> 沿用 Demo 设计：path 为主键「路径即身份」、compile_tasks/pending_reviews 自增 ID。全部表 `IF NOT EXISTS` 幂等。时间戳沿用 TEXT `YYYY-MM-DD HH:MM:SS`（本地时间），与 Phase 1 一致，避免时区迁移负担；SP5 健康巡检要求的是日期精度。

```sql
-- 扩展（SP4 向量检索用；幂等）
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_entries (
    path        TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    department  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    version     TEXT NOT NULL DEFAULT 'V1.0',
    fingerprint TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_status ON knowledge_entries(status);
CREATE INDEX IF NOT EXISTS idx_entries_type ON knowledge_entries(type);

CREATE TABLE IF NOT EXISTS compile_tasks (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raw_path     TEXT NOT NULL,
    nexus_path   TEXT,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    error_msg    TEXT,
    started_at   TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON compile_tasks(status);

CREATE TABLE IF NOT EXISTS pending_reviews (
    id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nexus_path     TEXT NOT NULL,
    submitter      TEXT,
    department     TEXT,
    ai_verdict     TEXT,
    ai_scores      JSONB,          -- 由 TEXT('六维度 JSON') 升级为 JSONB
    human_decision TEXT,
    reject_reason  TEXT,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_nexus ON pending_reviews(nexus_path);
CREATE INDEX IF NOT EXISTS idx_reviews_human   ON pending_reviews(human_decision);

CREATE TABLE IF NOT EXISTS search_logs (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query        TEXT NOT NULL,
    match_count  INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'streamlit',
    timestamp    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_source ON search_logs(source);

-- Phase 2 新增（SP1 建表；行为在对应 SP 实现）
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operator    TEXT,
    action      TEXT NOT NULL,          -- upload/review/approve/reject/rebuild/...
    target_path TEXT,
    detail      JSONB,
    timestamp   TEXT NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS contributors (
    entry_path        TEXT NOT NULL REFERENCES knowledge_entries(path),
    user_id           TEXT NOT NULL,
    contribution_type TEXT NOT NULL,    -- submit/review/approve
    PRIMARY KEY (entry_path, user_id, contribution_type)
);
CREATE TABLE IF NOT EXISTS conflicts (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_a_path  TEXT NOT NULL,
    entry_b_path  TEXT NOT NULL,
    conflict_type TEXT NOT NULL,        -- factual_contradiction/duplicate/stale
    status        TEXT DEFAULT 'open',
    created_at    TEXT
);
```

**迁移映射要点（SQLite → PG）**：

| SQLite 写法 | PG 写法 | 涉及 |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `INTEGER GENERATED ALWAYS AS IDENTITY` | 3 表 |
| `INSERT OR REPLACE` | `INSERT ... ON CONFLICT(path) DO UPDATE` | db.py upsert_entry / rebuild_index |
| `sqlite3 lastrowid` | `INSERT ... RETURNING id` | insert_compile_task / insert_review |
| `datetime('now','localtime')` | `now()`（或 TIMESTAMP） | 多处 |
| `ai_scores TEXT(JSON)` | `ai_scores JSONB` | pending_reviews |
| `?` 占位符 | `%s` 占位符 | db.py 全部 |
| `PRAGMA journal_mode=WAL / busy_timeout` | 连接/池参数（PG 无需） | get_conn |

## 5. 数据层改写（streamlit_app/db.py）

**接口签名保持不变**（36 个函数/工具对外服务正依赖），仅替换内部实现：

```python
import os
from psycopg_pool import ConnectionPool
from contextlib import contextmanager
from typing import Iterator

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "localhost"),
    "port":     int(os.environ.get("DB_PORT", "5432")),
    "dbname":   os.environ.get("DB_NAME", "llmwiki"),
    "user":     os.environ.get("DB_USER", "llmwiki"),
    "password": os.environ.get("DB_PASS", "llmwiki"),
}
_SCHEMA = os.path.join(os.path.dirname(__file__), "..", "schema.sql")

_pool: ConnectionPool | None = None
_REQUIRED_DIRS = [...]  # 沿用 db.py:_REQUIRED_DIRS（目录自愈清单）

def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo=_dsn(), min_size=1, max_size=5)
    return _pool

def _dsn() -> str:
    return " ".join(f"{k}={v}" for k, v in DB_CONFIG.items())

def ensure_schema() -> None:
    for rel in _REQUIRED_DIRS:                      # 原目录自愈逻辑保留
        os.makedirs(os.path.join(KB_ROOT, rel), exist_ok=True)
    with get_conn() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")  # schema.sql 已含，双保险
        conn.execute(Path(_SCHEMA).read_text(encoding="utf-8"))
```

**每个函数的语义等价翻译**：

| 函数 | SQLite 原实现 | PG 等价实现 |
|---|---|---|
| `get_conn()` | sqlite3.connect + PRAGMA | 从连接池 `connection()`；commit/rollback/close 语义保持 |
| `upsert_entry()` | INSERT OR REPLACE | `INSERT ... ON CONFLICT(path) DO UPDATE SET type=EXCLUDED.type,...` |
| `update_status()` | UPDATE | 原样（`%s` 占位） |
| `move_entry()` | SELECT+DELETE+INSERT | 原样；可加事务 |
| `insert_compile_task()` | lastrowid | `INSERT ... RETURNING id` |
| `update_compile_task()` | COALESCE + datetime | `%s` + `now()` |
| `insert_review()` | lastrowid | `INSERT ... RETURNING id`（ai_scores 传 JSON 字符串，靠 JSONB 列自动转换） |
| `list_pending_reviews()` | SELECT * LIMIT 0 取列名 | 仍可 `SELECT * ... LIMIT 0` 取 `cursor.description`；psycopg 返回行支持 `.description` |
| `rebuild_index()` | DELETE + INSERT OR REPLACE | DELETE + `ON CONFLICT DO UPDATE` |

**upload.py:97 直接 SQL**：改为调用 `db.insert/update_compile_task` 既有 API 或加等价 psycopg 查询——统一收敛到 db.py，消除第二处 DB 访问。

## 6. 部署改造

```yaml
# docker-compose.yml
services:
  db:
    image: pgvector/pgvector:pg16        # pgvector 官方镜像（含扩展）
    environment:
      POSTGRES_DB: llmwiki
      POSTGRES_USER: llmwiki
      POSTGRES_PASSWORD: llmwiki
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U llmwiki -d llmwiki"]
      interval: 5s
      timeout: 3s
      retries: 10
  streamlit:
    build: .
    depends_on:
      db:
        condition: service_healthy
    environment:
      - KB_ROOT=/app/vault
      - DB_HOST=db
      - DB_PORT=5432
      - DB_NAME=llmwiki
      - DB_USER=llmwiki
      - DB_PASS=llmwiki
volumes:
  pgdata:
```

**vault/ 不再含 meta.db**——知识 Markdown 仍在 vault/ 卷挂载；数据库是独立 PG 卷。`ensure_schema()` 自愈逻辑保留（目录 + 建表幂等）。

**init.sh**：目录自愈保留；`sqlite3 meta.db` 建表改为「启动时由 ensure_schema() 自动建 PG 表」或可选 `psql` 入口。DB 初始化托管给 Compose db 服务。

> **连接配置**：本机开发用 `DB_HOST=localhost` 连 Docker PG 或用本机 PG；测试同样连 Docker PG。

## 7. 测试迁移

**前置**：测试需一个真实 PostgreSQL。提供 `docker compose up db` 或 `docker-compose up -d db` 供测试。

**conftest.py**：
- 移除 `DB_PATH` monkeypatch（SQLite 语义）
- 改为：为每个测试库使用**独立 PG 库**（如 `llmwiki_test` 或用 `tmp` schema），连接参数用环境变量 `DB_HOST/DB_PORT/DB_NAME_TEST/...`
- `_env` autouse fixture 改为设置 PG 连接 env + `importlib.reload(db)` 刷新池
- 提供 fixture：建库/清空/`ensure_schema()` 幂等调用（复用现有各测试文件的建表流程但换 psycopg）

**schema 断言**：
- `test_schema.py`：`sqlite_master` → `information_schema.tables` / `pg_class`；`sqlite3` 外部 CLI → psycopg 连接查询
- `test_ensure_schema.py`：`NOT LIKE 'sqlite_%'` → 改为断言 PG 系统表过滤
- 其它测试文件把 `executescript(schema.sql)` 的执行换成 `ensure_schema()` / psycopg。

**新增 SP1 专项测试**：
- 迁移一致性：样本 YAML ↔ PG 重建索引后字段全等
- pgvector 扩展存在
- `ON CONFLICT` upsert 幂等性
- `RETURNING id` 正确返回自增 id

**已知代价**：测试依赖 Docker PG（或本机 PG）——与 Demo「无外部服务也能跑 35 测试」的 claim 不同。文档需更新该表述为「SP1 起测试需本地/Docker PostgreSQL」。

## 8. 一致性保障（双写铁律迁至 PG）

- YAML（Obsidian 文件）仍是规范数据源；PG 表是物化缓存
- `rebuild_index()` 可全量从 YAML 重建——PG 库丢失/损坏只需重跑即可恢复（与 Demo 的「重建索引」按钮一致）
- **旧数据导入（一次性脚本，SP1 范围内）**：
  - **知识条目**（knowledge_entries）→ 走 `rebuild_index()` 从 YAML 重建（YAML 权威，丢弃缓存层陈旧字段）
  - **不可重建的操作历史**（search_logs / pending_reviews / compile_tasks）→ 走一次性的 SQLite dump → PG import 脚本（process 级历史不可丢）
  - 新表 audit_logs / contributors / conflicts Demo 无数据，不迁移
  - DEMO 的 meta.db 退役前，导入脚本负责把上述两类数据搬入 PG

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 测试需 Docker PG 破坏「沙箱可跑」 | 接受并更新文档表述；提供 `docker compose up -d db` 单命令 |
| 窗口沙箱无 Docker | SP1 属架构迁移必须真 PG；若受限环境无法验证，标记 skip 但仍保真 PG 套件在可用环境跑 |
| 迁移后行为漂移（时间格式/JSON 列） | 测试覆盖 created_at/ai_scores/status 语义等价 |
| psycopg_pool 资源泄漏（Streamlit 长驻） | 连接池 max_size 受限；ensure_schema 复用池 |

## 10. 退出标准（Done 定义）

- [ ] `schema.sql` 为 PG DDL，`ensure_schema()` 在 PG 上幂等建表成功
- [ ] db.py 全部函数 psycopg3 实现，接口签名不变；`insert_*` 返回 id 正确
- [ ] 全部 5 个数据层测试文件 + 2 个 schema 测试文件迁到真 PG 并转绿
- [ ] 新增 SP1 专项测试（重建一致性 / pgvector / upsert 幂等 / RETURNING id）绿
- [ ] docker-compose 起 `db` + `streamlit`，应用连 PG 正常工作（含 `rebuild_index`）
- [ ] vault/meta.db 退役；`.env.example`/README/CLAUDE.md 中 DB 相关说明更新

---

## Changelog

- **v0.1（2026-08-19）**：初稿。基于路线图 SP1 与 Explore 调研（schema.sql 4 表、db.py 单一数据层、部署现状）；部署决策=Compose+PG 服务、测试决策=连真 PG；ai_scores 由 TEXT 升 JSONB；提前建 audit_logs/contributors/conflicts（health_reports 留 SP5）。
- **v0.1 评审修订**：第 8 节补「一次性旧数据导入脚本」——知识条目走 rebuild_index（YAML 权威），search_logs/pending_reviews/compile_tasks 走 SQLite dump→PG import（不可重建历史不丢）；明确 psycopg3 同步驱动（异步留 SP2 独立评估）。