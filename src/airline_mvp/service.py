"""应用组件装配入口，以及与框架无关的 Chat Service。

设计映射
--------
- 设计 §8：通过显式依赖注入保持 Runtime 可替换。
- 设计 §17：提供单一公开 invoke 边界，并使用 Case 级 Checkpoint Thread。
- 设计 §21：Fixture/本地模式是默认面试演示方式。

FastAPI 和 CLI 都调用 ``AirlineMVPService.chat``，因此无需启动网络服务，
也能对编排行为进行测试。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from .checkpointing import build_checkpointer
from .config import RuntimeSettings
from .fixtures import AirlineFixtureStore
from .knowledge import KnowledgeService, build_knowledge_service
from .model_gateway import ModelGateway, build_model_gateway
from .models import CaseStatus, ChatRequest, ChatResult, TokenUsage
from .parent_graph import ParentDependencies, build_parent_graph
from .paths import RUNTIME_ROOT, ensure_runtime_dirs
from .persistence import (
    CaseRepository,
    Database,
    HandoffRepository,
    PostgreSQLDatabase,
    SQLiteDatabase,
    TraceRepository,
)
from .quality import QualityGate
from .tools import ToolExecutor, build_tool_registry
from .worker_graph import WorkerDependencies


@dataclass
class AirlineMVPService:
    """提供 Chat、Case 和 Trace 查询能力的轻量服务门面。"""

    graph: Any
    cases: CaseRepository
    traces: TraceRepository
    checkpoint_backend: str
    knowledge_backend: str
    model_backend: str = "mock"
    database_backend: str = "sqlite"
    embedding_backend: str = "mock"
    airline_api_backend: str = "fixture"

    def chat(self, request: ChatRequest) -> ChatResult:
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        conversation_id = request.conversation_id or f"conv_{uuid.uuid4().hex[:16]}"
        case_id = f"case_{uuid.uuid4().hex[:16]}"
        trace_id = f"trace_{uuid.uuid4().hex[:16]}"

        self.cases.start_case(
            conversation_id=conversation_id,
            case_id=case_id,
            request_id=request_id,
            message=request.message,
            verified_subject_id=request.verified_subject_id,
            locale=request.locale,
        )
        initial_state = {
            "request_id": request_id,
            "trace_id": trace_id,
            "thread_id": case_id,
            "conversation_id": conversation_id,
            "case_id": case_id,
            "messages": [HumanMessage(content=request.message)],
            "current_message": request.message,
            "locale": request.locale,
            "verified_subject_id": request.verified_subject_id,
            "intents": [],
            "missing_fields": [],
            "risk_flags": [],
            "domain_tasks": [],
            "findings": [],
            "evidence": [],
            "tool_calls": [],
            "errors": [],
            "status": CaseStatus.NEW.value,
            "replan_count": 0,
            "revision_count": 0,
            "token_usage": TokenUsage(),
            "case_summary": "",
        }
        final_state = self.graph.invoke(
            initial_state,
            config={
                "configurable": {"thread_id": case_id},
                "recursion_limit": 50,
            },
        )
        return ChatResult(
            request_id=request_id,
            conversation_id=conversation_id,
            case_id=case_id,
            status=CaseStatus(final_state["status"]),
            response=final_state.get("service_response"),
            handoff=final_state.get("handoff_packet"),
        )

    def get_trace(self, case_id: str) -> list[dict[str, Any]]:
        return [
            event.model_dump(mode="json")
            for event in self.traces.list_for_case(case_id)
        ]

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        return self.cases.get_case(case_id)


def build_service(
    *,
    runtime_root: Path | None = None,
    prefer_chroma: bool | None = None,
    model: ModelGateway | None = None,
    knowledge: KnowledgeService | None = None,
    database: Database | None = None,
    settings: RuntimeSettings | None = None,
    forced_tool_statuses: dict[str, Any] | None = None,
) -> AirlineMVPService:
    """根据运行配置装配 Mock 或真实基础设施。

    默认配置仍是无需外部服务的离线模式。设置环境变量后，完全相同的 Graph
    会切换到真实 LLM、PostgreSQL 和真实 Embedding；航司业务 API 在本轮需求
    中始终保留 Fixture，不会连接任何真实旅客或订单系统。

    # 1. 准备运行目录 runtime
    # 2. 创建 SQLite 或 PostgreSQL 业务数据库和 Repository
    # 3. 创建模拟航司后台（本轮唯一固定为 Mock 的部分）
    # 4. 创建本地/Chroma RAG，并选择 Mock/真实 Embedding
    # 5. 注册只读工具
    # 6. 创建带权限检查的 Tool Executor
    # 7. 创建 Mock 或真实模型决策 Gateway
    # 8. 把 Worker 需要的依赖打包
    # 9. 创建 Memory/SQLite/PostgreSQL LangGraph Checkpointer
    # 10. 把 Parent Graph 需要的依赖打包
    # 11. 编译完整 LangGraph
    # 12. 返回统一 Service
