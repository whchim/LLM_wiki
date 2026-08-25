# LLM Wiki Phase 2 SP1「数据地基」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Demo 数据层（SQLite 文件 + `streamlit_app/db.py`）整体迁移到 PostgreSQL 16 + pgvector，保持「YAML 规范源 / 数据库缓存」铁律，为 SP2-SP5 铺统一数据底座。

**Architecture:** PostgreSQL 16 独立服务（pgvector/pgvector:pg16 镜像）；`streamlit_app/db.py` 用同步 psycopg3 + `psycopg_pool.ConnectionPool` 改写，对外接口签名不变；Docker Compose 新增 `db` 服务 + `streamlit` depends_on healthy；测试连真实 PG（`docker compose up -d db`）。

**Tech Stack:** PostgreSQL 16 + pgvector、Python 3.11+、psycopg[binary] v3、psycopg_pool、Streamlit、Docker Compose。

**Spec:** [LLM_wiki_Phase2_SP1_设计文档.md](LLM_wiki_Phase2_SP1_设计文档.md)（v0.1，本文档实现依据）；范围与需求依 [LLM_wiki_Phase2_路线图.md](LLM_wiki_Phase2_路线图.md)、[LLM_wiki_PRD.md](LLM_wiki_PRD.md) v1.8。

## Global Constraints

- 所有知识文件、目录名、Prompt 输出使用中文（UTF-8 全链路）；代码内注释保持中文
- 数据库是缓存，YAML 是规范源；任何状态变更双写（YAML + PG），不一致时 YAML 为准
- `db.py` 对外接口签名**保持不变**（36 个函数/对象被 app.py/ops.py/review.py/growth.py/upload.py/rules.py 依赖，只改内部实现）
- 时间戳沿用 TEXT `YYYY-MM-DD HH:MM:SS`（本地时间，与 Phase 1 一致，不做时区迁移）
- `ai_scores` 升级为 JSONB（唯一 schema 变更，设计文档决策 3）
- 环境变量：`DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS`（替代 `DB_PATH`）；`KB_ROOT` 保留
- 开发范式：SDD（数据层输出可断言）+ TDD（迁移/一致性测试）；不引入 BDD
- 每次提交中文或英文 conventional commit 均可，提交信息描述实际变更
- **测试前置要求**：本地需可用 PostgreSQL（`docker compose up -d db` 或本机 PG）；沙箱/CI 无 Docker 时真实 PG 套件标记 skip，但本机必须跑绿再提交
- 旧数据导入：知识条目走 `rebuild_index()`（YAML 权威）；search_logs/pending_reviews/compile_tasks 走一次性 dump→import（不可重建历史不丢）

---

### Task 1: PostgreSQL 服务与连接配置

**Files:**
- Modify: `docker-compose.yml`（新增 `db` 服务 + healthcheck + `streamlit.depends_on`）
- Modify: `Dockerfile`（无本质改动；requirements 更新在 Task 2）
- Create: `docker/init-db.sh`（如需 pgvector 扩展初始化；pgvector/pgvector 镜像自带 `vector` 扩展，仅作文档化入口）
- Modify: `.env.example`（DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS 替代 DB_PATH）

**Interfaces:**
- Consumes: 无
- Produces: 可启动的 PostgreSQL `db` 服务；环境变量契约

- [ ] **Step 1: 更新 docker-compose.yml，新增 db 服务**

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
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

- [ ] **Step 2: 更新 .env.example，用 DB_* 替代 DB_PATH**

```env
# PostgreSQL 连接（替代 DB_PATH）
DB_HOST=localhost
DB_PORT=5432
DB_NAME=llmwiki
DB_USER=llmwiki
DB_PASS=llmwiki
KB_ROOT=./vault
```

- [ ] **Step 3: 验证** —— `docker compose up -d db && docker compose ps`（db healthy）；`docker exec <db> psql -U llmwiki -d llmwiki -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT 1"`

### Task 2: PostgreSQL schema.sql 重写

**Files:**
- Modify: `schema.sql`（SQLite → PG 全量重写，见设计文档第 4 节）
- Modify: `requirements.txt`（+`psycopg[binary]` +`psycopg_pool`）

