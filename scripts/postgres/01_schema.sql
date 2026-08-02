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
-- 二、RAG 知识文档表（pgvector 就绪，但当前应用默认仍使用 Chroma）
--
-- embedding 不固定维数，是为了同时容纳 256 维 Mock Hash Embedding 和用户选择的
-- 真实 Embedding 模型。正式建立 HNSW/IVFFlat 索引前，应按模型和维数隔离数据。
-- 当前表先承担政策原文、版本、生效期和来源坐标的可靠存储职责。
-- =============================================================================

CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id TEXT NOT NULL,
    version TEXT NOT NULL,
    section TEXT NOT NULL,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    document_type TEXT NOT NULL,
    authority TEXT NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    status TEXT NOT NULL,
    locale TEXT NOT NULL,
    content TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    embedding_provider TEXT,
    embedding_model TEXT,
    embedding_dimensions INTEGER,
    embedding VECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(document_id, version, section)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_domain_status_validity
    ON knowledge_documents(domain, status, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_knowledge_authority
    ON knowledge_documents(authority);

COMMENT ON TABLE knowledge_documents IS
    '政策原文和可选向量；当前 Chroma Adapter 尚未切换到本表';
COMMENT ON COLUMN knowledge_documents.source_locator IS
    '能够回到原始政策文件和章节的 JSON 字符串，不允许只保存模型摘要';
COMMENT ON COLUMN knowledge_documents.embedding IS
    '可为空；写入向量时必须同时记录 provider、model 和 dimensions';
