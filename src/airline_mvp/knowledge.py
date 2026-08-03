"""限定业务域的混合 RAG：PostgreSQL FTS + pgvector 与确定性测试实现。

设计映射
--------
- §16：文档元数据、业务域/生效日期过滤、向量检索、权威等级重排，
  以及确切条款下钻。
- §19：检索到的政策会转化为 Evidence；检索摘要不能在无提示的情况下
  替代确切的原始条款。

面试运行档使用 PostgreSQL 保存原文、元数据和向量，使用 pgvector 做语义召回、
PostgreSQL FTS 做关键词召回，再通过 RRF 融合。Embedding 默认由本地
FastEmbed 中文模型生成，也可以切换 OpenAI-compatible Embedding API。

``LocalKnowledgeStore`` 和 Hash Embedding 只用于无需数据库和网络的单元测试，
真实后端显式失败时不会静默回退。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .config import ConfigurationError, RuntimeSettings
from .paths import KNOWLEDGE_ROOT, RUNTIME_ROOT, ensure_runtime_dirs
from .persistence import Database


class PolicyDocument(BaseModel):
    document_id: str
    version: str
    title: str
    domain: str
    document_type: str
    authority: str
    valid_from: str
    valid_to: str | None = None
    status: str
    section: str
    locale: str
    text: str
    carrier_codes: list[str] = Field(default_factory=lambda: ["*"])
    source_url: str | None = None
    source_path: str | None = None
    retrieved_at: str | None = None
    content_sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_fixture(cls, data: dict[str, Any]) -> "PolicyDocument":
        return cls(
            document_id=data["documentId"],
            version=data["version"],
            title=data["title"],
            domain=data["domain"],
            document_type=data["documentType"],
            authority=data["authority"],
            valid_from=data["validFrom"],
            valid_to=data.get("validTo"),
            status=data["status"],
            section=data["section"],
            locale=data["locale"],
            text=data["text"],
            carrier_codes=data.get("carrierCodes", ["*"]),
            source_url=data.get("sourceUrl"),
            source_path=data.get("sourcePath"),
            retrieved_at=data.get("retrievedAt"),
            content_sha256=data.get("contentSha256"),
            metadata=data.get("metadata", {}),
        )


class KnowledgeHit(BaseModel):
    document: PolicyDocument
    score: float


def _tokens(text: str) -> list[str]:
    """无需额外模型，对拉丁单词和单个 CJK 字符进行分词。"""

    return re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())


# =============================================================================
# Mock Embedding：无网络、结果确定，供测试和离线演示使用
# =============================================================================


def hash_embedding(text: str, dimensions: int = 256) -> list[float]:
    """稳定的 Feature Hashing Embedding。

    它并非生产级语义模型，只用于保持单元测试完全离线并使检索结果确定。
    真实 Embedding Adapter 可以在 ``KnowledgeStore``
    接口后替换该实现。
    """

    vector = [0.0] * dimensions
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _effective(document: PolicyDocument, as_of: str) -> bool:
    target = date.fromisoformat(as_of)
    start = date.fromisoformat(document.valid_from)
    end = date.fromisoformat(document.valid_to) if document.valid_to else None
    return document.status == "active" and start <= target and (end is None or target <= end)


def _carrier_visible(
    document: PolicyDocument,
    carrier_codes: list[str] | None,
) -> bool:
    """通用内部规则对所有航司可见，官网政策只能用于对应承运人。"""

    document_codes = {code.upper() for code in document.carrier_codes}
    if "*" in document_codes:
        return True
    requested = {code.upper() for code in (carrier_codes or [])}
    return bool(document_codes & requested)


class EmbeddingProvider(Protocol):
    """Mock 与真实 Embedding 共同遵守的最小接口。"""

    @property
    def identity(self) -> str: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashEmbeddingProvider:
    """Mock：使用本地 Feature Hashing，不访问任何外部服务。"""

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    @property
    def identity(self) -> str:
        return f"mock_hash_{self.dimensions}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(text, self.dimensions) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return hash_embedding(text, self.dimensions)


# =============================================================================
# 真实本地 Embedding：FastEmbed + ONNX
# =============================================================================


class FastEmbedEmbeddingProvider:
    """真实本地中文 Embedding，不需要外部 API Key。

    默认模型 ``BAAI/bge-small-zh-v1.5`` 为 512 维 ONNX 模型。第一次运行会
    下载约 90MB 模型到 ``.runtime/fastembed_cache``（Docker 中使用命名卷），
    后续启动直接复用。它是真实语义模型，与仅供测试的 Hash Embedding 不同。
    """

    DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

    def __init__(self, model_name: str | None, cache_dir: Path) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "本地真实 Embedding 需要 fastembed：python -m pip install -e ."
            ) from exc

        self.model = model_name or self.DEFAULT_MODEL
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = TextEmbedding(
            model_name=self.model,
            cache_dir=str(cache_dir),
        )
        self.dimensions = int(TextEmbedding.get_embedding_size(self.model))

    @property
    def identity(self) -> str:
        return f"fastembed_{self.model}_{self.dimensions}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self.client.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        vectors = list(self.client.query_embed(text))
        if not vectors:
            raise RuntimeError("FastEmbed 没有返回 Query Embedding")
        return vectors[0].tolist()


# =============================================================================
# 真实远程 Embedding：OpenAI-compatible API
# =============================================================================


class OpenAICompatibleEmbeddingProvider:
    """真实 Embedding Adapter。

    ``langchain_openai.OpenAIEmbeddings`` 只在启用真实模式时导入，因此默认
    Mock 测试不会产生网络请求。API Key 不会进入文档元数据或 Trace。
    """

    def __init__(self, settings: RuntimeSettings) -> None:
        from langchain_openai import OpenAIEmbeddings

        kwargs: dict[str, Any] = {
            "model": settings.embedding_model or "",
            "api_key": settings.embedding_api_key,
            "base_url": settings.embedding_base_url,
            "max_retries": settings.llm_max_retries,
            "request_timeout": settings.llm_timeout_seconds,
        }
        if settings.embedding_dimensions is not None:
            kwargs["dimensions"] = settings.embedding_dimensions
        self.client = OpenAIEmbeddings(**kwargs)
        self.model = settings.embedding_model or "unknown"
        self.dimensions = settings.embedding_dimensions

    @property
    def identity(self) -> str:
        return f"openai_{self.model}_{self.dimensions or 'default'}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.client.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.client.embed_query(text)


def build_embedding_provider(
    settings: RuntimeSettings,
    *,
    runtime_root: Path | None = None,
) -> EmbeddingProvider:
    if settings.embedding_backend == "mock":
        return HashEmbeddingProvider(
            dimensions=settings.embedding_dimensions or 256
        )
    if settings.embedding_backend == "local_fastembed":
        return FastEmbedEmbeddingProvider(
            settings.embedding_model,
            (runtime_root or RUNTIME_ROOT) / "fastembed_cache",
        )
    if settings.embedding_backend == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider(settings)
    raise ConfigurationError(
        f"不支持的 Embedding backend：{settings.embedding_backend}"
    )


class KnowledgeStore(Protocol):
    def search(
        self,
        query: str,
        domains: list[str],
        as_of: str,
        top_k: int,
        carrier_codes: list[str] | None = None,
    ) -> list[KnowledgeHit]: ...

    def get_clause(
        self, document_id: str, version: str, section: str
    ) -> PolicyDocument | None: ...


class LocalKnowledgeStore:
    """供单元测试和回退模式使用的无额外依赖向量检索实现。"""

    def __init__(
        self,
        documents: list[PolicyDocument],
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.documents = documents
        self.embedding_provider = embedding_provider or HashEmbeddingProvider()
        document_vectors = self.embedding_provider.embed_documents(
            [f"{doc.title} {doc.text}" for doc in documents]
        )
        self._embeddings = {
            (doc.document_id, doc.version, doc.section): vector
            for doc, vector in zip(documents, document_vectors, strict=True)
        }

    def search(
        self,
        query: str,
        domains: list[str],
        as_of: str,
        top_k: int = 3,
        carrier_codes: list[str] | None = None,
    ) -> list[KnowledgeHit]:
        query_vector = self.embedding_provider.embed_query(query)
        authority_boost = {"official_policy": 0.08, "approved_faq": 0.03}
        hits: list[KnowledgeHit] = []
        for document in self.documents:
            if (
                document.domain not in domains
                or not _effective(document, as_of)
                or not _carrier_visible(document, carrier_codes)
            ):
                continue
            base = cosine(
                query_vector,
                self._embeddings[(document.document_id, document.version, document.section)],
            )
            score = base + authority_boost.get(document.authority, 0.0)
            hits.append(KnowledgeHit(document=document, score=round(score, 6)))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]

    def get_clause(
        self, document_id: str, version: str, section: str
    ) -> PolicyDocument | None:
        return next(
            (
                doc
                for doc in self.documents
                if doc.document_id == document_id
                and doc.version == version
                and doc.section == section
            ),
            None,
        )

    def health(self) -> dict[str, Any]:
        return {
            "backend": type(self).__name__,
            "documents": len(self.documents),
            "embeddedDocuments": len(self._embeddings),
            "vectorExtension": None,
            "hybridSearch": False,
        }


def _content_hash(document: PolicyDocument) -> str:
    return document.content_sha256 or hashlib.sha256(
        document.text.encode("utf-8")
    ).hexdigest()


def _vector_literal(vector: list[float]) -> str:
    """将数值向量转换成 pgvector 可接收的文本表示，不拼接进 SQL。"""

    return "[" + ",".join(f"{value:.10g}" for value in vector) + "]"


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


class PostgreSQLKnowledgeStore:
    """真实 PostgreSQL 混合知识库。

    - 原文、版本、生效期、来源坐标和 Embedding 全部保存在 PostgreSQL；
    - pgvector 负责余弦距离召回；
    - PostgreSQL ``tsvector`` / GIN 负责全文召回；
    - 应用层只做 RRF 排名融合和权威等级微调；
    - 精确条款下钻重新读取数据库原文，不相信召回摘要。

    数据同步使用 upsert 和内容 Hash，只新增或更新发生变化的文档，从不清空表。
    """

    def __init__(
        self,
        documents: list[PolicyDocument],
        database: Database,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        if database.backend_name != "postgres":
            raise ConfigurationError("PostgreSQLKnowledgeStore 需要 PostgreSQL Database")
        self.database = database
        self.embedding_provider = embedding_provider
        self.initialize()
        self.sync_documents(documents)

    def initialize(self) -> None:
        statements = [
            "CREATE EXTENSION IF NOT EXISTS vector",
            """
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
            )
            """,
            """
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS knowledge_ingestion_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                embedding_provider TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                started_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ,
                error_summary TEXT
            )
            """,
            """
            ALTER TABLE knowledge_documents
            ADD COLUMN IF NOT EXISTS source_id TEXT
            """,
            """
            ALTER TABLE knowledge_documents
            ADD COLUMN IF NOT EXISTS carrier_codes TEXT[] NOT NULL
            DEFAULT ARRAY['*']::TEXT[]
            """,
            """
            ALTER TABLE knowledge_documents
            ADD COLUMN IF NOT EXISTS content_sha256 TEXT
            """,
            """
            ALTER TABLE knowledge_documents
            ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
            GENERATED ALWAYS AS (
                to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, ''))
            ) STORED
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_domain_status_validity
            ON knowledge_documents(domain, status, valid_from, valid_to)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_carrier_codes
            ON knowledge_documents USING GIN(carrier_codes)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_search_vector
            ON knowledge_documents USING GIN(search_vector)
            """,
        ]
        with self.database.connect(write=True) as connection:
            for statement in statements:
                connection.execute(statement)

    def sync_documents(self, documents: list[PolicyDocument]) -> None:
        """按内容 Hash 增量写入原文和向量，并记录可审计的导入批次。"""

        if not documents:
            raise ConfigurationError("PostgreSQL RAG 没有可导入的知识文档")

        run_id = f"ing_{uuid.uuid4().hex[:16]}"
        started_at = datetime.now(timezone.utc)
        with self.database.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO knowledge_ingestion_runs(
                    run_id, status, embedding_provider, started_at
                ) VALUES (?, 'running', ?, ?)
                """,
                (run_id, self.embedding_provider.identity, started_at),
            )
            rows = connection.execute(
                """
                SELECT document_id, version, section, content_sha256,
                       embedding_provider, embedding_dimensions,
                       (embedding IS NOT NULL) AS has_embedding
                FROM knowledge_documents
                """
            ).fetchall()

        existing = {
            (row["document_id"], row["version"], row["section"]): row
            for row in rows
        }
        needs_embedding: list[PolicyDocument] = []
        for document in documents:
            row = existing.get(
                (document.document_id, document.version, document.section)
            )
            if (
                row is None
                or row["content_sha256"] != _content_hash(document)
                or row["embedding_provider"] != self.embedding_provider.identity
                or not row["has_embedding"]
            ):
                needs_embedding.append(document)

        vectors: dict[tuple[str, str, str], list[float]] = {}
        try:
            if needs_embedding:
                embedded = self.embedding_provider.embed_documents(
                    [f"{doc.title}\n{doc.text}" for doc in needs_embedding]
                )
                vectors = {
                    (doc.document_id, doc.version, doc.section): vector
                    for doc, vector in zip(
                        needs_embedding,
                        embedded,
                        strict=True,
                    )
                }

            with self.database.connect(write=True) as connection:
                official_versions = {
                    (document.document_id, document.version)
                    for document in documents
                    if document.authority == "airline_official_web"
                }
                for document_id, current_version in official_versions:
                    # 旧官网快照只标记为 superseded，不删除，Trace 中已有的
                    # document/version/section 仍然能够精确回放。
                    connection.execute(
                        """
                        UPDATE knowledge_documents
                        SET status='superseded', updated_at=CURRENT_TIMESTAMP
                        WHERE document_id=? AND version<>?
                          AND authority='airline_official_web'
                          AND status='active'
                        """,
                        (document_id, current_version),
                    )
                for document in documents:
                    source_id = str(
                        document.metadata.get("sourceId") or document.document_id
                    )
                    source_url = document.source_url or f"internal://{source_id}"
                    # 官网源文件的 Hash 代表“整份快照”，不能被最后一个切块的
                    # Hash 覆盖。knowledge_documents 仍保存各切块自己的 Hash，
                    # 两者配合后既能验证原始文件，也能判断单个切块是否变化。
                    source_content_hash = str(
                        document.metadata.get("sourceContentSha256")
                        or _content_hash(document)
                    )
                    carrier_code = next(
                        (
                            code
                            for code in document.carrier_codes
                            if code != "*"
                        ),
                        "*",
                    )
                    connection.execute(
                        """
                        INSERT INTO knowledge_sources(
                            source_id, carrier_code, title, source_url,
                            document_type, locale, status, retrieved_at,
                            content_sha256, local_path, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_id) DO UPDATE SET
                            carrier_code=excluded.carrier_code,
                            title=excluded.title,
                            source_url=excluded.source_url,
                            document_type=excluded.document_type,
                            locale=excluded.locale,
                            status=excluded.status,
                            retrieved_at=excluded.retrieved_at,
                            content_sha256=excluded.content_sha256,
                            local_path=excluded.local_path,
                            metadata_json=excluded.metadata_json,
                            updated_at=CURRENT_TIMESTAMP
                        """,
                        (
                            source_id,
                            carrier_code,
                            document.title,
                            source_url,
                            document.document_type,
                            document.locale,
                            document.status,
                            document.retrieved_at,
                            source_content_hash,
                            document.source_path,
                            json.dumps(document.metadata, ensure_ascii=False),
                        ),
                    )

                    key = (
                        document.document_id,
                        document.version,
                        document.section,
                    )
                    vector = vectors.get(key)
                    dimensions = len(vector) if vector is not None else None
                    locator = {
                        "sourceId": source_id,
                        "url": document.source_url,
                        "path": document.source_path,
                        "retrievedAt": document.retrieved_at,
                        "contentSha256": _content_hash(document),
                        "section": document.section,
                    }
                    connection.execute(
                        """
                        INSERT INTO knowledge_documents(
                            document_id, version, section, source_id,
                            title, domain, document_type, authority,
                            valid_from, valid_to, status, locale, content,
                            carrier_codes, source_locator, metadata_json,
                            content_sha256, embedding_provider, embedding_model,
                            embedding_dimensions, embedding
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?::vector
                        )
                        ON CONFLICT(document_id, version, section) DO UPDATE SET
                            source_id=excluded.source_id,
                            title=excluded.title,
                            domain=excluded.domain,
                            document_type=excluded.document_type,
                            authority=excluded.authority,
                            valid_from=excluded.valid_from,
                            valid_to=excluded.valid_to,
                            status=excluded.status,
                            locale=excluded.locale,
                            content=excluded.content,
                            carrier_codes=excluded.carrier_codes,
                            source_locator=excluded.source_locator,
                            metadata_json=excluded.metadata_json,
                            content_sha256=excluded.content_sha256,
                            embedding_provider=CASE
                                WHEN excluded.embedding IS NULL
                                THEN knowledge_documents.embedding_provider
                                ELSE excluded.embedding_provider
                            END,
                            embedding_model=CASE
                                WHEN excluded.embedding IS NULL
                                THEN knowledge_documents.embedding_model
                                ELSE excluded.embedding_model
                            END,
                            embedding_dimensions=COALESCE(
                                excluded.embedding_dimensions,
                                knowledge_documents.embedding_dimensions
                            ),
                            embedding=COALESCE(
                                excluded.embedding,
                                knowledge_documents.embedding
                            ),
                            updated_at=CURRENT_TIMESTAMP
                        """,
                        (
                            document.document_id,
                            document.version,
                            document.section,
                            source_id,
                            document.title,
                            document.domain,
                            document.document_type,
                            document.authority,
                            document.valid_from,
                            document.valid_to,
                            document.status,
                            document.locale,
                            document.text,
                            document.carrier_codes,
                            json.dumps(locator, ensure_ascii=False),
                            json.dumps(document.metadata, ensure_ascii=False),
                            _content_hash(document),
                            self.embedding_provider.identity if vector else None,
                            getattr(self.embedding_provider, "model", None),
                            dimensions,
                            _vector_literal(vector) if vector else None,
                        ),
                    )

                # 当前默认模型固定 512 维。表达式索引既兼容无固定维数的列，
                # 又允许 pgvector 在当前维数上使用 HNSW。
                all_dimensions = {
                    len(vector) for vector in vectors.values() if vector
                }
                for dimensions in all_dimensions:
                    connection.execute(
                        f"""
                        CREATE INDEX IF NOT EXISTS
                        idx_knowledge_embedding_{dimensions}_hnsw
                        ON knowledge_documents USING hnsw(
                            (embedding::vector({dimensions})) vector_cosine_ops
                        )
                        WHERE embedding_dimensions = {dimensions}
                        """
                    )

                connection.execute(
                    """
                    UPDATE knowledge_ingestion_runs
                    SET status='succeeded', source_count=?, chunk_count=?,
                        completed_at=CURRENT_TIMESTAMP
                    WHERE run_id=?
                    """,
                    (
                        len({doc.document_id for doc in documents}),
                        len(documents),
                        run_id,
                    ),
                )
        except Exception as exc:
            with self.database.connect(write=True) as connection:
                connection.execute(
                    """
                    UPDATE knowledge_ingestion_runs
                    SET status='failed', completed_at=CURRENT_TIMESTAMP,
                        error_summary=?
                    WHERE run_id=?
                    """,
                    (type(exc).__name__, run_id),
                )
            raise

    @staticmethod
    def _document_from_row(row: Any) -> PolicyDocument:
        locator = _json_object(row["source_locator"])
        metadata = _json_object(row["metadata_json"])
        return PolicyDocument(
            document_id=row["document_id"],
            version=row["version"],
            section=row["section"],
            title=row["title"],
            domain=row["domain"],
            document_type=row["document_type"],
            authority=row["authority"],
            valid_from=str(row["valid_from"]),
            valid_to=str(row["valid_to"]) if row["valid_to"] else None,
            status=row["status"],
            locale=row["locale"],
            text=row["content"],
            carrier_codes=list(row["carrier_codes"] or ["*"]),
            source_url=locator.get("url"),
            source_path=locator.get("path"),
            retrieved_at=locator.get("retrievedAt"),
            content_sha256=row["content_sha256"],
            metadata=metadata,
        )

    def search(
        self,
        query: str,
        domains: list[str],
        as_of: str,
        top_k: int = 3,
        carrier_codes: list[str] | None = None,
    ) -> list[KnowledgeHit]:
        query_vector = self.embedding_provider.embed_query(query)
        dimensions = len(query_vector)
        candidate_limit = max(top_k * 4, 12)
        visible_carriers = sorted(
            {"*", *(code.upper() for code in (carrier_codes or []))}
        )
        selected_columns = """
            document_id, version, section, title, domain, document_type,
            authority, valid_from, valid_to, status, locale, content,
            carrier_codes, source_locator, metadata_json, content_sha256
        """

        with self.database.connect() as connection:
            vector_rows = connection.execute(
                f"""
                SELECT {selected_columns},
                       (embedding::vector({dimensions}) <=>
                        ?::vector({dimensions})) AS distance
                FROM knowledge_documents
                WHERE domain = ANY(?::text[])
                  AND status = 'active'
                  AND valid_from <= ?::date
                  AND (valid_to IS NULL OR valid_to >= ?::date)
                  AND carrier_codes && ?::text[]
                  AND embedding_provider = ?
                  AND embedding_dimensions = {dimensions}
                  AND embedding IS NOT NULL
                ORDER BY (embedding::vector({dimensions}) <=>
                          ?::vector({dimensions}))
                LIMIT ?
                """,
                (
                    _vector_literal(query_vector),
                    domains,
                    as_of,
                    as_of,
                    visible_carriers,
                    self.embedding_provider.identity,
                    _vector_literal(query_vector),
                    candidate_limit,
                ),
            ).fetchall()
            lexical_rows = connection.execute(
                f"""
                SELECT {selected_columns},
                       ts_rank_cd(
                           search_vector,
                           plainto_tsquery('simple', ?)
                       ) AS lexical_score
                FROM knowledge_documents
                WHERE domain = ANY(?::text[])
                  AND status = 'active'
                  AND valid_from <= ?::date
                  AND (valid_to IS NULL OR valid_to >= ?::date)
                  AND carrier_codes && ?::text[]
                  AND search_vector @@ plainto_tsquery('simple', ?)
                ORDER BY lexical_score DESC
                LIMIT ?
                """,
                (
                    query,
                    domains,
                    as_of,
                    as_of,
                    visible_carriers,
                    query,
                    candidate_limit,
                ),
            ).fetchall()

        # RRF 避免把 pgvector 距离和 FTS rank 直接归一化到同一数值尺度。
        scores: dict[tuple[str, str, str], float] = {}
        rows_by_key: dict[tuple[str, str, str], Any] = {}
        for ranking in (vector_rows, lexical_rows):
            for rank, row in enumerate(ranking, start=1):
                key = (row["document_id"], row["version"], row["section"])
                scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
                rows_by_key[key] = row

        authority_boost = {
            "airline_official_web": 0.003,
            "official_policy": 0.002,
            "approved_faq": 0.001,
        }
        hits = [
            KnowledgeHit(
                document=self._document_from_row(rows_by_key[key]),
                score=round(
                    score
                    + authority_boost.get(rows_by_key[key]["authority"], 0.0),
                    6,
                ),
            )
            for key, score in scores.items()
        ]
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]

    def get_clause(
        self,
        document_id: str,
        version: str,
        section: str,
    ) -> PolicyDocument | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT document_id, version, section, title, domain,
                       document_type, authority, valid_from, valid_to,
                       status, locale, content, carrier_codes,
                       source_locator, metadata_json, content_sha256
                FROM knowledge_documents
                WHERE document_id=? AND version=? AND section=?
                """,
                (document_id, version, section),
            ).fetchone()
        return self._document_from_row(row) if row is not None else None

    def health(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT extversion FROM pg_extension WHERE extname='vector')
                        AS vector_version,
                    (SELECT COUNT(*) FROM knowledge_sources) AS sources,
                    (SELECT COUNT(*) FROM knowledge_documents
                     WHERE status='active') AS active_documents,
                    (SELECT COUNT(*) FROM knowledge_documents
                     WHERE embedding IS NOT NULL) AS embedded_documents,
                    (SELECT COUNT(*) FROM pg_indexes
                     WHERE tablename='knowledge_documents'
                       AND indexname LIKE 'idx_knowledge_embedding_%_hnsw')
                        AS hnsw_indexes
                """
            ).fetchone()
        return {
            "backend": type(self).__name__,
            "sources": int(row["sources"]),
            "activeDocuments": int(row["active_documents"]),
            "embeddedDocuments": int(row["embedded_documents"]),
            "vectorExtension": row["vector_version"],
            "hnswIndexes": int(row["hnsw_indexes"]),
            "hybridSearch": True,
        }


