-- EchoMind Airline MVP：PostgreSQL 扩展初始化
-- 该目录中的脚本只会由官方镜像在空数据卷第一次初始化时自动执行。
\set ON_ERROR_STOP on

-- pgvector 镜像已经包含扩展二进制，这条语句负责在当前数据库中启用扩展。
CREATE EXTENSION IF NOT EXISTS vector;

COMMENT ON EXTENSION vector IS
    'EchoMind Airline MVP 为后续 PostgreSQL RAG Adapter 预留的向量能力';
