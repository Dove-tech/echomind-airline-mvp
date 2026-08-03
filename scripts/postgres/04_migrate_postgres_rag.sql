-- 现有命名卷的 PostgreSQL RAG 前向迁移。
-- 本脚本只 CREATE/ALTER/CREATE INDEX，不删除、清空或覆盖业务数据。
\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS vector;

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

ALTER TABLE knowledge_documents
    ADD COLUMN IF NOT EXISTS source_id TEXT;
ALTER TABLE knowledge_documents
    ADD COLUMN IF NOT EXISTS carrier_codes TEXT[] NOT NULL
    DEFAULT ARRAY['*']::TEXT[];
ALTER TABLE knowledge_documents
    ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
ALTER TABLE knowledge_documents
    ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, ''))
    ) STORED;

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

SELECT
    to_regclass('public.knowledge_sources') AS knowledge_sources,
    to_regclass('public.knowledge_documents') AS knowledge_documents,
    to_regclass('public.knowledge_ingestion_runs') AS knowledge_ingestion_runs;
