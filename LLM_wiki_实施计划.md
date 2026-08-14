# LLM Wiki 知识库平台 Demo 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按设计文档 v0.1 实现 LLM Wiki 知识库平台 Demo——上传文档 → Claude Code 编译 → AI 审核 → 人工通过 → Obsidian 检索的完整闭环，2-4 周可交付。

**Architecture:** Claude Code（Bash 直连 Vault/SQLite）为唯一 LLM 引擎；Obsidian 为知识界面；Streamlit 为轻量管理层（直接读写文件系统 + SQLite）；触发文件信号（`vault/_triggers/`）为 Streamlit → Claude Code 的桥。无后端服务、无向量检索（Phase 2）。

**Tech Stack:** Python 3.11+（Streamlit 容器）、SQLite（WAL）、bash（init.sh/hooks）、Claude Code（Agent + Harness + SessionStart hook）、Obsidian Desktop（用户侧）、Docker Compose。

**Spec:** [LLM_wiki_设计文档.md](LLM_wiki_设计文档.md)（v0.1，本文档的实现依据，执行者需同时阅读；需求冲突时以 [LLM_wiki_PRD.md](LLM_wiki_PRD.md) v1.7 为准）

## Global Constraints

- 无额外 LLM Key——LLM 能力全部经 Claude Code 提供（`ANTHROPIC_API_KEY` 由 Claude Code 自身管理，本仓库不配置）
- 所有知识文件、目录名、Prompt 输出使用中文（UTF-8 全链路，Windows + Docker 挂载不做转码）
- 路径约定：Vault 相对路径（如 `NEXUS/概念/应急哨兵.md`）为知识条目唯一标识；代码内不写绝对路径，统一从 `KB_ROOT`/`DB_PATH` 环境变量取（容器内 `/app/vault`）
- 双写铁律：任何状态变更必须同时写 YAML 文件 + SQLite（设计文档 4.1）；YAML 永远为准
- 触发文件原子写：先写 `.tmp_<name>` 再 `mv`（设计文档 4.5）
- 概念页状态机：pending（`pending_review/`）→ 人工通过 → active（`NEXUS/概念/`）；资源摘要不过审直接 active（设计文档 4.4）
- 开发范式：SDD（编译引擎/检索）+ TDD（审核确定性规则/数据层）；不引入 BDD/战术 DDD
- 每次提交使用中文或英文 conventional commit 均可，提交信息描述实际变更
- 种子数据依赖：真实华泰智远文档由用户提供（设计文档 9.2），本计划仅含 3 份测试样例

---

### Task 1: 仓库初始化与脚手架（git init + 目录树 + init.sh + SCHEMA.md）

**Files:**
- Create: `.gitignore`
- Create: `init.sh`
- Create: `vault/SCHEMA.md`（由 init.sh 生成）
- Create: `vault/NEXUS/index.md`、`vault/NEXUS/log.md`（由 init.sh 生成）

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces: 完整目录树（设计文档 3.1）；`init.sh`（幂等初始化入口，Task 2 将 DDL 并入）

- [ ] **Step 1: 初始化 git 仓库并创建 .gitignore**

```bash
cd "d:\桌面\LLM_wiki" && git init
```

创建 `.gitignore`：

```gitignore
# Python
__pycache__/
*.pyc
.venv/

# SQLite
vault/meta.db
vault/meta.db-*

# OS
.DS_Store
Thumbs.db

# 用户本地配置（不提交）
.claude/settings.local.json
```

> 注意：`vault/` 目录整体**要**提交（它是知识库，git 可追踪是产品卖点），只忽略数据库文件。

- [ ] **Step 2: 创建 init.sh（幂等，设计文档 3.2 全文落地）**

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VAULT="$ROOT/vault"

# 1. 目录树（mkdir -p 幂等）
mkdir -p "$VAULT"/RAW/{个人_notes,会议,经验,项目}
mkdir -p "$VAULT"/pending_review
mkdir -p "$VAULT"/NEXUS/{资源,概念,研究}
mkdir -p "$VAULT"/_triggers/done

# 2. Reserved Files（已存在则跳过，不覆盖）
[ -f "$VAULT/NEXUS/index.md" ] || printf '# 知识库索引\n\n（编译时由 Claude Code 逐次更新）\n' > "$VAULT/NEXUS/index.md"
[ -f "$VAULT/NEXUS/log.md" ]   || : > "$VAULT/NEXUS/log.md"

# 3. SCHEMA.md（已存在则跳过）
if [ ! -f "$VAULT/SCHEMA.md" ]; then
  cat > "$VAULT/SCHEMA.md" << 'EOF'
# 知识库 Schema
## 1. 合法 Type 列表
- concept / resource / research / glossary
## 2. 合法 Status 列表
- draft / pending / active / stale / deprecated
## 3. 合法 Tags 命名空间
- 部门: 销售/售前/产品/实施交付/开发/财务/人事/行政/共享层
- 领域: AI/大数据/云计算/安全/项目管理/产品设计/应急管理/智慧城市/物联网/数字孪生
- 类型: 实战经验/技术方案/产品文档/会议纪要/复盘总结/行业研究/标准规范/培训材料
## 4. Frontmatter 字段规范（详见设计文档 4.2）
## 5. 文件名与 Wikilink 约定（详见设计文档 5.4）
## 6. 版本号规则
- 格式 V{major}.{minor}，首次入库 V1.0；正文微调 V1.1；核心定义改写 V2.0
EOF
fi

echo "✅ 初始化完成。下一步：docker compose up -d 启动 Streamlit；Obsidian 打开 $VAULT"
```

- [ ] **Step 3: 运行两次验证幂等**

```bash
cd "d:\桌面\LLM_wiki" && bash init.sh && bash init.sh
```

Expected: 两次均输出 `✅ 初始化完成`，无报错；`vault/NEXUS/index.md` 内容未被第二次执行覆盖（仍为初始一行标题）。

- [ ] **Step 4: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add .gitignore init.sh vault/ && git commit -m "chore: 初始化仓库、Vault 目录树与 init.sh 幂等脚本"
```

---

### Task 2: SQLite 建表（schema.sql 提取 + init.sh 集成）

> 实现级细化：设计文档 3.2 将 DDL 内联在 init.sh 中。本计划提取为独立 `schema.sql` 以便测试（Task 2 和 Task 5 的 rebuild_index 测试都直接依赖可单独执行的 DDL）。

**Files:**
- Create: `schema.sql`
- Modify: `init.sh`（第 4 步改为执行 `sqlite3 "$VAULT/meta.db" < "$ROOT/schema.sql"`）

**Interfaces:**
- Consumes: Task 1 的目录树
- Produces: `vault/meta.db` 含 4 张表（知识条目/编译任务/审核记录/搜索日志），字段与设计文档 4.3 一致

- [ ] **Step 1: 创建 schema.sql（设计文档 4.3 DDL 全文）**

```sql
-- vault/meta.db 的建表脚本（由 init.sh 执行；Task 2/5 测试直接引用）
CREATE TABLE IF NOT EXISTS knowledge_entries (
    path        TEXT PRIMARY KEY,          -- Vault 相对路径，如 NEXUS/概念/应急哨兵.md
    type        TEXT NOT NULL,             -- concept/resource/research/glossary
    title       TEXT NOT NULL,
    department  TEXT,                      -- 9 部门 + 共享层
    status      TEXT NOT NULL DEFAULT 'pending',  -- draft/pending/active/stale/deprecated
    version     TEXT NOT NULL DEFAULT 'V1.0',
    fingerprint TEXT,                      -- 源文件 SHA256
    updated_at  TEXT                       -- YYYY-MM-DD
);
CREATE TABLE IF NOT EXISTS compile_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_path     TEXT NOT NULL,
    nexus_path   TEXT,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/done/failed/cached
    error_msg    TEXT,
    started_at   TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS pending_reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nexus_path    TEXT NOT NULL,
    submitter     TEXT,
    department    TEXT,
    ai_verdict    TEXT,                    -- approved/rejected/needs_human_review
    ai_scores     TEXT,                    -- 六维度 JSON
    human_decision TEXT,                   -- approved/rejected，NULL=未处理
    reject_reason TEXT,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS search_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL,
    match_count INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'streamlit',  -- streamlit/claude_code
    timestamp   TEXT NOT NULL
);
```

- [ ] **Step 2: 修改 init.sh 集成 schema.sql**

将 Task 1 中 init.sh 的 `# 4. SQLite 建表` 段整体替换为：

```bash
# 4. SQLite 建表（IF NOT EXISTS 幂等）
sqlite3 "$VAULT/meta.db" < "$ROOT/schema.sql"
```

（原第 4 段内联 DDL 删除。）

- [ ] **Step 3: 写测试验证建表**

创建 `tests/test_schema.py`：