**Interfaces:**
- Consumes: Task 1 的 db 服务
- Produces: 幂等 PG DDL（`IF NOT EXISTS`）；索引；pgvector 扩展

- [ ] **Step 1: 重写 schema.sql 为 PG DDL**

按设计文档第 4 节全文替换。要点：
- `CREATE EXTENSION IF NOT EXISTS vector;`
- `knowledge_entries.path TEXT PRIMARY KEY`（路径即身份）
- 自增 ID → `INTEGER GENERATED ALWAYS AS IDENTITY`（compile_tasks/pending_reviews/search_logs/audit_logs/conflicts）
- `ai_scores JSONB`
- 新增索引：`idx_entries_status/idx_entries_type/idx_tasks_status/idx_reviews_nexus/idx_reviews_human/idx_logs_source`
- 新增表：audit_logs / contributors（含 FK 到 knowledge_entries.path）/ conflicts

- [ ] **Step 2: requirements.txt 增加依赖**

```
psycopg[binary]>=3.1
psycopg_pool>=3.1
```

- [ ] **Step 3: 验证** —— 在 PG 上手动执行 schema.sql 两次确认幂等；`\dt` 列出 7 张表（4 现有 + audit/contributors/conflicts）+ `\dx` 见 vector

### Task 3: db.py 数据层改写为 psycopg3

**Files:**
- Modify: `streamlit_app/db.py`（内部全改，接口不变）

**Interfaces:**
- Consumes: Task 2 schema
- Produces: `get_conn()` 连接池 + 36 个函数的 PG 等价实现

- [ ] **Step 1: 重写连接层**

```python
os.environ DB_* 字典 → DB_CONFIG
_pool: ConnectionPool | None = None
_get_pool() → ConnectionPool(conninfo=_dsn(), min_size=1, max_size=5)
_dsn() → "host=... port=... dbname=... user=... password=..."
get_conn() → @contextmanager yield 池连接, commit/rollback/close 语义保持
```

- [ ] **Step 2: 逐函数迁移（SQL 语义等价）**

| 函数 | 迁移 |
|---|---|
| `ensure_schema()` | 目录自愈保留 + `CREATE EXTENSION vector` + 执行 schema.sql；从池取连接 |
| `upsert_entry()` | `INSERT ... ON CONFLICT(path) DO UPDATE` |
| `update_status()` | 原样改 `%s` |
| `move_entry()` | SELECT+DELETE+INSERT → `%s`；保持 KeyError 语义 |
| `insert_compile_task()` | `INSERT ... RETURNING id`（替代 lastrowid） |
| `update_compile_task()` | `%s` + `now()` |
| `insert_review()` | `INSERT ... RETURNING id`；ai_scores 传 JSON 字符串靠 JSONB 列自动转换 |
| `set_human_decision/resubmit_review()` | `%s` 原样 |
| `list_pending_reviews/list_rejected_reviews()` | join 查询保留；`cursor.description` 取列名兼容 |
| `insert_search_log/top_missed_queries/search_stats()` | `%s` + `now()` |
| `rebuild_index()` | DELETE + `ON CONFLICT DO UPDATE`；YAML 解析逻辑不动 |

- [ ] **Step 3: upload.py 直接 SQL 收敛**

把 `streamlit_app/upload.py:97-100` 的 `get_conn()` 直连 SQL 改为调用 db.py 既有 API 或等价 psycopg 查询，消除第二处 DB 访问点。

- [ ] **Step 4: 冒烟验证** —— 手动连 PG 调用 `ensure_schema()` + `upsert_entry()` + `rebuild_index()`，确认 CRUD 正常

