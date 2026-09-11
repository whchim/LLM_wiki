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

-- Phase 2 SP2.5 新增：可观测性 Trace 事件表
CREATE TABLE IF NOT EXISTS trace_events (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    span_type   TEXT NOT NULL,          -- compile_session / search / review_approve / review_reject /
                                        -- review_resubmit / review_retry_ai / rebuild_index / login
    trace_id    TEXT,                   -- 一次编译会话的 UUID（过程 trace 分组键；单操作可空）
    operation   TEXT,                   -- 细分动作
    status      TEXT NOT NULL,          -- ok / error（业务失败也记 error，便于看失败率）
    latency_ms  INTEGER,                -- 会话/操作耗时（毫秒）
    detail      JSONB,                  -- 附加：compiled/cached/failed、search hit、错误 message、目标路径
    token_usage JSONB,                  -- Langfuse 探针回填（input/output/成本）；过程 trace 可空
    operator    TEXT,                   -- 触发者（compile_trace 记 system 或触发用户）
    created_at  TEXT NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trace_span_created ON trace_events (span_type, created_at);
CREATE INDEX IF NOT EXISTS idx_trace_traceid ON trace_events (trace_id);

-- Phase 2 SP4 新增：向量检索（向量 = 可重建缓存；模型换版/损坏 → backfill 全量重算）
ALTER TABLE knowledge_entries ADD COLUMN IF NOT EXISTS embedding vector(1024);
CREATE INDEX IF NOT EXISTS idx_entries_embedding
    ON knowledge_entries USING hnsw (embedding vector_cosine_ops);
-- frontmatter 的 description（推荐字段）纳入缓存（SP4 embedding 输入用）
ALTER TABLE knowledge_entries ADD COLUMN IF NOT EXISTS description TEXT;

-- Phase 2 SP5 新增：健康巡检报告
CREATE TABLE IF NOT EXISTS health_reports (
    id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_date       TEXT NOT NULL,
    orphan_count      INTEGER NOT NULL DEFAULT 0,   -- 孤立节点数
    broken_link_count INTEGER NOT NULL DEFAULT 0,   -- wikilink 断链数
    stale_count       INTEGER NOT NULL DEFAULT 0,   -- 过期（>180 天）数
    conflict_count    INTEGER NOT NULL DEFAULT 0,   -- 相似候选对数
    total_entries     INTEGER NOT NULL DEFAULT 0,
    growth_rate       REAL,                          -- 相比上次巡检增长率
    detail            JSONB                          -- 明细清单（路径/配对）
);

-- Sales customer state Agent（阶段 2）：客户状态生命周期
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    display_name_redacted TEXT,
    owner_user_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL CHECK (source_type IN ('meeting_note','transcript','chat_summary')),
    occurred_at TIMESTAMPTZ NOT NULL,
    submitted_by TEXT NOT NULL,
    source_ref TEXT,
    processing_status TEXT NOT NULL DEFAULT 'received'
        CHECK (processing_status IN ('received','processed','needs_review','rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_customer ON conversations(customer_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    content_redacted TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evidence_conversation ON evidence(conversation_id);

-- Sales clarification Agent（阶段 4）：有限轮次的澄清会话，原始证据不可变
CREATE TABLE IF NOT EXISTS clarification_sessions (
    session_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(conversation_id),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','ready_for_proposal','needs_human_review','completed','cancelled')),
    round_count INTEGER NOT NULL DEFAULT 0 CHECK (round_count >= 0),
    max_rounds INTEGER NOT NULL DEFAULT 2 CHECK (max_rounds BETWEEN 1 AND 2),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_clarification_sessions_status ON clarification_sessions(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS clarification_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES clarification_sessions(session_id),
    turn_no INTEGER NOT NULL CHECK (turn_no >= 1),
    status TEXT NOT NULL CHECK (status IN ('needs_clarification','ready_for_proposal','insufficient_evidence','human_review')),
    agent_output JSONB NOT NULL,
    question_count INTEGER NOT NULL DEFAULT 0 CHECK (question_count BETWEEN 0 AND 2),
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, turn_no)
);
CREATE INDEX IF NOT EXISTS idx_clarification_turns_session ON clarification_turns(session_id, turn_no);

CREATE TABLE IF NOT EXISTS clarification_answers (
    answer_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES clarification_sessions(session_id),
    turn_id TEXT NOT NULL REFERENCES clarification_turns(turn_id),
    question_id TEXT NOT NULL,
    answer_text_redacted TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (turn_id, question_id)
);
CREATE INDEX IF NOT EXISTS idx_clarification_answers_session ON clarification_answers(session_id, created_at);

CREATE TABLE IF NOT EXISTS state_proposals (
    proposal_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    current_state TEXT,
    proposed_state TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'propose'
        CHECK (decision IN ('propose','needs_review','reject')),
    confidence NUMERIC(5,4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    reasoning_summary TEXT,
    next_action TEXT,
    valid_until TIMESTAMPTZ,
    needs_human_confirmation BOOLEAN NOT NULL DEFAULT true,
    risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_version TEXT,
    prompt_version TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','superseded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON state_proposals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS state_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES state_proposals(proposal_id),
    decision TEXT NOT NULL CHECK (decision IN ('approved','modified','rejected')),
    final_state TEXT,
    decided_by TEXT NOT NULL,
    reason TEXT,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS state_events (
    event_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    decision_id TEXT REFERENCES state_decisions(decision_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('state_confirmed','state_expired','state_withdrawn','state_corrected')),
    state TEXT NOT NULL CHECK (state IN ('new_lead','contacted','need_confirmed','solution_eval','commercial_negotiation','won','lost_or_paused','expired')),
    effective_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    created_by TEXT NOT NULL,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_state_events_customer ON state_events(customer_id, effective_at DESC);

CREATE TABLE IF NOT EXISTS current_states (
    customer_id TEXT PRIMARY KEY REFERENCES customers(customer_id),
    state TEXT NOT NULL CHECK (state IN ('new_lead','contacted','need_confirmed','solution_eval','commercial_negotiation','won','lost_or_paused','expired')),
    source_event_id TEXT NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    projection_version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_current_states_event ON current_states(source_event_id);

CREATE TABLE IF NOT EXISTS sensitive_numeric_values (
    numeric_value_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    field_type TEXT NOT NULL CHECK (field_type IN ('amount','budget','discount','quote','quantity','other')),
    ciphertext TEXT NOT NULL,
    key_version TEXT NOT NULL,
    unit TEXT,
    comparison_bucket TEXT,
    access_policy TEXT NOT NULL DEFAULT 'owner_only',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sensitive_numeric_evidence ON sensitive_numeric_values(evidence_id);
