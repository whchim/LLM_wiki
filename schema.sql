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

-- L2 队列化（幂等）：compile_tasks 从"记录表"升级为可并发认领的工作队列。
-- 背景：原表无租户、无租约、无尝试次数，时间戳还是 TEXT——多 worker 会抢同一任务，
-- 崩溃后任务会卡死，也无法按租户隔离（详见 docs/WIKI-70 §5 L2）。
ALTER TABLE compile_tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE compile_tasks ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE compile_tasks ADD COLUMN IF NOT EXISTS leased_by TEXT;
ALTER TABLE compile_tasks ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE compile_tasks ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;
ALTER TABLE compile_tasks ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 100;
ALTER TABLE compile_tasks ALTER COLUMN started_at   TYPE TIMESTAMPTZ USING started_at::timestamptz;
ALTER TABLE compile_tasks ALTER COLUMN completed_at TYPE TIMESTAMPTZ USING completed_at::timestamptz;
-- 认领索引：按 (status, next_retry_at, priority, id) 取下一个任务，配合 FOR UPDATE SKIP LOCKED
CREATE INDEX IF NOT EXISTS idx_tasks_claim  ON compile_tasks(status, next_retry_at, priority, id);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON compile_tasks(tenant_id, status);

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

-- 应用层：客户别名表
-- 用途：销售按内部习惯用中文简称（如「某某项目」）提交纪要，系统内部仍只存脱敏代号。
-- 别名只在本系统内用于选择，不作为业务事实；customer_id 仍是状态机唯一键。
-- 唯一键为 (alias, customer_id)：同一客户可有多个叫法；不同客户不得共用同一别名
-- （插入时会因冲突被显式拒绝，避免把两个客户静默合并）。
CREATE TABLE IF NOT EXISTS customer_aliases (
    alias       TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (alias, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_customer_aliases_customer ON customer_aliases(customer_id);

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
-- 内容级幂等（防"同一份纪要重复提交生成多条会话"）：按正文指纹找同客户的历史洽谈
CREATE INDEX IF NOT EXISTS idx_evidence_content_hash ON evidence(content_hash);

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

-- 人工处置（闭环）：reviewer/admin 可"补充事实后重开"或"关闭会话"，
-- 处置原因与处置人必须落库（关闭无原因等于没闭环）。
ALTER TABLE clarification_sessions ADD COLUMN IF NOT EXISTS resolution_note TEXT;
ALTER TABLE clarification_sessions ADD COLUMN IF NOT EXISTS resolved_by TEXT;
ALTER TABLE clarification_sessions ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

-- 软删除（归档）：仅管理员可删澄清会话与其产生的状态建议；
-- **不删状态事件**——客户事实只能追加更正/撤回/过期（SA-02），不可删除。
ALTER TABLE clarification_sessions ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE clarification_sessions ADD COLUMN IF NOT EXISTS deleted_by TEXT;

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

-- 状态建议的软删除（归档）：会话归档时一并归档其建议（仅管理员，可恢复）
ALTER TABLE state_proposals ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE state_proposals ADD COLUMN IF NOT EXISTS deleted_by TEXT;

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

-- ============================================================
-- L3 多租户隔离（幂等）：全表 tenant_id + PostgreSQL RLS
-- ------------------------------------------------------------
-- 设计（详见 docs/WIKI-70 §5 L3）：
--   · 每张业务表加 tenant_id，**默认值动态取当前租户** `current_setting('app.tenant_id')`——
--     这样应用代码里的 INSERT 不必逐个改写就自动落在当前租户，历史行回填 'default'；
--   · 启用 **FORCE ROW LEVEL SECURITY**：连表 owner 也受策略约束；
--   · 策略 = tenant_id 必须等于会话变量 app.tenant_id（`core/db.get_conn` 每次 checkout 注入，
--     HTTP 请求由中间件按 JWT 的 tenant claim 绑定），USING 管读、WITH CHECK 管写
--     ——跨租户读写由**数据库**拒绝，不靠应用自觉；
--   · **RLS 只对非超级用户生效**：Docker 默认的 POSTGRES_USER 是超级用户，会绕过 RLS，
--     因此生产/验收要用受限角色（见 docker/initdb/20-app-role，测试见 test_tenant_isolation.py）；
--   · **users 表豁免 RLS**：登录发生在"还不知道租户"之前，按用户名查身份必须跨租户可见，
--     用户归属由 users.tenant_id 在应用层判定。
-- ============================================================
ALTER TABLE users ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE users ALTER COLUMN tenant_id SET DEFAULT current_setting('app.tenant_id', true);
UPDATE users SET tenant_id = 'default' WHERE tenant_id IS NULL;
ALTER TABLE users ALTER COLUMN tenant_id SET NOT NULL;

-- ============================================================
-- L4 模型配置进库 + 用量记账（幂等）
-- ------------------------------------------------------------
-- 现状问题：模型配置全是**进程级环境变量**（MODEL_API_KEY/MODEL_BASE_URL/MODEL_NAME/
-- MODEL_TIMEOUT、向量用的 DASHSCOPE_API_KEY），一份配置服务所有租户：租户无法自带 key、
-- 改配置要重启、密钥明文放 env、**没有按租户的用量与配额**。
-- 目标：租户级配置进库（密钥 AES-GCM 加密），调用前后按租户记账与拦截。
--   · tenant_model_configs：按 (tenant_id, purpose) 一条；purpose=default 兜底，
--     可给 compile/review/answer/clarification/state 分别配模型；
--   · llm_usage：每次模型调用的 token/延迟/成败记账，供配额拦截与账单看板；
--   · 两者都进 RLS（下面的策略数组已包含），租户只能看到自己的配置与用量。
-- ============================================================
CREATE TABLE IF NOT EXISTS tenant_model_configs (
    id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id             TEXT NOT NULL,
    purpose               TEXT NOT NULL DEFAULT 'default',
    provider              TEXT NOT NULL DEFAULT 'openai_compatible',
    base_url              TEXT,
    model                 TEXT NOT NULL,
    api_key_ciphertext    TEXT,
    api_key_key_version   TEXT,
    max_tokens            INTEGER NOT NULL DEFAULT 4000,
    temperature           REAL NOT NULL DEFAULT 0,
    daily_token_quota     INTEGER,                 -- NULL = 不限
    enabled               BOOLEAN NOT NULL DEFAULT true,
    updated_by            TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, purpose)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    purpose       TEXT NOT NULL DEFAULT 'default',
    model         TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    ok            BOOLEAN NOT NULL DEFAULT true,
    trace_id      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant ON llm_usage(tenant_id, created_at DESC);

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'knowledge_entries','compile_tasks','pending_reviews','search_logs','audit_logs',
    'contributors','conflicts','customer_aliases','trace_events','health_reports',
    'customers','conversations','evidence','clarification_sessions','clarification_turns',
    'clarification_answers','state_proposals','state_decisions','state_events',
    'current_states','sensitive_numeric_values',
    'tenant_model_configs','llm_usage'
  ]
  LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id TEXT', t);
    -- 默认值动态取当前租户：INSERT 不写 tenant_id 也会落在正确租户
    EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT current_setting(''app.tenant_id'', true)', t);
    EXECUTE format('UPDATE %I SET tenant_id = %L WHERE tenant_id IS NULL', t, 'default');
    EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET NOT NULL', t);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_tenant ON %I(tenant_id)', t, t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I '
      'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
      'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
  END LOOP;
END $$;
