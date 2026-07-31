"""EchoMind Airline Care 面试级 MVP。

该包有意控制规模，同时保留完整控制闭环：LangGraph 编排、领域 Agent、
只读 Tool、RAG、Evidence、Checkpoint、人工接管、Trace 和轨迹评测。

设计文档：``docs/DESIGN.md``。
"""

from .service import AirlineMVPService, build_service

__all__ = ["AirlineMVPService", "build_service"]
