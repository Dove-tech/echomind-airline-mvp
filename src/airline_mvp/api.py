"""FastAPI 交付层 Adapter。

设计映射：设计 §17（API 边界）和 §23（Trace 查询）。

API 层不包含 Agent 逻辑。它只校验传输输入，并委托给 CLI 和测试共用的
``AirlineMVPService``，从而保证无需 Web Server 也能回放编排流程。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from .models import ChatRequest, ChatResult
from .service import AirlineMVPService, build_service


@lru_cache(maxsize=1)
def get_service() -> AirlineMVPService:
    return build_service()


def create_app(service: AirlineMVPService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        current = service
        if current is not None:
            current.close()
        elif get_service.cache_info().currsize:
            get_service().close()
            get_service.cache_clear()

    app = FastAPI(
        title="EchoMind Airline Care MVP",
        version="0.1.0",
        description=(
            "Customer-facing, read-only airline service-recovery multi-agent MVP"
        ),
        lifespan=lifespan,
    )

    def dependency() -> AirlineMVPService:
        return service or get_service()

    @app.get("/health")
    def health(
        current: AirlineMVPService = Depends(dependency),
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            "modelBackend": current.model_backend,
            "databaseBackend": current.database_backend,
            "checkpointBackend": current.checkpoint_backend,
            "knowledgeBackend": current.knowledge_backend,
            "embeddingBackend": current.embedding_backend,
            "rag": current.knowledge_health,
            "airlineApiBackend": current.airline_api_backend,
            "writeBusinessToolsEnabled": False,
        }

    @app.post("/v1/chat", response_model=ChatResult)
    def chat(
        request: ChatRequest,
        current: AirlineMVPService = Depends(dependency),
    ) -> ChatResult:
        return current.chat(request)

    @app.get("/v1/cases/{case_id}")
    def get_case(
        case_id: str,
        current: AirlineMVPService = Depends(dependency),
    ) -> dict[str, Any]:
        case = current.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        return case

    @app.get("/v1/cases/{case_id}/trace")
    def get_trace(
        case_id: str,
        current: AirlineMVPService = Depends(dependency),
    ) -> list[dict[str, Any]]:
        if current.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return current.get_trace(case_id)

    return app


app = create_app()


def run() -> None:
    """控制台入口；只有用户显式调用时才会启动服务。"""

    import os

    import uvicorn

    uvicorn.run(
        "airline_mvp.api:app",
        host=os.getenv("AIRLINE_MVP_API_HOST", "127.0.0.1"),
        port=int(os.getenv("AIRLINE_MVP_API_PORT", "8000")),
        reload=False,
    )