### Task 4: 测试迁移到真实 PostgreSQL

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_schema.py`、`tests/test_ensure_schema.py`
- Modify: `tests/test_db_basic.py`、`test_db_growth.py`、`test_db_review.py`、`test_ops.py`、`test_upload_flow.py`
- Create: `tests/test_pg_migration.py`（SP1 专项）

**Interfaces:**
- Consumes: Task 1-3
- Produces: 全绿真实 PG 测试套件

- [ ] **Step 1: conftest.py 改连 PG**

移除 `DB_PATH` monkeypatch；autouse `_env` fixture 设 PG 连接 env（独立测试库，如 `DB_NAME=llmwiki_test`）+ `importlib.reload(db)`；提供 fixture 建/清测试库 + `ensure_schema()`

- [ ] **Step 2: schema 断言改 PG**

`test_schema.py` / `test_ensure_schema.py`：外部 `sqlite3` CLI → psycopg 连接查询；`sqlite_master` → `information_schema.tables` / `pg_catalog.pg_class`；`NOT LIKE 'sqlite_%'` → PG 系统表过滤

- [ ] **Step 3: 各数据层测试文件对齐**

`executescript(schema.sql)` → `ensure_schema()` 或 psycopg；断言列名/返回结构适配 psycopg 行对象

- [ ] **Step 4: 新增 test_pg_migration.py（SP1 专项）**

- `rebuild_index()` 从 YAML 重建后字段全等（含 department/status/version/updated_at）
- pgvector 扩展存在（`\dx`）
- `ON CONFLICT` upsert 幂等（同 path 两次 upsert 行数=1）
- `RETURNING id` 自增正确
- search_logs/pending_reviews/compile_tasks 导入脚本（Task 5）往返一致

- [ ] **Step 5: 全部测试绿** —— `docker compose up -d db && python -m pytest tests -q`

### Task 5: 旧数据导入脚本

**Files:**
- Create: `tools/migrate_to_pg.py`（或 `scripts/`，一次性 CLI）

**Interfaces:**
- Consumes: 旧 `vault/meta.db`（Phase 1）+ Task 2 schema
- Produces: PG 中知识条目重建 + 操作历史导入

- [ ] **Step 1: 知识条目重建**

遍历现有 vault，调用 `db.rebuild_index()` 从 YAML 重建 knowledge_entries（YAML 权威）

- [ ] **Step 2: 操作历史 dump→import**

`search_logs` / `pending_reviews` / `compile_tasks`：sqlite3 读出 → 批量写入 PG（保留 id/时间戳原值）；new 表 audit/contributors/conflicts 不迁

- [ ] **Step 3: 验证** —— 迁移后 PG 行数与 meta.db 一致（按表核对）；`rebuild_index` 条目数与 NEXUS md 数相符

### Task 6: 部署接线与文档收尾

**Files:**
- Modify: `Dockerfile`（如需 psycopg binary wheel 在 slim 基镜像，确认 `psycopg[binary]` 可装）
- Modify: `init.sh`（目录自愈保留；移除 sqlite3 建表，改说明 PG 由 Compose/ensure_schema 托管）
- Modify: `README.md`、`CLAUDE.md`、`.env.example`（DB 相关说明：SP1 起测试需本地/Docker PG；移除 meta.db 表述）
- Delete: `vault/meta.db`（退役，若已导入完成）

**Interfaces:**
- Consumes: Task 1-5
- Produces: 可 `docker compose up -d` 全量启动的部署

- [ ] **Step 1: 全量 Compose 启动验证**

`docker compose up -d --build` → streamlit 依赖 db healthy → 应用 `ensure_schema()` 自建表 → Streamlit 页面可用 + `rebuild_index` 可跑

- [ ] **Step 2: init.sh / README / CLAUDE.md 更新**

- init.sh：`sqlite3` 建表移除；保留目录自愈；说明 DB 由 compose/ensure_schema 托管
- README/CLAUDE.md：DB 由 SQLite 改为 PostgreSQL；测试运行前置（docker compose up -d db）；移除 meta.db 引用
- `.env.example`：DB_* 列全

- [ ] **Step 3: 移交 meta.db**

确认导入完成后 `vault/meta.db` 不再被引用，从 .gitignore 清单核对后退役

- [ ] **Step 4: 退出标准核对**（设计文档第 10 节逐项打勾）+ 提交

---

**验收（Done 定义）**：设计文档第 10 节全部满足；`docker compose up -d` 可跑通；`pytest tests -q` 全绿（含 test_pg_migration.py）；旧数据导入后行数核对一致。

**Changelog**：本计划 v0.1，随 SP1 实现推进更新。
