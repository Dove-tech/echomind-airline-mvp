"""运行时配置：统一管理 Mock 与真实基础设施的切换。

这个模块只读取环境变量，不会修改系统环境、项目配置或数据库内容。
项目默认保持完全离线：

- 大模型：``mock``，使用确定性规则网关；
- 业务数据库：``sqlite``，使用工程内嵌入式数据库；
- LangGraph Checkpoint：``sqlite``；
- Embedding：``mock``，使用确定性 Hash Embedding；
- 知识库：``chroma``，使用本地持久化 Chroma。

需要连接真实服务时，只需复制 ``.env.example`` 为 ``.env`` 并切换对应
backend。显式选择真实后端后，如果必要配置缺失，应用会快速失败并给出
中文错误，避免用户误以为已经调用真实服务。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .paths import PROJECT_ROOT


class ConfigurationError(ValueError):
    """运行时配置缺失或组合不合法。"""


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _integer(name: str, default: int) -> int:
    raw = _optional(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数，当前值为：{raw!r}") from exc


def _optional_integer(name: str) -> int | None:
    raw = _optional(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数，当前值为：{raw!r}") from exc


def _floating(name: str, default: float) -> float:
    raw = _optional(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是数字，当前值为：{raw!r}") from exc


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = (_optional(name) or default).lower()
    if value not in allowed:
        choices = "、".join(sorted(allowed))
        raise ConfigurationError(f"{name} 只能是 {choices}，当前值为：{value!r}")
    return value


def _require(value: str | None, name: str, backend_name: str) -> str:
    if value:
        return value
    raise ConfigurationError(
        f"已启用真实后端 {backend_name}，但 {name} 为空。"
        "请复制 .env.example 为 .env 后填写该配置。"
    )


@dataclass(frozen=True)
class RuntimeSettings:
    """经过校验的应用运行配置。

    字段按基础设施分组，方便在面试中解释“业务编排协议不变，Adapter 可替换”。
    API Key 只保存在内存中，健康检查和 Trace 都不会输出它。
    """

    # -------------------- Mock/真实大模型切换 --------------------
    llm_backend: str = "mock"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_temperature: float = 0.0
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2

    # -------------------- SQLite/真实 PostgreSQL 切换 --------------------
    database_backend: str = "sqlite"
    database_url: str | None = None
    database_pool_size: int = 5

    # -------------------- LangGraph Checkpoint 后端 --------------------
    checkpoint_backend: str = "sqlite"
    checkpoint_database_url: str | None = None

    # -------------------- Mock/真实 Embedding 切换 --------------------
    embedding_backend: str = "mock"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None

    # ``local`` 是纯内存检索；``chroma`` 是真实的本地持久化向量库。
    knowledge_backend: str = "chroma"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "RuntimeSettings":
        """从 ``.env`` 和系统环境变量加载配置。

        ``override=False`` 保证 PowerShell、PyCharm 或容器显式注入的环境变量
        优先于文件内容。
        """

        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        settings = cls(
            llm_backend=_choice(
                "AIRLINE_MVP_LLM_BACKEND",
                "mock",
                {"mock", "openai_compatible"},
            ),
            llm_base_url=_optional("AIRLINE_MVP_LLM_BASE_URL"),
            llm_api_key=_optional("AIRLINE_MVP_LLM_API_KEY"),
            llm_model=_optional("AIRLINE_MVP_LLM_MODEL"),
            llm_temperature=_floating("AIRLINE_MVP_LLM_TEMPERATURE", 0.0),
            llm_timeout_seconds=_integer("AIRLINE_MVP_LLM_TIMEOUT_SECONDS", 60),
            llm_max_retries=_integer("AIRLINE_MVP_LLM_MAX_RETRIES", 2),
            database_backend=_choice(
                "AIRLINE_MVP_DATABASE_BACKEND",
                "sqlite",
                {"sqlite", "postgres"},
            ),
            database_url=_optional("AIRLINE_MVP_DATABASE_URL"),
            database_pool_size=_integer("AIRLINE_MVP_DATABASE_POOL_SIZE", 5),
            checkpoint_backend=_choice(
                "AIRLINE_MVP_CHECKPOINT_BACKEND",
                "sqlite",
                {"memory", "sqlite", "postgres"},
            ),
            checkpoint_database_url=_optional(
                "AIRLINE_MVP_CHECKPOINT_DATABASE_URL"
            ),
            embedding_backend=_choice(
                "AIRLINE_MVP_EMBEDDING_BACKEND",
                "mock",
                {"mock", "openai_compatible"},
            ),
            embedding_base_url=_optional("AIRLINE_MVP_EMBEDDING_BASE_URL"),
            embedding_api_key=_optional("AIRLINE_MVP_EMBEDDING_API_KEY"),
            embedding_model=_optional("AIRLINE_MVP_EMBEDDING_MODEL"),
            embedding_dimensions=_optional_integer(
                "AIRLINE_MVP_EMBEDDING_DIMENSIONS"
            ),
            knowledge_backend=_choice(
                "AIRLINE_MVP_KNOWLEDGE_BACKEND",
                "chroma",
                {"local", "chroma"},
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """仅校验被显式启用的真实后端。"""

        if self.llm_backend == "openai_compatible":
            _require(
                self.llm_base_url,
                "AIRLINE_MVP_LLM_BASE_URL",
                "openai_compatible LLM",
            )
            _require(
                self.llm_api_key,
                "AIRLINE_MVP_LLM_API_KEY",
                "openai_compatible LLM",
            )
            _require(
                self.llm_model,
                "AIRLINE_MVP_LLM_MODEL",
                "openai_compatible LLM",
            )

        if self.database_backend == "postgres":
            url = _require(
                self.database_url,
                "AIRLINE_MVP_DATABASE_URL",
                "PostgreSQL 业务数据库",
            )
            if not url.startswith(("postgresql://", "postgres://")):
                raise ConfigurationError(
                    "AIRLINE_MVP_DATABASE_URL 必须以 postgresql:// 或 postgres:// 开头"
                )

        if self.checkpoint_backend == "postgres":
            checkpoint_url = (
                self.checkpoint_database_url or self.database_url
            )
            _require(
                checkpoint_url,
                "AIRLINE_MVP_CHECKPOINT_DATABASE_URL（或 AIRLINE_MVP_DATABASE_URL）",
                "PostgreSQL Checkpoint",
            )

        if self.embedding_backend == "openai_compatible":
            _require(
                self.embedding_base_url,
                "AIRLINE_MVP_EMBEDDING_BASE_URL",
                "openai_compatible Embedding",
            )
            _require(
                self.embedding_api_key,
                "AIRLINE_MVP_EMBEDDING_API_KEY",
                "openai_compatible Embedding",
            )
            _require(
                self.embedding_model,
                "AIRLINE_MVP_EMBEDDING_MODEL",
                "openai_compatible Embedding",
            )

        if self.llm_timeout_seconds <= 0:
            raise ConfigurationError("AIRLINE_MVP_LLM_TIMEOUT_SECONDS 必须大于 0")
        if self.llm_max_retries < 0:
            raise ConfigurationError("AIRLINE_MVP_LLM_MAX_RETRIES 不能小于 0")
        if self.database_pool_size <= 0:
            raise ConfigurationError("AIRLINE_MVP_DATABASE_POOL_SIZE 必须大于 0")
        if self.embedding_dimensions is not None and self.embedding_dimensions <= 0:
            raise ConfigurationError("AIRLINE_MVP_EMBEDDING_DIMENSIONS 必须大于 0")

    @property
    def resolved_checkpoint_database_url(self) -> str | None:
        """Checkpoint 可以复用业务 PostgreSQL，也可以连接独立数据库。"""

        return self.checkpoint_database_url or self.database_url
