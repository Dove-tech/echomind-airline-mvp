"""真实 PostgreSQL/pgvector 集成测试；默认跳过，不访问开发者数据库。"""

from __future__ import annotations

import os

import pytest

from airline_mvp.config import RuntimeSettings
from airline_mvp.knowledge import build_knowledge_service
from airline_mvp.persistence import PostgreSQLDatabase


POSTGRES_URL = os.getenv("AIRLINE_MVP_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 AIRLINE_MVP_TEST_POSTGRES_URL",
)


def test_postgres_hybrid_rag_and_carrier_isolation() -> None:
    settings = RuntimeSettings(
        llm_backend="mock",
        database_backend="postgres",
        database_url=POSTGRES_URL,
        checkpoint_backend="memory",
        knowledge_backend="postgres",
        embedding_backend="local_fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_dimensions=512,
    )
    database = PostgreSQLDatabase(POSTGRES_URL or "", pool_size=2)
    try:
        knowledge = build_knowledge_service(
            settings=settings,
            database=database,
        )
        ek_hits = knowledge.search(
            "航班取消后如何退款或改签",
            ["journey", "refund"],
            "2026-08-15",
            3,
            ["EK"],
        )
        cz_hits = knowledge.search(
            "航班取消后如何退款或改签",
            ["journey", "refund"],
            "2026-08-15",
            6,
            ["CZ"],
        )

        assert ek_hits
        assert any(hit["authority"] == "airline_official_web" for hit in ek_hits)
        assert all(
            hit["authority"] != "airline_official_web" for hit in cz_hits
        )
        assert all(hit["sourceUrl"] for hit in ek_hits)

        health = knowledge.health()
        assert health["backend"] == "PostgreSQLKnowledgeStore"
        assert health["vectorExtension"]
        assert health["hnswIndexes"] >= 1
        assert health["embeddedDocuments"] >= 32
    finally:
        database.close()