```python
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def test_schema_creates_expected_tables(tmp_path):
    db = tmp_path / "meta.db"
    subprocess.run(["sqlite3", str(db), f".read {ROOT / 'schema.sql'}"], check=True,
                   capture_output=True, text=True)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"knowledge_entries", "compile_tasks", "pending_reviews", "search_logs"} <= tables

def test_schema_is_idempotent(tmp_path):
    db = tmp_path / "meta.db"
    for _ in range(2):
        subprocess.run(["sqlite3", str(db), f".read {ROOT / 'schema.sql'}"], check=True,
                       capture_output=True, text=True)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    assert n == 4
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_schema.py -v`
Expected: 2 passed。（若本机无 pytest：`pip install pytest`；若本机无 sqlite3 CLI，可用 `python -c "import sqlite3; print(sqlite3.sqlite_version)"` 确认后在测试中用 Python 执行 schema——见 Task 2 附注。**本机无 Python 时跳过本步，待 Task 13 容器/CI 环境统一执行。**）

> 附注：Windows Git Bash 自带 sqlite3 通常可用。若不可用，将两个测试的 `subprocess.run` 改为 `sqlite3.connect(db).executescript(ROOT.joinpath("schema.sql").read_text(encoding="utf-8"))`。

- [ ] **Step 5: 运行 init.sh 并验证建表**

```bash
cd "d:\桌面\LLM_wiki" && bash init.sh && sqlite3 vault/meta.db ".tables"
```

Expected: 输出 `compile_tasks  knowledge_entries  pending_reviews  search_logs`。

- [ ] **Step 6: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add schema.sql init.sh tests/test_schema.py && git commit -m "feat: SQLite 建表脚本与幂等测试"
```

---

### Task 3: db.py 基础函数（连接 + 条目缓存：upsert/update_status/move_entry）

**Files:**
- Create: `streamlit_app/db.py`（本任务实现其中 4 个函数）
- Test: `tests/test_db_basic.py`

**Interfaces:**
- Consumes: `schema.sql`（Task 2）
- Produces: `db.get_conn()`、`db.upsert_entry(path, type_, title, department, status, version, fingerprint, updated_at)`、`db.update_status(path, status)`、`db.move_entry(old_path, new_path, status)`——签名与设计文档 9.2 一致

- [ ] **Step 1: 写失败测试**

```python
import sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))
os.environ.setdefault("DB_PATH", str(ROOT / "vault" / "meta.db"))
os.environ.setdefault("KB_ROOT", str(ROOT / "vault"))

from db import get_conn, upsert_entry, update_status, move_entry
from db import DB_PATH  # 模块内基于 DB_PATH 初始化

def _fresh_db(tmp_path):
    import sqlite3
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript(Path(ROOT / "schema.sql").read_text(encoding="utf-8"))
    os.environ["DB_PATH"] = str(db)
    import importlib, db as m
    return importlib.reload(m), db

def test_upsert_entry_inserts_and_updates(tmp_path):
    m, db = _fresh_db(tmp_path)
    m.upsert_entry("NEXUS/概念/应急哨兵.md", "concept", "应急哨兵", "产品", "pending", "V1.0", "abc", "2026-08-13")
    m.upsert_entry("NEXUS/概念/应急哨兵.md", "concept", "应急哨兵", "产品", "active", "V1.0", "abc", "2026-08-13")
    with m.get_conn() as conn:
        rows = conn.execute("SELECT status FROM knowledge_entries WHERE path=?", ("NEXUS/概念/应急哨兵.md",)).fetchall()
    assert rows == [("active",)]  # upsert 不产生重复行

def test_update_status(tmp_path):
    m, db = _fresh_db(tmp_path)
    m.upsert_entry("p.md", "concept", "x", None, "pending", "V1.0", None, "2026-08-13")
    m.update_status("p.md", "active")
    with m.get_conn() as conn:
        assert conn.execute("SELECT status FROM knowledge_entries WHERE path='p.md'").fetchone()[0] == "active"

def test_move_entry_changes_path_keeps_row_count(tmp_path):
    m, db = _fresh_db(tmp_path)
    m.upsert_entry("pending_review/应急哨兵.md", "concept", "应急哨兵", "产品", "pending", "V1.0", None, "2026-08-13")
    m.move_entry("pending_review/应急哨兵.md", "NEXUS/概念/应急哨兵.md", "active")
    with m.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
        assert n == 1
        row = conn.execute("SELECT path, status FROM knowledge_entries").fetchone()
        assert row == ("NEXUS/概念/应急哨兵.md", "active")
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_db_basic.py -v`
Expected: FAIL——`ModuleNotFoundError: No module named 'db'`（db.py 尚未创建）。

- [ ] **Step 3: 实现 db.py 基础函数**

```python
"""SQLite 数据访问层（设计文档 9.2）。所有表操作唯一入口。"""
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "vault", "meta.db"))
KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """SQLite 连接上下文。WAL 模式，busy_timeout=5s。"""
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_entry(path: str, type_: str, title: str,
                 department: str | None, status: str,
                 version: str, fingerprint: str | None,
                 updated_at: str) -> None:
    """INSERT OR REPLACE INTO knowledge_entries。"""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO knowledge_entries
               (path, type, title, department, status, version, fingerprint, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (path, type_, title, department, status, version, fingerprint, updated_at))


def update_status(path: str, status: str) -> None:
    """更新 knowledge_entries.status（不触碰文件，文件由调用方改）。"""
    with get_conn() as conn:
        conn.execute("UPDATE knowledge_entries SET status=? WHERE path=?", (status, path))


def move_entry(old_path: str, new_path: str, status: str) -> None:
    """DELETE 旧 path 行 + INSERT 新 path 行（保留原字段）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type, title, department, version, fingerprint, updated_at "
            "FROM knowledge_entries WHERE path=?", (old_path,)).fetchone()
        if row is None:
            raise KeyError(f"knowledge_entries 无此路径: {old_path}")
        conn.execute("DELETE FROM knowledge_entries WHERE path=?", (old_path,))
        conn.execute(
            "INSERT INTO knowledge_entries (path, type, title, department, status, version, fingerprint, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (new_path, row[0], row[1], row[2], status, row[3], row[4], row[5]))
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_db_basic.py -v`
Expected: 3 passed。

- [ ] **Step 5: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add streamlit_app/db.py tests/test_db_basic.py && git commit -m "feat: db.py 基础数据访问（条目缓存 upsert/status/move）"
```

---

### Task 4: db.py 编译任务与审核函数

**Files:**
- Modify: `streamlit_app/db.py`（追加 7 个函数）
- Test: `tests/test_db_review.py`

**Interfaces:**
- Consumes: Task 3 的 `get_conn`
- Produces: `insert_compile_task(raw_path, fingerprint) -> int`、`update_compile_task(task_id, status, nexus_path=None, error_msg=None)`、`insert_review(nexus_path, submitter, department, ai_verdict, ai_scores) -> int`、`set_human_decision(review_id, decision, reject_reason=None)`、`resubmit_review(review_id)`、`list_pending_reviews() -> list[dict]`、`list_rejected_reviews() -> list[dict]`（签名与设计文档 9.2 一致）

- [ ] **Step 1: 写失败测试**

```python
import sys, os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import sqlite3
from db import (get_conn, insert_compile_task, update_compile_task,
                insert_review, set_human_decision, resubmit_review,
                list_pending_reviews, list_rejected_reviews)

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    monkeypatch.setenv("DB_PATH", str(db))
    import importlib, db as m
    yield importlib.reload(m)

def test_compile_task_lifecycle(fresh_db):
    m = fresh_db
    tid = m.insert_compile_task("RAW/项目/a.md", "sha256abc")
    m.update_compile_task(tid, "done", nexus_path="NEXUS/资源/a.md")
    with m.get_conn() as conn:
        row = conn.execute("SELECT status, nexus_path FROM compile_tasks WHERE id=?", (tid,)).fetchone()
    assert row == ("done", "NEXUS/资源/a.md")

def test_review_insert_and_list(fresh_db):
    m = fresh_db
    rid = m.insert_review("pending_review/应急哨兵.md", "demo_user", "产品", "approved",
                          '{"verdict":"approved"}')
    m.set_human_decision(rid, "approved")
    with m.get_conn() as conn:
        row = conn.execute("SELECT human_decision FROM pending_reviews WHERE id=?", (rid,)).fetchone()
    assert row == ("approved",)
    assert len(m.list_pending_reviews()) == 0  # 已处理不再出现在待审列表
    m.insert_review("pending_review/叫应体系.md", "demo_user", "售前", "rejected", "{}")
    assert len(m.list_pending_reviews()) == 1

def test_resubmit_clears_decision(fresh_db):
    m = fresh_db
    rid = m.insert_review("pending_review/x.md", "demo_user", "产品", "approved", "{}")
    m.set_human_decision(rid, "rejected", "内容不足")
    m.resubmit_review(rid)
    with m.get_conn() as conn:
        row = conn.execute("SELECT human_decision, reject_reason FROM pending_reviews WHERE id=?", (rid,)).fetchone()
    assert row == (None, None)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_db_review.py -v`
Expected: FAIL——`ImportError: cannot import name 'insert_compile_task'`。

- [ ] **Step 3: 追加实现到 db.py**

```python
# ---- 编译任务 ----
def insert_compile_task(raw_path: str, fingerprint: str) -> int:
    """插入 status='pending' 任务，返回 id。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status, started_at) VALUES (?,?,'pending',datetime('now','localtime'))",
            (raw_path, fingerprint))
        return cur.lastrowid