class KnowledgeService:
    """供知识类 Tool 调用的统一门面。"""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    @property
    def source_system(self) -> str:
        """返回 Trace 中使用的真实知识后端名称。"""

        if isinstance(self.store, PostgreSQLKnowledgeStore):
            return "postgresql_rag"
        return "local_rag"

    def health(self) -> dict[str, Any]:
        health = getattr(self.store, "health", None)
        return health() if callable(health) else {"backend": type(self.store).__name__}

    def search(
        self,
        query: str,
        domains: list[str],
        as_of: str,
        top_k: int = 3,
        carrier_codes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "documentId": hit.document.document_id,
                "version": hit.document.version,
                "title": hit.document.title,
                "domain": hit.document.domain,
                "authority": hit.document.authority,
                "validFrom": hit.document.valid_from,
                "validTo": hit.document.valid_to,
                "section": hit.document.section,
                "summary": hit.document.text,
                "carrierCodes": hit.document.carrier_codes,
                "sourceUrl": hit.document.source_url,
                "sourcePath": hit.document.source_path,
                "retrievedAt": hit.document.retrieved_at,
                "contentSha256": hit.document.content_sha256,
                "score": hit.score,
            }
            for hit in self.store.search(
                query,
                domains,
                as_of,
                top_k,
                carrier_codes,
            )
        ]

    def get_clause(
        self, document_id: str, version: str, section: str
    ) -> dict[str, Any] | None:
        document = self.store.get_clause(document_id, version, section)
        if document is None:
            return None
        return {
            "documentId": document.document_id,
            "version": document.version,
            "title": document.title,
            "domain": document.domain,
            "authority": document.authority,
            "validFrom": document.valid_from,
            "validTo": document.valid_to,
            "section": document.section,
            "text": document.text,
            "carrierCodes": document.carrier_codes,
            "sourceUrl": document.source_url,
            "sourcePath": document.source_path,
            "retrievedAt": document.retrieved_at,
            "contentSha256": document.content_sha256,
        }


