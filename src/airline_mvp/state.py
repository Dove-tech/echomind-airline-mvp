"""LangGraph State 与 Reducer 定义。

设计 §10 要求：所有可能被并行领域 Worker 写入的字段都必须配置 Reducer；
标量输出字段仍由 Parent Graph 独占写入。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage

from .models import (
    AirlineEntities,
    CasePlan,
    DomainFinding,
    DomainTask,
    EvidenceItem,
    GraphError,
    HandoffPacket,
    QualityReport,
    ServiceResponse,
    TokenUsage,
    ToolCallRecord,
)


def merge_findings(
    current: list[DomainFinding], incoming: list[DomainFinding]
) -> list[DomainFinding]:
    """按 task id 合并重试结果，同时保持稳定的任务顺序。

    设计参考：§10.1。重试结果会替换同一任务之前的 Finding，避免一个任务
    同时产生两份相互矛盾的结果。
    """

    merged = {item.task_id: item for item in current}
    for item in incoming:
        merged[item.task_id] = item
    return list(merged.values())


def merge_evidence(
    current: list[EvidenceItem], incoming: list[EvidenceItem]
) -> list[EvidenceItem]:
    """使用全局唯一的 evidence id 对 Evidence 去重。"""

    merged = {item.evidence_id: item for item in current}
    for item in incoming:
        merged[item.evidence_id] = item
    return list(merged.values())


class AirlineMVPState(TypedDict, total=False):
    """设计 §10 定义的 Parent Graph State。"""

    request_id: str
    trace_id: str
    thread_id: str
    conversation_id: str
    case_id: str

    messages: Annotated[list[BaseMessage], add_messages]
    current_message: str
    locale: str
    verified_subject_id: str | None

    intents: list[str]
    entities: AirlineEntities
    missing_fields: list[str]
    risk_flags: list[str]
    user_goal: str

    plan: CasePlan | None
    domain_tasks: Annotated[list[DomainTask], operator.add]
    # 由 ``Send`` 写入的临时分支局部字段。Worker 节点不会将其写回，
    # 因此并行分支不会竞争同一个标量值。
    active_task: DomainTask | None
    findings: Annotated[list[DomainFinding], merge_findings]
    evidence: Annotated[list[EvidenceItem], merge_evidence]
    tool_calls: Annotated[list[ToolCallRecord], operator.add]

    service_response: ServiceResponse | None
    handoff_packet: HandoffPacket | None
    handoff_id: str | None
    quality_report: QualityReport | None

    status: str
    replan_count: int
    revision_count: int
    token_usage: TokenUsage
    errors: Annotated[list[GraphError], operator.add]
    case_summary: str


class DomainWorkerState(TypedDict, total=False):
    """可复用 DomainWorkerGraph 的私有 State，参见设计 §13。"""

    task: DomainTask
    entities: AirlineEntities
    case_id: str
    request_id: str
    trace_id: str
    conversation_id: str
    verified_subject_id: str | None

    invocation_id: str
    evidence: Annotated[list[EvidenceItem], merge_evidence]
    tool_calls: Annotated[list[ToolCallRecord], operator.add]
    called_signatures: Annotated[list[str], operator.add]
    finding: DomainFinding | None
    next_decision: Any
    errors: Annotated[list[GraphError], operator.add]
