-- PostgreSQL 建表脚本（由 ensure_schema() 在应用启动时执行；幂等）
-- Phase 2 SP1：SQLite → PostgreSQL 16 + pgvector
CREATE EXTENSION IF NOT EXISTS vector;         -- SP4 向量检索用；幂等

CREATE TABLE IF NOT EXISTS knowledge_entries (
    path        TEXT PRIMARY KEY,          -- Vault 相对路径，如 NEXUS/概念/示例监测产品.md
    type        TEXT NOT NULL,             -- concept/resource/research/glossary
    title       TEXT NOT NULL,
    department  TEXT,                      -- 9 部门 + 共享层
    status      TEXT NOT NULL DEFAULT 'pending',  -- draft/pending/active/stale/deprecated
    version     TEXT NOT NULL DEFAULT 'V1.0',
    fingerprint TEXT,                      -- 源文件 SHA256
    updated_at  TEXT                       -- YYYY-MM-DD
);
CREATE INDEX IF NOT EXISTS idx_entries_status ON knowledge_entries(status);
CREATE INDEX IF NOT EXISTS idx_entries_type   ON knowledge_entries(type);

CREATE TABLE IF NOT EXISTS compile_tasks (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raw_path     TEXT NOT NULL,
    nexus_path   TEXT,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/done/failed/cached
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
    ai_verdict     TEXT,                    -- approved/rejected/needs_human_review
    ai_scores      JSONB,                   -- 六维度 JSON（SP1 由 TEXT 升级 JSONB）
    human_decision TEXT,                    -- approved/rejected，NULL=未处理
    reject_reason  TEXT,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_nexus ON pending_reviews(nexus_path);
CREATE INDEX IF NOT EXISTS idx_reviews_human ON pending_reviews(human_decision);

CREATE TABLE IF NOT EXISTS search_logs (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query       TEXT NOT NULL,
    match_count INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'streamlit',  -- streamlit/claude_code
    timestamp   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_source ON search_logs(source);

-- Phase 2 新增（SP1 建表；行为在对应 SP 实现）
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operator    TEXT,
    action      TEXT NOT NULL,             -- upload/review/approve/reject/rebuild/...
    target_path TEXT,
    detail      JSONB,
    timestamp   TEXT NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS contributors (
    entry_path        TEXT NOT NULL REFERENCES knowledge_entries(path),
    user_id           TEXT NOT NULL,
    contribution_type TEXT NOT NULL,       -- submit/review/approve
    PRIMARY KEY (entry_path, user_id, contribution_type)
);
CREATE TABLE IF NOT EXISTS conflicts (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_a_path  TEXT NOT NULL,
    entry_b_path  TEXT NOT NULL,
    conflict_type TEXT NOT NULL,           -- factual_contradiction/duplicate/stale
    status        TEXT DEFAULT 'open',
    created_at    TEXT
);

-- Phase 2 SP2 新增：认证用户表
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,           -- argon2 hash
    role          TEXT NOT NULL DEFAULT 'user',   -- admin / reviewer / user
    display_name  TEXT,
    created_at    TEXT NOT NULL DEFAULT now()
);