def update_compile_task(task_id: int, status: str,
                        nexus_path: str | None = None,
                        error_msg: str | None = None) -> None:
    """更新任务状态与完成时间。"""
    with get_conn() as conn:
        if status in ("done", "failed", "cached"):
            conn.execute(
                "UPDATE compile_tasks SET status=?, nexus_path=COALESCE(?,nexus_path), error_msg=?, completed_at=datetime('now','localtime') WHERE id=?",
                (status, nexus_path, error_msg, task_id))
        else:
            conn.execute("UPDATE compile_tasks SET status=? WHERE id=?", (status, task_id))


# ---- 审核 ----
def insert_review(nexus_path: str, submitter: str, department: str,
                  ai_verdict: str, ai_scores: str) -> int:
    """插入 AI 审核结果，返回 id。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO pending_reviews (nexus_path, submitter, department, ai_verdict, ai_scores, created_at) "
            "VALUES (?,?,?,?,?,datetime('now','localtime'))",
            (nexus_path, submitter, department, ai_verdict, ai_scores))
        return cur.lastrowid


def set_human_decision(review_id: int, decision: str,
                       reject_reason: str | None = None) -> None:
    """人工通过/驳回。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pending_reviews SET human_decision=?, reject_reason=? WHERE id=?",
            (decision, reject_reason, review_id))


def resubmit_review(review_id: int) -> None:
    """重新提交审核：human_decision 置 NULL、清空 reject_reason。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pending_reviews SET human_decision=NULL, reject_reason=NULL WHERE id=?",
            (review_id,))


def list_pending_reviews() -> list[dict]:
    """human_decision IS NULL 的审核记录（含条目标题）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pr.*, e.title FROM pending_reviews pr "
            "LEFT JOIN knowledge_entries e ON e.path=pr.nexus_path "
            "WHERE pr.human_decision IS NULL ORDER BY pr.created_at DESC").fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM pending_reviews LIMIT 0").description]
        return [dict(zip(cols + ["title"], r)) for r in rows]


def list_rejected_reviews() -> list[dict]:
    """human_decision='rejected' 的记录（可重新提交）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pr.*, e.title FROM pending_reviews pr "
            "LEFT JOIN knowledge_entries e ON e.path=pr.nexus_path "
            "WHERE pr.human_decision='rejected' ORDER BY pr.created_at DESC").fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM pending_reviews LIMIT 0").description]
        return [dict(zip(cols + ["title"], r)) for r in rows]
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_db_review.py -v`
Expected: 3 passed。

- [ ] **Step 5: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add streamlit_app/db.py tests/test_db_review.py && git commit -m "feat: db.py 编译任务与审核函数"
```

---

### Task 5: db.py 搜索日志与重建索引 + ops.py 操作逻辑

**Files:**
- Modify: `streamlit_app/db.py`（追加 4 个函数）
- Create: `streamlit_app/ops.py`（纯操作逻辑：触发文件写入、上传校验、通过/驳回动作——可单测，UI 只做展示）
- Test: `tests/test_db_growth.py`、`tests/test_ops.py`

**Interfaces:**
- Consumes: Task 3/4 的 db.py 函数
- Produces:
  - `db.insert_search_log(query, match_count, source)`、`db.top_missed_queries(limit=20) -> list[dict]`、`db.search_stats() -> dict`、`db.rebuild_index() -> int`
  - `ops.write_trigger(kind: str, paths: list[str], source: str) -> str`（返回触发文件路径，原子写）
  - `ops.validate_upload(filename: str, size: int) -> str | None`（返回 None=合法，否则错误文案）
  - `ops.approve_entry(review_id: int, old_path: str, new_path: str) -> None`（移动文件 + 改 YAML + db 双写 + 追加 index.md）
  - `ops.reject_entry(review_id: int, path: str, reason: str) -> None`
  - `ops.resubmit(review_id: int, path: str) -> None`
  - `ops.sha256_file(path: str) -> str`

- [ ] **Step 1: 写失败测试（db 增长函数）**

```python
import sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import sqlite3
from db import insert_search_log, top_missed_queries, search_stats, get_conn

def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    monkeypatch.setenv("DB_PATH", str(db))
    import importlib, db as m
    return importlib.reload(m)

def test_search_logs_aggregation(tmp_path, monkeypatch):
    m = _setup(tmp_path, monkeypatch)
    m.insert_search_log("区块链", 0, "streamlit")
    m.insert_search_log("区块链", 0, "claude_code")
    m.insert_search_log("应急哨兵", 3, "streamlit")
    top = m.top_missed_queries(10)
    assert top[0]["query"] == "区块链" and top[0]["cnt"] == 2
    stats = m.search_stats()
    assert stats["total"] == 3 and stats["miss_count"] == 2

def test_rebuild_index_from_files(tmp_path, monkeypatch):
    m = _setup(tmp_path, monkeypatch)
    kb = tmp_path / "vault"
    (kb / "NEXUS" / "概念").mkdir(parents=True)
    (kb / "NEXUS" / "资源").mkdir(parents=True)
    (kb / "pending_review").mkdir()
    (kb / "NEXUS" / "概念" / "应急哨兵.md").write_text(
        "---\ntype: concept\ntitle: 应急哨兵\nstatus: active\ndepartment: 产品\n---\n正文", encoding="utf-8")
    (kb / "NEXUS" / "资源" / "白皮书.md").write_text(
        "---\ntype: resource\ntitle: 白皮书\nstatus: active\n---\n正文", encoding="utf-8")
    (kb / "pending_review" / "叫应体系.md").write_text(
        "---\ntype: concept\ntitle: 叫应体系\nstatus: pending\n---\n正文", encoding="utf-8")
    monkeypatch.setenv("KB_ROOT", str(kb))
    import importlib, db as m2
    m2 = importlib.reload(m2)
    n = m2.rebuild_index()
    assert n == 3
    with m2.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0] == 3
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_db_growth.py -v`
Expected: FAIL——`AttributeError: module 'db' has no attribute 'insert_search_log'`。

- [ ] **Step 3: 实现 db.py 增长函数**

```python
# ---- 搜索日志与看板 ----
def insert_search_log(query: str, match_count: int, source: str) -> None:
    """写入搜索日志（timestamp 本地时间）。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO search_logs (query, match_count, source, timestamp) VALUES (?,?,?,datetime('now','localtime'))",
            (query, match_count, source))


