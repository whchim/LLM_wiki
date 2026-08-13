-- vault/meta.db 的建表脚本（由 init.sh 执行；Task 2/5 测试直接引用）
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
