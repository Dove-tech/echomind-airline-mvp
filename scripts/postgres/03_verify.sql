-- EchoMind Airline MVP：初始化自检
-- 任一关键对象或种子数据缺失时让首次初始化明确失败，避免容器“看似启动成功”。
\set ON_ERROR_STOP on

DO $$
DECLARE
    seed_case_count INTEGER;
    policy_count INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'pgvector extension was not installed';
    END IF;

    IF to_regclass('public.cases') IS NULL
       OR to_regclass('public.trace_events') IS NULL
       OR to_regclass('public.knowledge_documents') IS NULL
       OR to_regclass('public.knowledge_sources') IS NULL
       OR to_regclass('public.knowledge_ingestion_runs') IS NULL THEN
        RAISE EXCEPTION 'one or more required tables are missing';
    END IF;

    SELECT COUNT(*) INTO seed_case_count
    FROM cases
    WHERE case_id LIKE 'seed_case_%';

    SELECT COUNT(*) INTO policy_count
    FROM knowledge_documents;

    IF seed_case_count < 5 THEN
        RAISE EXCEPTION 'expected at least 5 seeded cases, got %', seed_case_count;
    END IF;

    IF policy_count < 8 THEN
        RAISE EXCEPTION 'expected at least 8 policy documents, got %', policy_count;
    END IF;
END
$$;

SELECT
    current_database() AS database_name,
    (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS vector_version,
    (SELECT COUNT(*) FROM knowledge_documents) AS policy_documents,
    (SELECT COUNT(*) FROM cases WHERE case_id LIKE 'seed_case_%') AS seeded_cases,
    (SELECT COUNT(*) FROM trace_events WHERE event_id LIKE 'seed_trace_%') AS seeded_trace_events;