def load_policy_documents(
    path: Path = KNOWLEDGE_ROOT / "policies.json",
) -> list[PolicyDocument]:
    """加载内部规则和已经抓取、切块的官网快照。

    应用启动只读取本地快照，不在请求路径访问互联网。官网更新由
    ``scripts/sync_official_knowledge.py`` 显式执行并保留内容 Hash。
    """

    paths = [path, path.with_name("official_policies.json")]
    documents: list[PolicyDocument] = []
    for current in paths:
        if not current.exists():
            continue
        with current.open("r", encoding="utf-8") as handle:
            documents.extend(
                PolicyDocument.from_fixture(item) for item in json.load(handle)
            )
    return documents


def build_knowledge_service(
    *,
    settings: RuntimeSettings | None = None,
    runtime_root: Path | None = None,
    database: Database | None = None,
) -> KnowledgeService:
    """装配知识检索服务。

    没有传入 ``settings`` 时只构建确定性 Local Store，供单元测试使用。
    真实运行档必须显式选择 PostgreSQL，并复用业务数据库连接；不会回退其他
    向量库或内存索引。
    """

    documents = load_policy_documents()
    if settings is None:
        return KnowledgeService(
            LocalKnowledgeStore(documents, HashEmbeddingProvider())
        )

    embedding_provider = build_embedding_provider(
        settings,
        runtime_root=runtime_root,
    )
    if settings.knowledge_backend == "local":
        return KnowledgeService(
            LocalKnowledgeStore(documents, embedding_provider)
        )
    if settings.knowledge_backend != "postgres" or database is None:
        raise ConfigurationError(
            "真实 RAG 需要 AIRLINE_MVP_KNOWLEDGE_BACKEND=postgres 和数据库连接"
        )
    ensure_runtime_dirs()
    return KnowledgeService(
        PostgreSQLKnowledgeStore(
            documents,
            database,
            embedding_provider,
        )
    )