在程序生命周期中，它一般只创建一次：
FastAPI 启动
→ build_service() 一次
→ 得到 service
→ 接收很多旅客请求
→ 重复调用 service.chat()

最值得先理解的是这四组关系：
KnowledgeService + Fixture
→ ToolRegistry
→ ToolExecutor

ModelGateway + ToolExecutor
→ DomainWorkerGraph

DomainWorkerGraph + Repository + QualityGate
→ ParentGraph

ParentGraph
→ AirlineMVPService
"""

    ensure_runtime_dirs()
    root = runtime_root or RUNTIME_ROOT
    root.mkdir(parents=True, exist_ok=True)

    resolved_settings = settings or RuntimeSettings.from_env()
    # ``prefer_chroma`` 是旧版测试入口。显式传入时仅覆盖知识库类型，不影响
    # LLM、数据库和 Embedding 配置；新代码推荐统一使用 RuntimeSettings。
    if prefer_chroma is not None:
        resolved_settings = replace(
            resolved_settings,
            knowledge_backend="chroma" if prefer_chroma else "local",
        )
        resolved_settings.validate()

    # -------------------- Mock/本地数据库 vs 真实 PostgreSQL --------------------
    if database is not None:
        application_database = database
    elif resolved_settings.database_backend == "postgres":
        application_database = PostgreSQLDatabase(
            resolved_settings.database_url or "",
            pool_size=resolved_settings.database_pool_size,
        )
    else:
        application_database = SQLiteDatabase(root / "airline_mvp.sqlite3")
    cases = CaseRepository(application_database)
    traces = TraceRepository(application_database)
    handoffs = HandoffRepository(application_database)

    # -------------------- 航司 API：按需求继续使用合成 Fixture --------------------
    fixtures = AirlineFixtureStore()

    # -------------------- Mock Hash/真实 Embedding + 本地/Chroma RAG --------------------
    knowledge_service = knowledge or build_knowledge_service(
        settings=resolved_settings,
        runtime_root=root,
    )
    registry = build_tool_registry(fixtures, knowledge_service)
    executor = ToolExecutor(
        registry,
        fixtures.dataset_version,
        forced_status_by_tool=forced_tool_statuses,
    )

    # -------------------- Mock 规则模型 vs 真实 OpenAI-compatible LLM --------------------
    gateway = model or build_model_gateway(resolved_settings)
    worker_dependencies = WorkerDependencies(
        model=gateway,
        executor=executor,
        registry=registry,
        traces=traces,
    )
    checkpointer, checkpoint_backend = build_checkpointer(
        root / "langgraph_checkpoints.sqlite3",
        backend=resolved_settings.checkpoint_backend,
        postgres_url=resolved_settings.resolved_checkpoint_database_url,
        pool_size=resolved_settings.database_pool_size,
    )
    dependencies = ParentDependencies(
        model=gateway,
        worker_dependencies=worker_dependencies,
        cases=cases,
        handoffs=handoffs,
        traces=traces,
        quality=QualityGate(),
    )
    graph = build_parent_graph(dependencies, checkpointer)
    return AirlineMVPService(
        graph=graph,
        cases=cases,
        traces=traces,
        checkpoint_backend=checkpoint_backend,
        knowledge_backend=type(knowledge_service.store).__name__,
        model_backend=(
            "custom"
            if model is not None
            else resolved_settings.llm_backend
        ),
        database_backend=application_database.backend_name,
        embedding_backend=resolved_settings.embedding_backend,
        airline_api_backend="fixture",
    )