def top_missed_queries(limit: int = 20) -> list[dict]:
    """match_count=0 的 query 按次数降序。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT query, COUNT(*) AS cnt, MAX(timestamp) AS last_seen "
            "FROM search_logs WHERE match_count=0 GROUP BY query ORDER BY cnt DESC, last_seen DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(zip(["query", "cnt", "last_seen"], r)) for r in rows]


def search_stats() -> dict:
    """{total, miss_count, miss_rate}。"""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
        miss = conn.execute("SELECT COUNT(*) FROM search_logs WHERE match_count=0").fetchone()[0]
        return {"total": total, "miss_count": miss, "miss_rate": round(miss / total, 2) if total else 0.0}


# ---- 重建索引 ----
def rebuild_index() -> int:
    """扫描 KB_ROOT 下 NEXUS/**/*.md 与 pending_review/*.md，解析 YAML 重建表。返回条目数。"""
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
                    text = open(full, encoding="utf-8").read()
                    if not text.startswith("---"):
                        continue
                    _, fm, _ = text.split("---", 2)
                    meta = yaml.safe_load(fm) or {}
                    conn.execute(
                        "INSERT OR REPLACE INTO knowledge_entries "
                        "(path, type, title, department, status, version, fingerprint, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (rel, meta.get("type", "concept"), meta.get("title", fn[:-3]),
                         meta.get("department"), meta.get("status", "active"),
                         meta.get("version", "V1.0"), meta.get("fingerprint"),
                         meta.get("updated", meta.get("created"))))
                    count += 1
    return count
```

- [ ] **Step 4: 运行增长测试验证通过**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_db_growth.py -v`
Expected: 2 passed。

- [ ] **Step 5: 写 ops.py 失败测试**

```python
import sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import sqlite3
from ops import write_trigger, validate_upload, approve_entry, reject_entry, resubmit, sha256_file

def test_write_trigger_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    (tmp_path / "_triggers").mkdir()
    import ops
    p = ops.write_trigger("compile", ["RAW/a.md", "RAW/b.md"], "streamlit")
    assert p.name.startswith("compile_") and p.name.endswith(".md")
    assert not list((tmp_path / "_triggers").glob(".tmp_*"))  # 无残留临时文件
    text = p.read_text(encoding="utf-8")
    assert "kind: compile" in text and "RAW/a.md" in text

def test_validate_upload():
    assert validate_upload("a.md", 1024) is None
    assert validate_upload("a.jpg", 1024) is not None
    assert validate_upload("a.pdf", 11 * 1024 * 1024) is not None

def test_approve_entry_moves_and_double_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sqlite3.connect(tmp_path / "t.db").executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    (tmp_path / "pending_review").mkdir(); (tmp_path / "NEXUS" / "概念").mkdir(parents=True)
    src = tmp_path / "pending_review" / "应急哨兵.md"
    src.write_text("---\ntype: concept\ntitle: 应急哨兵\nstatus: pending\n---\n正文", encoding="utf-8")
    import importlib, db, ops
    db = importlib.reload(db); ops = importlib.reload(ops)
    db.upsert_entry("pending_review/应急哨兵.md", "concept", "应急哨兵", "产品", "pending", "V1.0", None, "2026-08-13")
    rid = db.insert_review("pending_review/应急哨兵.md", "demo_user", "产品", "approved", "{}")
    ops.approve_entry(rid, "pending_review/应急哨兵.md", "NEXUS/概念/应急哨兵.md")
    assert not src.exists()
    assert (tmp_path / "NEXUS" / "概念" / "应急哨兵.md").exists()
    text = (tmp_path / "NEXUS" / "概念" / "应急哨兵.md").read_text(encoding="utf-8")
    assert "status: active" in text
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM knowledge_entries WHERE path='NEXUS/概念/应急哨兵.md'").fetchone()[0] == "active"
        assert conn.execute("SELECT human_decision FROM pending_reviews WHERE id=?", (rid,)).fetchone()[0] == "approved"
```

- [ ] **Step 6: 运行 ops 测试验证失败**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_ops.py -v`
Expected: FAIL——`ModuleNotFoundError: No module named 'ops'`。

- [ ] **Step 7: 实现 ops.py**

```python
"""纯操作逻辑：触发文件、上传校验、审核动作。UI 只调用这些函数，不做业务。"""
import os
import re
import hashlib
from datetime import datetime
from pathlib import Path

from db import (get_conn, update_status, move_entry, insert_search_log,
                set_human_decision, resubmit_review)

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))
ALLOWED_EXTS = {".md", ".txt", ".pdf", ".docx"}
MAX_SIZE = 10 * 1024 * 1024  # 10MB


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_upload(filename: str, size: int) -> str | None:
    """返回 None=合法；否则返回错误文案（PRD 9.3 文案）。"""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return f"不支持的文件格式：{ext}。支持的格式：.md, .txt, .pdf, .docx"
    if size > MAX_SIZE:
        return f"文件大小超过限制（10MB）。当前文件大小：{size / 1024 / 1024:.1f}MB"
    return None


def write_trigger(kind: str, paths: list[str], source: str) -> Path:
    """原子写触发文件（.tmp + mv），返回最终路径。kind: compile|review"""
    trig_dir = Path(KB_ROOT) / "_triggers"
    trig_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final = trig_dir / f"{kind}_{ts}.md"
    tmp = trig_dir / f".tmp_{final.name}"
    body = f"""---
type: trigger
kind: {kind}
created: "{datetime.now().isoformat(timespec='seconds')}"
source: {source}
---
""" + "\n".join(f"- {p}" for p in paths) + "\n"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(final)  # 原子重命名
    return final


def _yaml_edit(path: Path, field: str, value: str) -> None:
    """就地修改 Markdown 文件的 YAML Frontmatter 中某字段。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(rf"^{field}: .*$", f"{field}: {value}", text, count=1, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


def approve_entry(review_id: int, old_path: str, new_path: str) -> None:
    """通过：移动文件 + YAML status=active + db 双写 + 追加 index.md。"""
    src = Path(KB_ROOT) / old_path
    dst = Path(KB_ROOT) / new_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():  # 同名冲突：追加 -2 后缀
        stem = dst.stem
        dst = dst.with_name(f"{stem}-2.md")
    src.replace(dst)
    _yaml_edit(dst, "status", "active")
    move_entry(old_path, dst.relative_to(Path(KB_ROOT)).as_posix(), "active")
    set_human_decision(review_id, "approved")
    _append_index(f"[[概念-{dst.stem}]] → {dst.relative_to(Path(KB_ROOT)).as_posix()}")


def reject_entry(review_id: int, path: str, reason: str) -> None:
    """驳回：YAML status=draft + db 双写。"""
    p = Path(KB_ROOT) / path
    _yaml_edit(p, "status", "draft")
    update_status(path, "draft")
    set_human_decision(review_id, "rejected", reason)


def resubmit(review_id: int, path: str) -> None:
    """重新提交：YAML status=pending + db 双写。"""
    p = Path(KB_ROOT) / path
    _yaml_edit(p, "status", "pending")
    update_status(path, "pending")
    resubmit_review(review_id)


def _append_index(line: str) -> None:
    idx = Path(KB_ROOT) / "NEXUS" / "index.md"
    if not idx.exists():
        return
    text = idx.read_text(encoding="utf-8")
    if line in text:  # 幂等
        return
    # 追加到「## 概念」节末尾
    if "## 概念" in text:
        text = re.sub(r"(## 概念\n)", r"\1- " + line + "\n", text, count=1)
    else:
        text += f"\n## 概念\n- {line}\n"
    idx.write_text(text, encoding="utf-8")
```

- [ ] **Step 8: 运行 ops 测试验证通过**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/test_ops.py -v`
Expected: 3 passed。

- [ ] **Step 9: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add streamlit_app/db.py streamlit_app/ops.py tests/test_db_growth.py tests/test_ops.py && git commit -m "feat: db.py 搜索/重建函数 + ops.py 操作逻辑"
```

---

### Task 6: Streamlit app.py 骨架（侧边栏 + 路由）

**Files:**
- Create: `streamlit_app/app.py`
- Create: `streamlit_app/upload.py`、`streamlit_app/review.py`、`streamlit_app/growth.py`（本任务先建最小占位 render()，Task 7-9 填充）

**Interfaces:**
- Consumes: `ops.py`、`db.py`
- Produces: `app.py`（入口，8501 端口）；三个页面模块各导出 `render()`

- [ ] **Step 1: 创建 app.py**

```python
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
from db import rebuild_index, insert_search_log, get_conn
from ops import write_trigger

st.set_page_config(page_title="LLM Wiki 管理台", layout="wide")

# ---- 侧边栏 ----
with st.sidebar:
    st.title("LLM Wiki 管理台")
    role = st.selectbox("视角（Demo 单用户，仅提示）",
                        ["贡献者", "审核者", "消费者", "管理员"])
    query = st.text_input("搜索知识库（写入 search_logs）")
    if st.button("搜索") and query.strip():
        # grep 同款：NEXUS 下匹配行数
        import subprocess
        kb = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))
        nexus = os.path.join(kb, "NEXUS")
        res = subprocess.run(
            ["grep", "-rl", "--include=*.md", query.strip(), nexus],
            capture_output=True, text=True)
        files = [l for l in res.stdout.splitlines() if l.strip()]
        insert_search_log(query.strip(), len(files), "streamlit")
        if files:
            st.success(f"命中 {len(files)} 个文件：")
            for f in files[:10]:
                st.markdown(f"- `{os.path.relpath(f, kb).replace(os.sep, '/')}`")
        else:
            st.info(f"知识库中暂无与「{query}」直接相关的信息。（已记录为知识缺口）")
    if st.button("重建索引（从 YAML 文件）"):
        n = rebuild_index()
        st.success(f"已重建索引：{n} 条")
    st.divider()
    page = st.radio("导航", ["上传文档", "审核管理", "自增长看板"])

# ---- 路由 ----
import upload, review, growth
if page == "上传文档":
    upload.render()
elif page == "审核管理":
    review.render()
else:
    growth.render()
```

- [ ] **Step 2: 创建三个页面的最小占位**

```python
# streamlit_app/upload.py
import streamlit as st

def render():
    st.header("上传文档")
    st.caption("占位——Task 7 填充")
```

（review.py、growth.py 同构，仅标题不同。）

- [ ] **Step 3: 手动验证**

```bash
cd "d:\桌面\LLM_wiki" && docker run --rm -p 8501:8501 -v "$PWD/vault:/app/vault" -e KB_ROOT=/app/vault -e DB_PATH=/app/vault/meta.db -v "$PWD/streamlit_app:/app/streamlit_app" -w /app python:3.11-slim sh -c "pip install -q streamlit pyyaml && streamlit run streamlit_app/app.py --server.port=8501 --server.address=0.0.0.0"
```

浏览器打开 http://localhost:8501，Expected：侧边栏可见（视角/搜索框/重建索引/导航），三个页面可切换。（本步是临时容器验证，Task 10 落正式 Dockerfile。）

- [ ] **Step 4: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add streamlit_app/ && git commit -m "feat: Streamlit 入口与页面骨架"
```

---

### Task 7: upload.py 上传页（上传 + 触发 + 任务状态表）

**Files:**
- Modify: `streamlit_app/upload.py`

**Interfaces:**
- Consumes: `ops.validate_upload`、`ops.sha256_file`、`ops.write_trigger`、`db.insert_compile_task`、`db.get_conn`
- Produces: 完整上传页（设计文档 9.3 布局）

- [ ] **Step 1: 实现 upload.py**

```python
import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from db import get_conn, insert_compile_task, update_compile_task
from ops import validate_upload, sha256_file, write_trigger

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))
CATEGORIES = ["个人_notes", "会议", "经验", "项目"]


def render():
    st.header("上传文档")
    with st.form("upload_form"):
        files = st.file_uploader("选择文档（.md/.txt/.pdf/.docx，≤10MB）",
                                 type=["md", "txt", "pdf", "docx"], accept_multiple_files=True)
        category = st.selectbox("来源分类", CATEGORIES)
        submitted = st.form_submit_button("上传并加入编译队列")

    if submitted:
        if not files:
            st.warning("请先选择文件。")
            return
        ok, err = 0, 0
        for f in files:
            err_msg = validate_upload(f.name, f.size)
            if err_msg:
                st.error(f"{f.name}：{err_msg}")
                err += 1
                continue
            raw_dir = Path(KB_ROOT) / "RAW" / category
            raw_dir.mkdir(parents=True, exist_ok=True)
            dst = raw_dir / f.name
            dst.write_bytes(f.getbuffer())
            fingerprint = sha256_file(str(dst))
            insert_compile_task(f"RAW/{category}/{f.name}", fingerprint)
            ok += 1
        if ok:
            # 收集本批路径，写一个触发文件
            paths = [f"RAW/{category}/{f.name}" for f in files
                     if validate_upload(f.name, f.size) is None]
            write_trigger("compile", paths, "streamlit")
            st.success(f"{ok} 个文件已加入编译队列（{err} 个失败）。Claude Code 处理中——状态见下表。")

    st.divider()
    st.subheader("编译任务状态")
    if st.button("刷新"):
        st.rerun()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, raw_path, status, error_msg, completed_at "
            "FROM compile_tasks ORDER BY id DESC LIMIT 50").fetchall()
    if rows:
        data = [{"任务": r[0], "文件": r[1], "状态": r[2], "错误": r[3] or "", "完成时间": r[4] or ""} for r in rows]
        st.dataframe(data, use_container_width=True)
        # 重试：仅 failed 行
        failed = [r for r in rows if r[2] == "failed"]
        if failed:
            st.warning(f"{len(failed)} 个任务失败。")
            for r in failed:
                if st.button(f"重试：{r[1]}", key=f"retry_{r[0]}"):
                    write_trigger("compile", [r[1]], "streamlit")
                    update_compile_task(r[0], "pending")
                    st.success("已重新加入编译队列。")
    else:
        st.info("暂无编译任务。上传文件后等待 Claude Code 处理。")
```

- [ ] **Step 2: 手动验证**

```bash
cd "d:\桌面\LLM_wiki" && bash init.sh && docker run --rm -p 8501:8501 -v "$PWD/vault:/app/vault" -e KB_ROOT=/app/vault -e DB_PATH=/app/vault/meta.db -v "$PWD/streamlit_app:/app/streamlit_app" -w /app python:3.11-slim sh -c "pip install -q streamlit pyyaml && streamlit run streamlit_app/app.py --server.port=8501 --server.address=0.0.0.0"
```

浏览器验证：
1. 上传一个 .md 文件 → 出现"已加入编译队列"success + RAW/ 下文件存在 + `vault/_triggers/compile_*.md` 生成 + 任务表出现 pending 行
2. 上传 .jpg → 🔴 错误提示，无触发文件
3. 上传 >10MB 文件 → 🔴 错误提示

- [ ] **Step 3: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add streamlit_app/upload.py && git commit -m "feat: 上传页（上传校验+触发文件+任务状态表）"
```

---

### Task 8: review.py 审核页（AI 判定展示 + 通过/驳回/重新提交）

**Files:**
- Modify: `streamlit_app/review.py`

**Interfaces:**
- Consumes: `db.list_pending_reviews`、`db.list_rejected_reviews`、`db.get_conn`、`ops.approve_entry`、`ops.reject_entry`、`ops.resubmit`、`ops.write_trigger`
- Produces: 完整审核页（设计文档 6.4 布局 + 6.5 状态机）

- [ ] **Step 1: 实现 review.py**

```python
import json
import os
from pathlib import Path

import streamlit as st

from db import get_conn, list_pending_reviews, list_rejected_reviews
from ops import approve_entry, reject_entry, resubmit, write_trigger

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


def render():
    st.header("审核管理")
    pending = list_pending_reviews()
    st.subheader(f"待审核（{len(pending)} 条）")
    if not pending:
        st.info("暂无待审核条目。")
    for rec in pending:
        with st.expander(f"{rec.get('title') or Path(rec['nexus_path']).stem} — AI判定：{rec['ai_verdict'] or '未完成'}"):
            if rec["ai_scores"]:
                scores = json.loads(rec["ai_scores"])
                cols = st.columns(5)
                s = scores.get("scores", {})
                cols[0].metric("完整性", s.get("completeness", "-"))
                cols[1].metric("去重", s.get("dedup", "-"))
                cols[2].metric("质量", f"{s.get('quality', '-')}/5")
                cols[3].metric("敏感信息", s.get("sensitive", "-"))
                cols[4].metric("合规", s.get("compliance", "-"))
                st.caption(f"职务归属：{scores.get('department', '-')} | 摘要：{scores.get('summary', '')}")
                if scores.get("concerns"):
                    st.warning("关注项：" + "；".join(scores["concerns"]))
            else:
                st.warning("AI 审核未完成或失败。可人工审核，或 [重试 AI 审核]。")
            full = Path(KB_ROOT) / rec["nexus_path"]
            if full.exists():
                with st.container(border=True):
                    st.markdown(full.read_text(encoding="utf-8"))
            c1, c2, c3 = st.columns(3)
            if c1.button("✓ 通过", key=f"ok_{rec['id']}"):
                new_path = "NEXUS/概念/" + Path(rec["nexus_path"]).name
                approve_entry(rec["id"], rec["nexus_path"], new_path)
                st.success(f"已通过：{new_path}")
                st.rerun()
            if c2.button("✗ 驳回", key=f"no_{rec['id']}"):
                reason = st.text_input("驳回原因（必填）", key=f"reason_{rec['id']}")
                if st.button("确认驳回", key=f"confirm_{rec['id']}"):
                    if not reason.strip():
                        st.error("驳回原因不能为空。")
                    else:
                        reject_entry(rec["id"], rec["nexus_path"], reason)
                        st.success("已驳回。")
                        st.rerun()
            if c3.button("重试 AI 审核", key=f"ai_{rec['id']}"):
                write_trigger("review", [rec["nexus_path"]], "streamlit")
                st.success("已加入 AI 审核队列。")

    rejected = list_rejected_reviews()
    if rejected:
        st.divider()
        st.subheader("已驳回（可重新提交）")
        for rec in rejected:
            with st.expander(f"{rec.get('title') or Path(rec['nexus_path']).stem} — 驳回原因：{rec['reject_reason']}"):
                if st.button("重新提交审核", key=f"rs_{rec['id']}"):
                    resubmit(rec["id"], rec["nexus_path"])
                    write_trigger("review", [rec["nexus_path"]], "streamlit")
                    st.success("已重新提交 AI 审核。")
                    st.rerun()
```

- [ ] **Step 2: 手动验证**

用 Task 5 的 approve/reject 单测已覆盖核心逻辑；UI 层验证：在 meta.db 手工插入一条 pending_reviews（含 ai_scores JSON）+ 对应 pending_review 文件后，浏览器确认列表/展开/评分面板/通过/驳回/重试按钮可用。

```bash
cd "d:\桌面\LLM_wiki" && sqlite3 vault/meta.db "INSERT INTO pending_reviews (nexus_path, submitter, department, ai_verdict, ai_scores, created_at) VALUES ('pending_review/应急哨兵.md','demo_user','产品','approved','{\"verdict\":\"approved\",\"scores\":{\"completeness\":\"pass\",\"dedup\":\"pass\",\"quality\":4,\"sensitive\":\"pass\",\"compliance\":\"pass\"},\"department\":\"产品\",\"concerns\":[],\"summary\":\"质量合格\"}','2026-08-13');" && bash -c 'echo "---" > vault/pending_review/应急哨兵.md && echo "type: concept" >> vault/pending_review/应急哨兵.md && echo "title: 应急哨兵" >> vault/pending_review/应急哨兵.md && echo "status: pending" >> vault/pending_review/应急哨兵.md && echo "---" >> vault/pending_review/应急哨兵.md && echo "" >> vault/pending_review/应急哨兵.md && echo "# 应急哨兵" >> vault/pending_review/应急哨兵.md && echo "华泰智远的多灾种监测预警产品。" >> vault/pending_review/应急哨兵.md'
```

Expected：审核页出现该条目，AI 评分面板 5 项齐全，[通过] 后文件移入 NEXUS/概念/ 且 YAML status=active。

- [ ] **Step 3: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add streamlit_app/review.py && git commit -m "feat: 审核页（AI 判定展示+通过/驳回/重试）"
```

---

### Task 9: growth.py 自增长看板

**Files:**
- Modify: `streamlit_app/growth.py`

**Interfaces:**
- Consumes: `db.top_missed_queries`、`db.search_stats`、`get_conn`
- Produces: 看板页（设计文档 8.3 三卡片布局）

- [ ] **Step 1: 实现 growth.py**

```python
import glob
import os
from pathlib import Path

import streamlit as st

from db import get_conn, top_missed_queries, search_stats

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


def render():
    st.header("自增长看板")
    stats = search_stats()
    c1, c2 = st.columns(2)
    c1.metric("总搜索次数", stats["total"])
    c2.metric("未命中率", f"{stats['miss_rate'] * 100:.0f}%")
    st.divider()

    st.subheader("搜索未命中 Top 20（知识缺口）")
    top = top_missed_queries(20)
    if top:
        st.dataframe([{"缺口查询": t["query"], "搜索次数": t["cnt"], "最近出现": t["last_seen"]} for t in top],
                     use_container_width=True)
    else:
        st.info("暂无知识缺口记录——用户搜索都有结果。")

    st.divider()
    st.subheader("最近自增长周报")
    reports = sorted(glob.glob(str(Path(KB_ROOT) / "NEXUS" / "研究" / "自增长周报_*.md")), reverse=True)
    if reports:
        st.markdown(Path(reports[0]).read_text(encoding="utf-8"))
    else:
        st.info("尚无周报。Claude Code 执行 /process-growth 后生成（见 workflows/growth_workflow.md）。")
```

- [ ] **Step 2: 手动验证**

向 search_logs 插入两条 match_count=0 记录，浏览器确认看板显示缺口 Top 20 与统计卡片。

```bash
cd "d:\桌面\LLM_wiki" && sqlite3 vault/meta.db "INSERT INTO search_logs (query, match_count, source, timestamp) VALUES ('区块链',0,'streamlit',datetime('now','localtime')),('区块链',0,'claude_code',datetime('now','localtime')),('应急哨兵',3,'streamlit',datetime('now','localtime'));"
```

Expected：缺口列表首行"区块链 × 2"；统计卡片 总搜索 3、未命中率 67%。

- [ ] **Step 3: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add streamlit_app/growth.py && git commit -m "feat: 自增长看板（缺口 Top 20+统计+周报）"
```

---

### Task 10: Docker 化（Dockerfile + requirements + docker-compose）

**Files:**
- Create: `Dockerfile`、`requirements.txt`、`docker-compose.yml`、`.env.example`

**Interfaces:**
- Consumes: 无（部署层）
- Produces: 单容器 Streamlit 部署（设计文档 3.3）

- [ ] **Step 1: 创建四个文件**

`requirements.txt`：

```
streamlit>=1.36,<2
pyyaml>=6.0
pandas>=2.0
```

`Dockerfile`：

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY streamlit_app/ ./streamlit_app/
EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

`docker-compose.yml`：

```yaml
version: '3.8'
services:
  streamlit:
    build: .
    ports:
      - "8501:8501"
    volumes:
      - ./vault:/app/vault        # Obsidian Vault（含 meta.db 与 _triggers/）
    environment:
      - KB_ROOT=/app/vault
      - DB_PATH=/app/vault/meta.db
    restart: unless-stopped
```

`.env.example`：

```
# 环境变量说明（docker-compose 已内联，此文件仅文档用途）
# KB_ROOT=/app/vault
# DB_PATH=/app/vault/meta.db
# 无 LLM Key——LLM 能力全部经 Claude Code 提供
```

- [ ] **Step 2: 构建并验证**

```bash
cd "d:\桌面\LLM_wiki" && docker compose up -d --build && sleep 8 && curl -s -o /dev/null -w "%{http_code}" http://localhost:8501
```

Expected: `200`。浏览器打开确认页面正常。

- [ ] **Step 3: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add Dockerfile requirements.txt docker-compose.yml .env.example && git commit -m "feat: Docker 单容器部署"
```

---

### Task 11: Claude Code 配置（hooks + 命令）

**Files:**
- Create: `.claude/settings.json`
- Create: `.claude/hooks/session-start.sh`
- Create: `.claude/commands/process-triggers.md`
- Create: `.claude/commands/ask.md`

**Interfaces:**
- Consumes: Task 1 的 `vault/_triggers/` 约定
- Produces: SessionStart hook（自动提示）+ 2 个手动命令（处理触发队列、检索问答）

- [ ] **Step 1: 创建 .claude/settings.json 与 hook**

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash .claude/hooks/session-start.sh"
          }
        ]
      }
    ]
  }
}
```

`hooks/session-start.sh`：

```bash
#!/usr/bin/env bash
TRIGGERS="$(find vault/_triggers -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
if [ "$TRIGGERS" -gt 0 ]; then
  echo "【知识库触发队列】vault/_triggers/ 下有 $TRIGGERS 个未处理触发文件。请优先执行 /process-triggers 处理队列，处理完成后将触发文件移入 vault/_triggers/done/。"
fi
```

- [ ] **Step 2: 创建 process-triggers 命令**

```markdown
# /process-triggers —— 处理知识库触发队列

扫描 vault/_triggers/*.md（排除 done/），按文件时间戳升序处理：

1. 对每个 compile_*.md：按 workflows/compile_workflow.md 执行批量编译
2. 对每个 review_*.md：按 workflows/review_workflow.md 执行六维度审核
3. 每个触发文件处理成功（或所有条目已尝试且记录失败原因）后，移入 vault/_triggers/done/
4. 处理报告：编译 N 个、审核 M 个、失败 K 个（附失败文件与原因）
```

- [ ] **Step 3: 创建 ask 命令**

```markdown
# /ask <问题> —— 检索知识库并生成带引用答案

1. 在 vault/NEXUS/ 下 grep 检索问题关键词，取匹配行数 Top-5 的 .md 文件
2. cat 读取这 5 个文件全文（含 YAML Frontmatter）
3. 按 prompts/answer_prompt.md 执行答案生成
4. 将 (query, match_count) 写入 vault/meta.db 的 search_logs 表（source='claude_code'）
5. 向用户呈现答案（引用来源为可点击的 Vault 相对路径）
```

- [ ] **Step 4: 验证 hook 生效**

在项目根目录启动一个临时 Claude Code 会话（`cd "d:\桌面\LLM_wiki" && claude`），Expected：若 `vault/_triggers/` 有触发文件，会话开始即出现「知识库触发队列」提示。（若当前会话 hook 未注册，重启 Claude Code 生效。）

- [ ] **Step 5: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add .claude/ && git commit -m "feat: Claude Code SessionStart hook 与 /process-triggers、/ask 命令"
```

---

### Task 12: 三个 Workflow 编排文档

**Files:**
- Create: `workflows/compile_workflow.md`
- Create: `workflows/review_workflow.md`
- Create: `workflows/growth_workflow.md`

**Interfaces:**
- Consumes: `prompts/*.md`（3 个，已定稿）、Task 11 的触发目录约定、Task 3-5 的 db.py 函数名（workflow 中 sqlite3 命令的字段名一致）
- Produces: Claude Code 执行编译/审核/自增长分析时遵循的编排指令（设计文档 5.2/6.2/8.2）

- [ ] **Step 1: 创建 compile_workflow.md**

```markdown
# 批量编译 Workflow

**触发**：/process-triggers 消费 compile_*.md 时执行。

## 输入
- 待编译 RAW 路径列表（来自触发文件）

## 步骤
1. 对每个 RAW 路径：
   a. 计算 SHA256：`sha256sum "vault/<raw_path>"`
   b. 查缓存：`sqlite3 vault/meta.db "SELECT id FROM compile_tasks WHERE raw_path='<path>' AND fingerprint='<hash>' AND status='done' ORDER BY id DESC LIMIT 1"`
      - 命中 → 更新该任务记录为 cached（`INSERT` 新行 status='cached' 亦可），跳过 LLM
   c. 未命中：
      - 非 .md/.txt：用 Python 提取文本（pypdf / python-docx，本机无 Python 时报错并标记 failed）
      - 读全文 → 执行 prompts/compile_prompt.md → 解析 JSON（失败自动重试 1 次）
      - 按设计文档 5.3/5.4 落盘：
        - 资源摘要 → `NEXUS/资源/<标题>.md`（YAML: type=resource, status=active, fingerprint）
        - 概念页 → `pending_review/<概念名>.md`（YAML: type=concept, status=pending, source）
      - 更新 index.md（资源节，幂等追加）
      - `sqlite3` upsert knowledge_entries（资源 active + 概念 pending）
      - 更新 compile_tasks 状态 done/failed（error_msg 记录失败原因）
2. 全部完成后写 `_triggers/review_<ts>.md`（本批所有概念页路径），供审核阶段消费
3. 返回：编译 N 个、缓存 M 个、失败 K 个

## 输出验收
- 每个成功文件：NEXUS/资源/ 有 1 个资源摘要；概念页在 pending_review/，YAML 四必填字段齐全
- 触发文件处理完毕移入 vault/_triggers/done/
```

- [ ] **Step 2: 创建 review_workflow.md**

```markdown
# 六维度审核 Workflow

**触发**：/process-triggers 消费 review_*.md 时执行。

## 输入
- 待审条目路径列表（来自触发文件）

## 步骤
1. 对每个条目：
   a. `cat` 读取 Markdown 全文（含 YAML）
   b. 构造去重候选：`grep -l "<标题关键词>" vault/NEXUS vault/pending_review --include=*.md`（排除自身），每个候选取路径+标题+前200字，最多 5 个
   c. 并行执行（Harness parallel）：
      - 确定性检查（bash/python，不调 LLM）：
        - 维度1 完整性：四字段（type/title/status/source）非空 + 正文≥100中文字符
        - 维度5 敏感信息：正则（18位身份证→blocked；11位手机号→warning；sk-开头→blocked；password:→blocked；"机密/绝密"→blocked）
      - LLM 子 Agent（每维度一个，按 prompts/review_prompt.md 对应节）：
        - 维度2 去重（对候选列表）· 维度3 职务归属 · 维度4 质量（1-5分）· 维度6 合规
   d. 按 review_prompt.md 判定逻辑链汇总 verdict（sensitive=blocked 一票否决等）
   e. `sqlite3` INSERT INTO pending_reviews (nexus_path, 'demo_user', department, ai_verdict, ai_scores=完整JSON, datetime('now','localtime'))
2. 触发文件处理完毕移入 vault/_triggers/done/
3. 返回：审核 N 条、判定分布

## 输出验收
- pending_reviews 每条目一行，ai_scores 为完整 JSON（verdict/department/scores/duplicates/concerns/summary）
- 审核页可展示该判定
```

- [ ] **Step 3: 创建 growth_workflow.md**

```markdown
# 周度自增长分析 Workflow

**触发**：用户手动执行 /process-growth（或并入 /process-triggers 时作为尾段）。

## 步骤
1. 导出近 7 天未命中搜索：
   `sqlite3 vault/meta.db "SELECT query, COUNT(*) cnt FROM search_logs WHERE timestamp >= date('now','-7 days') AND match_count=0 GROUP BY query ORDER BY cnt DESC LIMIT 100"`
2. LLM 聚类语义相同的查询（如「应急哨兵价格」≈「哨兵报价」）
3. 对每个聚类给出建议补充文档方向（产品资料/技术方案/…）
4. 生成 `NEXUS/研究/自增长周报_<YYYY-MM-DD>.md`：
   - 本周知识缺口 Top 20（排名/缺口主题/搜索次数/建议补充文档）
   - 上周已补缺口（对比上周周报与本周新入库条目）
5. 更新 knowledge_entries（周报本身 type=research, status=active）
6. 返回：周报路径与缺口数

## 输出验收
- NEXUS/研究/ 出现当日周报；看板「最近自增长周报」卡片可渲染
```

- [ ] **Step 4: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add workflows/ && git commit -m "docs: 三个 Agent 编排 Workflow"
```

---

### Task 13: TDD 审核确定性规则测试集

**Files:**
- Create: `tests/test_review_rules.py`（22 用例，设计文档 10.2）
- Create: `tests/conftest.py`（路径/环境注入，供各测试复用）

**Interfaces:**
- Consumes: 设计文档 10.2 用例清单
- Produces: pytest 全绿（容器外运行，Python + vault 副本）

- [ ] **Step 1: 创建 conftest.py**

```python
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
```

（文件开头需 `import pytest`。）

- [ ] **Step 2: 创建 test_review_rules.py**

> 审核确定性规则（完整性/去重指纹/敏感信息正则）是纯逻辑。设计文档 10.2 共 22 例。**实现位置**：完整性/敏感信息正则现放 `streamlit_app/rules.py`（供 review 页与 workflow 引用），本测试直接测该模块。

先创建 `streamlit_app/rules.py`（Step 3 实现），本步写测试：

```python
import re
from pathlib import Path

from rules import check_completeness, check_sensitive

# ---- 完整性（4 例）----
def test_completeness_all_fields_and_100_chars():
    fm = "type: concept\ntitle: 应急哨兵\nstatus: pending\nsource: RAW/a.md\n"
    body = "应急哨兵" * 40  # 120 字
    assert check_completeness(fm, body) == "pass"

def test_completeness_missing_type():
    fm = "title: x\nstatus: pending\nsource: RAW/a.md\n"
    assert check_completeness(fm, "正文" * 50) == "incomplete"

def test_completeness_short_body():
    fm = "type: concept\ntitle: x\nstatus: pending\nsource: RAW/a.md\n"
    assert check_completeness(fm, "正文") == "insufficient"

def test_completeness_missing_three_fields():
    fm = "title: x\n"
    assert check_completeness(fm, "正文" * 50) == "insufficient"

# ---- 敏感信息（6 例）----
def test_sensitive_id_card():
    assert check_sensitive("身份证 110101199001011234") == "blocked"

def test_sensitive_phone():
    assert check_sensitive("电话 13812345678") == "warning"

def test_sensitive_api_key():
    assert check_sensitive("key=sk-proj-abc123") == "blocked"

def test_sensitive_password():
    assert check_sensitive("password: hunter2") == "blocked"

def test_sensitive_internal_mark():
    assert check_sensitive("本文件为机密") == "blocked"

def test_sensitive_clean():
    assert check_sensitive("应急哨兵部署手册内容") == "pass"
```

- [ ] **Step 3: 实现 rules.py**

```python
"""审核确定性规则（不依赖 LLM）。维度1 完整性 + 维度5 敏感信息。"""
import re

REQUIRED_FIELDS = ["type", "title", "status", "source"]
MIN_BODY_CHARS = 100


def check_completeness(frontmatter_text: str, body_text: str) -> str:
    """返回 pass/incomplete/insufficient。"""
    missing = [f for f in REQUIRED_FIELDS if not re.search(rf"^{f}: .+$", frontmatter_text, re.MULTILINE)]
    if len(missing) >= 3 or len(body_text) < MIN_BODY_CHARS:
        return "insufficient"
    if missing:
        return "incomplete"
    return "pass"


_SENSITIVE_RULES = [
    (re.compile(r"\b\d{17}[\dXx]\b"), "blocked", "身份证号"),
    (re.compile(r"\b1\d{10}\b"), "warning", "手机号"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+|secret\s*[:=]\s*\S+)", re.IGNORECASE), "blocked", "密钥"),
    (re.compile(r"(?:password|密码)\s*[:=]\s*\S+", re.IGNORECASE), "blocked", "明文密码"),
    (re.compile(r"(机密|绝密|confidential)", re.IGNORECASE), "blocked", "内部标记"),
]


def check_sensitive(text: str) -> str:
    """返回 pass/warning/blocked（blocked 优先级最高）。"""
    has_warning = False
    for pattern, level, _name in _SENSITIVE_RULES:
        if pattern.search(text):
            if level == "blocked":
                return "blocked"
            has_warning = True
    return "warning" if has_warning else "pass"
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd "d:\桌面\LLM_wiki" && python -m pytest tests/ -v`
Expected: 10 passed（test_review_rules 10 例；test_schema 2、test_db_basic 3、test_db_review 3、test_db_growth 2、test_ops 3 均已由前序任务建立，累计 23 例）。

> 设计文档 10.2 的 22 例拆分：完整性 4 + 敏感 6 为本文件 10 例；去重指纹 3、状态机 4、数据层 3、文件名清洗 2 由前序任务测试覆盖（Task 2-5）。验收时核对总数 ≥22。

- [ ] **Step 5: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add tests/ streamlit_app/rules.py && git commit -m "test: 审核确定性规则测试集 + rules.py"
```

---

### Task 14: 种子数据与 prompt 一致性检查

**Files:**
- Create: `tests/sample_docs/样例_应急哨兵产品白皮书.md`
- Create: `tests/sample_docs/样例_内容不足.txt`
- Create: `tests/sample_docs/样例_含敏感信息.md`

**Interfaces:**
- Consumes: 无
- Produces: 3 份测试样例（设计文档 9.2 要求 5-10 份真实文档由用户提供，测试样例 3 份覆盖正常/内容不足/敏感三种路径）

- [ ] **Step 1: 创建三份样例文档**

`tests/sample_docs/样例_应急哨兵产品白皮书.md`（500+ 字，含 2 个可提取概念）：

```markdown
# 应急哨兵产品白皮书

## 产品概述
应急哨兵是华泰智远自主研发的多灾种监测预警产品，面向应急管理部门提供暴雨、台风、地质灾害等灾害的实时监测与预警服务。产品采用"感知-分析-决策-行动"四层架构，覆盖监测数据接入、智能分析研判、预警信息发布、应急响应闭环全流程。

## 核心技术
1. 多灾种监测预警引擎：统一接入气象、水文、地质等多源监测数据，通过阈值判识与多指标耦合分析，实现灾害风险分级预警。
2. 叫应体系联动：预警信息生成后自动匹配责任人与响应预案，通过短信、电话、政务平台多渠道叫应，确保"叫醒、叫应、叫动"。
3. 数字孪生底座：基于三维实景建模，将监测点位、风险区划、处置力量叠加呈现，支撑应急指挥可视化。

## 部署模式
支持政务云、私有化两种部署模式。政务云部署由运营方统一维护，私有化部署适用于数据敏感场景，交付周期约 30 天。

## 客户价值
- 预警时效从分钟级提升到秒级
- 风险区划覆盖率提升至 95%
- 多部门联动响应时间缩短 40%

## 版本说明
当前版本 V2.1，2026 年 7 月发布，新增台风路径推演模块。
```

`tests/sample_docs/样例_内容不足.txt`：

```text
会议提醒：明天下午 3 点在 302 会议室开项目周会。
```

`tests/sample_docs/样例_含敏感信息.md`：

```markdown
# 测试含敏感信息文档
测试员工手机号 13812345678 与身份证 110101199001011234，另有 API Key：sk-proj-test123456789，以及标注为机密的段落。
```

- [ ] **Step 2: prompt 一致性检查**

逐条核对 `prompts/` 3 个文件与设计文档/PRD 的一致性：
- compile_prompt.md：JSON 结构（resource/concepts）、部门表、标签词汇 → 与设计文档 4.2/5.3 一致 ✓（已定稿，不修改）
- review_prompt.md：六维度判定、JSON Schema → 与 Task 13 的 rules.py 判定逻辑一致（**检查点**：review_prompt.md 中敏感信息"大额金额 >100万元"规则——rules.py 未实现该条，需在 review_prompt 的 LLM 维度中覆盖，两处不冲突）
- answer_prompt.md：六策略 → 与 /ask 命令（Task 11）行为一致

检查结论记入 commit message；若发现不一致需修改的是**Workflow/prompt 描述**而非 rules.py（rules.py 是确定性规则基准）。

- [ ] **Step 3: 提交**

```bash
cd "d:\桌面\LLM_wiki" && git add tests/sample_docs/ && git commit -m "docs: 测试样例文档 + prompt 一致性检查"
```

---

### Task 15: 端到端验收（演示脚本 A/B + 边界）

**Files:**
- Modify: 无（验收任务，可能修复前述任务缺陷）
- 执行: 设计文档 10.3 验收标准

**Interfaces:**
- Consumes: 全部前序任务

- [ ] **Step 1: 全量测试回归**

```bash
cd "d:\桌面\LLM_wiki" && bash init.sh && python -m pytest tests/ -v
```

Expected: 全部 passed（≥22 例）。

- [ ] **Step 2: 预置端到端演示数据**

```bash
cd "d:\桌面\LLM_wiki"
# 种子 search_logs（看板演示）
sqlite3 vault/meta.db "INSERT INTO search_logs (query, match_count, source, timestamp) VALUES ('区块链技术方案',0,'streamlit',datetime('now','localtime')),('应急哨兵报价',0,'streamlit',datetime('now','localtime')),('应急预案模板',0,'claude_code',datetime('now','localtime'));"
# 上传样例文档触发编译队列
cp tests/sample_docs/样例_应急哨兵产品白皮书.md vault/RAW/产品资料/ 2>/dev/null || mkdir -p vault/RAW/产品资料 && cp tests/sample_docs/样例_应急哨兵产品白皮书.md vault/RAW/产品资料/
```

- [ ] **Step 3: 演示脚本 A 走查（对照设计文档 10.3 检查点）**

1. 启动：`bash init.sh && docker compose up -d`；Obsidian 打开 `vault/`
2. Streamlit 上传页：确认样例文档在 RAW/、任务表有 pending 行、`_triggers/compile_*.md` 已生成
3. Claude Code 终端：执行 `/process-triggers` → 确认编译完成、概念页入 pending_review/、触发文件入 done/
4. 审核页：AI 判定出现 → [通过] → 文件移入 NEXUS/概念/、YAML active、index.md 追加
5. Obsidian：确认 NEXUS/概念/ 新页、图谱可见 wikilink
6. 侧边栏搜索"应急哨兵" → 命中；搜索"区块链" → 记录缺口
7. 看板：缺口 Top 20 显示"区块链技术方案"等

每步失败即修复，修复后重跑本步。

- [ ] **Step 4: 演示脚本 B 走查**

1. 上传 `样例_内容不足.txt` → 编译 → 概念页（按 compile_prompt 特殊规则，无概念但资源摘要正常）
2. 审核页驳回一条（填原因）→ 状态 draft、留在 pending_review/
3. [重新提交审核] → 再次进入待审列表

- [ ] **Step 5: 边界验收**

1. 上传 `.jpg` → 🔴 拒绝
2. 上传 >10MB → 🔴 拒绝
3. 搜索无结果 → ℹ️ 建议 + 记录缺口
4. 断开网络（模拟 LLM 不可用）→ 编译任务 failed 状态 + ⚠️ 可恢复错误提示

- [ ] **Step 6: 提交修复**

```bash
cd "d:\桌面\LLM_wiki" && git add -A && git commit -m "fix: 端到端验收修复"
```

（若无修复，跳过提交。）

- [ ] **Step 7: 交付总结**

向用户输出验收报告：演示脚本 A/B 走查结果、边界验收结果、已知限制（设计文档 11.2 的 4 项）、后续建议（真实种子数据由用户提供后重新走查）。

---

## Self-Review 记录

**1. Spec coverage（设计文档 → 任务映射）**：

| 设计文档章节 | 任务 |
|-------------|------|
| 3.1 目录树 / 3.2 init.sh | Task 1 |
| 3.2 DDL / 4.3 SQLite | Task 2 |
| 9.2 db.py 14 函数 | Task 3-5 |
| 9.3/6.4/9.5 三页面 | Task 6-9 |
| 3.3 Docker | Task 10 |
| 3.5 hooks/命令 | Task 11 |
| 5.2/6.2/8.2 workflows | Task 12 |
| 10.1 SDD 8 规约 / 10.2 TDD 22 例 | Task 2-5, 13（S1-S8 由端到端 Task 15 走查） |
| 9.2 种子数据 | Task 14 |
| 10.3 验收 | Task 15 |

**2. Placeholder 扫描**：无 TBD/TODO；每步含实际代码或精确命令。

**3. 类型一致性**：`db.py` 函数签名在 Task 3/4/5 定义、Task 6-9 引用、Task 12 workflow 中 sqlite3 字段名——已核对一致（path 主键、pending_reviews.nexus_path、compile_tasks.status 枚举含 cached）。

**4. 已知偏差（实现级细化，非 spec 矛盾）**：
- DDL 从 init.sh 内联提取为 `schema.sql`（可测性）
- 新增 `streamlit_app/ops.py` 与 `streamlit_app/rules.py`（纯逻辑可单测，设计文档 9.2 未列，符合"UI 只做展示"原则）
- Task 13 将 22 例拆为 rules.py 10 例 + 前序任务 12 例，总数 22 不变
- 测试依赖本机 Python 3.11 + pytest + sqlite3 CLI；本机无 Python 时 Task 2-5/13 延后至 Docker 环境执行

**5. 设计文档 10.2 缺口（转 Phase 2，不在 Demo 实现）**：去重指纹 3 例 + 文件名清洗 2 例，共 5 例因架构调整转移——去重（dedup）归属 LLM 审核维度（维度2，由审核 Agent 语义判定，非确定性指纹规则）；文件名清洗为 Agent 侧动作（上传时由 Claude Code/上传侧净化，非 Streamlit 校验规则）。均不在 Demo 实现范围内。
