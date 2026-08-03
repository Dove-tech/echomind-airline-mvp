-- EchoMind Airline MVP：应用表与知识表
-- 设计映射：docs/DESIGN.md §16（RAG）、§18（持久化）、§23（Trace）。
-- 本脚本只创建不存在的对象，不执行 DROP、TRUNCATE 或破坏性迁移。
\set ON_ERROR_STOP on

-- =============================================================================
-- 一、应用运行表
-- 下列 8 张表与 src/airline_mvp/persistence.py 的 PostgreSQL Schema 保持兼容。
-- JSON 与时间字段有意保留为 TEXT：当前 Repository 会自行序列化/反序列化，
-- 直接改成 JSONB/TIMESTAMPTZ 会改变 psycopg 返回类型并破坏现有协议。
-- =============================================================================

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    verified_subject_id TEXT,
    locale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    user_goal TEXT,
    case_summary TEXT,
    plan_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    invocation_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    authority TEXT NOT NULL,
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_responses (
    response_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    response_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, response_version)
);

CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    reason_code TEXT NOT NULL,
    response_version INTEGER NOT NULL,
    target_queue TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, reason_code, response_version)
);

CREATE TABLE IF NOT EXISTS trace_events (
    event_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cases_conversation_updated
    ON cases(conversation_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_cases_status_updated
    ON cases(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_case_started
    ON tool_calls(case_id, started_at);
CREATE INDEX IF NOT EXISTS idx_evidence_case
    ON evidence_items(case_id);
CREATE INDEX IF NOT EXISTS idx_service_responses_case_version
    ON service_responses(case_id, response_version);
CREATE INDEX IF NOT EXISTS idx_handoffs_status_created
    ON handoffs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_trace_case_sequence
    ON trace_events(case_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_trace_trace_id
    ON trace_events(trace_id);

-- =============================================================================
-- 二、RAG 来源、知识切块与导入审计
--
-- embedding 保持可变维数，使本地 FastEmbed 与远程 Embedding 模型能够按维数
-- 共存。应用会为当前实际维数创建表达式 HNSW 索引。
-- =============================================================================

CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id TEXT PRIMARY KEY,
    carrier_code TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    document_type TEXT NOT NULL,
    locale TEXT NOT NULL,
    status TEXT NOT NULL,
    retrieved_at TIMESTAMPTZ,
    content_sha256 TEXT NOT NULL,
    local_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id TEXT NOT NULL,
    version TEXT NOT NULL,
    section TEXT NOT NULL,
    source_id TEXT,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    document_type TEXT NOT NULL,
    authority TEXT NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    status TEXT NOT NULL,
    locale TEXT NOT NULL,
    content TEXT NOT NULL,
    carrier_codes TEXT[] NOT NULL DEFAULT ARRAY['*']::TEXT[],
    source_locator TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    embedding_provider TEXT,
    embedding_model TEXT,
    embedding_dimensions INTEGER,
    embedding VECTOR,
    content_sha256 TEXT,
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, ''))
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(document_id, version, section)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_domain_status_validity
    ON knowledge_documents(domain, status, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_knowledge_authority
    ON knowledge_documents(authority);
CREATE INDEX IF NOT EXISTS idx_knowledge_carrier_codes
    ON knowledge_documents USING GIN(carrier_codes);
CREATE INDEX IF NOT EXISTS idx_knowledge_search_vector
    ON knowledge_documents USING GIN(search_vector);

CREATE TABLE IF NOT EXISTS knowledge_ingestion_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    embedding_provider TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    error_summary TEXT
);

COMMENT ON TABLE knowledge_documents IS
    '航空政策原文切块、全文检索列和 pgvector 语义向量';
COMMENT ON COLUMN knowledge_documents.source_locator IS
    '能够回到原始政策文件和章节的 JSON 字符串，不允许只保存模型摘要';
COMMENT ON COLUMN knowledge_documents.embedding IS
    '可为空；写入向量时必须同时记录 provider、model 和 dimensions';
COMMENT ON TABLE knowledge_sources IS
    '官网或内部知识来源清单，保存 URL、本地快照和内容 Hash';
COMMENT ON TABLE knowledge_ingestion_runs IS
    '每次知识同步的成功/失败审计记录';
