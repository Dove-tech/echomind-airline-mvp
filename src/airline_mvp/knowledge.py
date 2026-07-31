"""限定业务域的 RAG，提供 Chroma Adapter 和确定性回退实现。

设计映射
--------
- §16：文档元数据、业务域/生效日期过滤、向量检索、权威等级重排，
  以及确切条款下钻。
- §19：检索到的政策会转化为 Evidence；检索摘要不能在无提示的情况下
  替代确切的原始条款。

默认模式使用确定性 Hash Embedding，因此测试无需下载模型；真实模式通过
OpenAI-compatible Embedding API 生成向量。两者都可以配合 Local Store 或
本地持久化 Chroma 使用。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from .config import ConfigurationError, RuntimeSettings
from .paths import KNOWLEDGE_ROOT, RUNTIME_ROOT, ensure_runtime_dirs


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

    它并非生产级语义模型，而是用于保持 MVP 完全离线，并使测试中的 Chroma
    检索具有确定性。真实 Embedding Adapter 可以在 ``KnowledgeStore``
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
# 真实 Embedding：通过 OpenAI-compatible API 生成语义向量
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


def build_embedding_provider(settings: RuntimeSettings) -> EmbeddingProvider:
    if settings.embedding_backend == "mock":
        return HashEmbeddingProvider(
            dimensions=settings.embedding_dimensions or 256
        )
    if settings.embedding_backend == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider(settings)
    raise ConfigurationError(
        f"不支持的 Embedding backend：{settings.embedding_backend}"
    )


class KnowledgeStore(Protocol):
    def search(
        self, query: str, domains: list[str], as_of: str, top_k: int
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
        self, query: str, domains: list[str], as_of: str, top_k: int = 3
    ) -> list[KnowledgeHit]:
        query_vector = self.embedding_provider.embed_query(query)
        authority_boost = {"official_policy": 0.08, "approved_faq": 0.03}
        hits: list[KnowledgeHit] = []
        for document in self.documents:
            if document.domain not in domains or not _effective(document, as_of):
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


class ChromaKnowledgeStore(LocalKnowledgeStore):
    """使用调用方提供 Embedding 的持久化 Chroma 实现。

    Embedding 由调用方显式传入，因此不会触发模型下载。父类仍负责确切条款
    查询和生效日期校验，从而使 Chroma 仅作为索引，而不是事实来源。
    """

    def __init__(
        self,
        documents: list[PolicyDocument],
        path: Path,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        import chromadb  # 导入时为可选依赖，由 pyproject 安装。

        super().__init__(documents, embedding_provider)
        path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path))
        # 不同模型的向量维数通常不同，不能写入同一个 Chroma Collection。
        # 使用不含 API Key 的 Provider 身份创建隔离索引，切换模型时保留旧索引。
        identity_hash = hashlib.sha256(
            self.embedding_provider.identity.encode("utf-8")
        ).hexdigest()[:12]
        self.collection = self.client.get_or_create_collection(
            name=f"airline_mvp_policies_{identity_hash}",
            metadata={
                "description": "EchoMind Airline MVP policy chunks",
                "embedding_provider": self.embedding_provider.identity,
            },
        )
        self.collection.upsert(
            ids=[
                f"{doc.document_id}:{doc.version}:{doc.section}" for doc in documents
            ],
            documents=[doc.text for doc in documents],
            embeddings=[
                self._embeddings[(doc.document_id, doc.version, doc.section)]
                for doc in documents
            ],
            metadatas=[
                {
                    "document_id": doc.document_id,
                    "version": doc.version,
                    "section": doc.section,
                    "domain": doc.domain,
                    "authority": doc.authority,
                }
                for doc in documents
            ],
        )

    def search(
        self, query: str, domains: list[str], as_of: str, top_k: int = 3
    ) -> list[KnowledgeHit]:
        # 查询多于最终 top_k 的候选，使应用层能够在向量召回之后继续执行
        # 生效日期和权威等级规则。
        result = self.collection.query(
            query_embeddings=[self.embedding_provider.embed_query(query)],
            n_results=min(max(top_k * 3, 6), max(len(self.documents), 1)),
            where={"domain": {"$in": domains}},
            include=["metadatas", "distances"],
        )
        by_key = {
            (doc.document_id, doc.version, doc.section): doc for doc in self.documents
        }
        authority_boost = {"official_policy": 0.08, "approved_faq": 0.03}
        hits: list[KnowledgeHit] = []
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        for metadata, distance in zip(metadatas, distances, strict=False):
            key = (
                metadata["document_id"],
                metadata["version"],
                metadata["section"],
            )
            document = by_key.get(key)
            if document is None or not _effective(document, as_of):
                continue
            score = 1.0 - float(distance) + authority_boost.get(document.authority, 0.0)
            hits.append(KnowledgeHit(document=document, score=round(score, 6)))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]


class KnowledgeService:
    """供知识类 Tool 调用的统一门面。"""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def search(
        self, query: str, domains: list[str], as_of: str, top_k: int = 3
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
                "score": hit.score,
            }
            for hit in self.store.search(query, domains, as_of, top_k)
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
        }


def load_policy_documents(path: Path = KNOWLEDGE_ROOT / "policies.json") -> list[PolicyDocument]:
    with path.open("r", encoding="utf-8") as handle:
        return [PolicyDocument.from_fixture(item) for item in json.load(handle)]


def build_knowledge_service(
    *,
    prefer_chroma: bool = True,
    settings: RuntimeSettings | None = None,
    runtime_root: Path | None = None,
) -> KnowledgeService:
    """装配知识检索服务。

    ``prefer_chroma`` 保留给已有测试和调用方；传入 ``settings`` 后，以
    ``AIRLINE_MVP_KNOWLEDGE_BACKEND`` 为准。Chroma 是真实的本地持久化
    向量数据库，区别只在于向量来自 Mock Hash 还是真实 Embedding API。
    """

    documents = load_policy_documents()
    if settings is None:
        embedding_provider: EmbeddingProvider = HashEmbeddingProvider()
        use_chroma = prefer_chroma
        strict_backend = False
    else:
        embedding_provider = build_embedding_provider(settings)
        use_chroma = settings.knowledge_backend == "chroma"
        strict_backend = True

    if use_chroma:
        try:
            ensure_runtime_dirs()
            return KnowledgeService(
                ChromaKnowledgeStore(
                    documents,
                    (runtime_root or RUNTIME_ROOT) / "chroma",
                    embedding_provider,
                )
            )
        except (ImportError, RuntimeError, ValueError):
            if strict_backend:
                # 用户显式选择真实 Chroma 后不能悄悄切回内存，否则健康检查会
                # 给出误导结果。异常原样抛出，便于定位依赖或索引问题。
                raise
            # 即使处于最小 Python 环境，整个架构仍然可以运行。
            # 测试会通过回退实现显式验证相同的检索契约。
            pass
    return KnowledgeService(
        LocalKnowledgeStore(documents, embedding_provider)
    )
