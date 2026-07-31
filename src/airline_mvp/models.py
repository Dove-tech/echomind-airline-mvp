"""共享领域契约。

设计映射
--------
- 设计 §10：``AirlineMVPState`` 字段（TypedDict 定义在 state.py 中）。
- 设计 §11：CasePlan、DomainTask、DomainFinding、EvidenceItem、
  ServiceResponse 和 HandoffPacket。
- 设计 §15：Tool 契约与状态语义。
- 设计 §23：TraceEvent。

所有跨 Agent 通信都使用这些 Pydantic 契约。Agent 之间不会交换无约束的
自由文本对话，因此每个节点都可以回放，评测器也能检查完整运行轨迹。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """返回带时区的时间戳，供持久化和 Trace 事件使用。"""

    return datetime.now(timezone.utc)


class DomainName(StrEnum):
    """已注册的业务域，参见设计 §4 和 §12。"""

    JOURNEY = "journey"
    REFUND = "refund"
    BAGGAGE = "baggage"


class CaseStatus(StrEnum):
    """设计 §9 和 §20 定义的轻量 MVP Case 生命周期。"""

    NEW = "new"
    UNDERSTANDING = "understanding"
    WAITING_FOR_INFORMATION = "waiting_for_information"
    RESEARCHING = "researching"
    SYNTHESIZING = "synthesizing"
    RESPONDED = "responded"
    WAITING_FOR_HUMAN = "waiting_for_human"
    FAILED = "failed"


class ToolStatus(StrEnum):
    """设计 §15.3 定义的 Tool 结果语义。

    ``NOT_FOUND`` 是有效的业务观察；``TIMEOUT`` 和 ``UNAVAILABLE``
    表示系统尚未建立对应事实。
    """

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    PARTIAL = "partial"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    INVALID_INPUT = "invalid_input"


class AirlineEntities(BaseModel):
    """从当前对话中提取出的航空业务实体。"""

    flight_no: str | None = None
    travel_date: str | None = None
    pnr_ref: str | None = None
    order_ref: str | None = None
    ticket_refs: list[str] = Field(default_factory=list)
    refund_ref: str | None = None


class RequestUnderstanding(BaseModel):
    """Coordinator 在创建 CasePlan 之前产生的结构化理解结果。

    设计 §11.1 将理解与规划分开，使二者可以独立评测，避免把意图和实体指标
    隐藏在最终回答中。
    """

    user_goal: str
    intents: list[str]
    entities: AirlineEntities
    missing_fields: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    requested_write_action: bool = False


class DomainTask(BaseModel):
    """Coordinator 分派给单个领域 Worker 的有边界任务。"""

    task_id: str
    domain: DomainName
    objective: str
    entity_refs: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    max_tool_calls: int = Field(default=4, ge=1, le=6)


class CasePlan(BaseModel):
    """Coordinator 规划阶段的结构化输出。"""

    case_type: str
    user_goal: str
    intents: list[str]
    missing_fields: list[str] = Field(default_factory=list)
    tasks: list[DomainTask] = Field(default_factory=list)
    parallel: bool = False
    human_action_likely: bool = False

    @model_validator(mode="after")
    def enforce_mvp_scope(self) -> "CasePlan":
        """设计 §14.3：每个 MVP 请求最多涉及两个不同业务域。"""

        domains = [task.domain for task in self.tasks]
        if len(domains) != len(set(domains)):
            raise ValueError("A plan cannot dispatch the same domain twice")
        if len(domains) > 2:
            raise ValueError("The interview MVP allows at most two domains")
        return self


class ToolExecutionContext(BaseModel):
    """由服务端注入的上下文；这些值绝不能由 LLM 提供。"""

    request_id: str
    case_id: str
    invocation_id: str
    tool_call_id: str
    verified_subject_id: str | None = None
    allowed_record_refs: list[str] = Field(default_factory=list)
    deadline_at: datetime | None = None


class ToolSource(BaseModel):
    system: str
    dataset_version: str
    snapshot_at: datetime = Field(default_factory=utc_now)


class ToolAudit(BaseModel):
    tool_call_id: str
    duration_ms: float
    cache_hit: bool = False
    attempt: int = 1


class ToolResult(BaseModel):
    """供 Worker 和 Evidence Adapter 消费的标准化 Tool 结果。"""

    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    source: ToolSource
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    audit: ToolAudit


class ToolCallRecord(BaseModel):
    """可审计的 Tool 调用提议与执行记录。"""

    tool_call_id: str
    invocation_id: str
    task_id: str
    domain: DomainName
    tool_name: str
    arguments: dict[str, Any]
    status: ToolStatus
    started_at: datetime
    ended_at: datetime
    error_code: str | None = None


class EvidenceItem(BaseModel):
    """粒度为单个事实、能够定位原始来源的 Evidence 记录。"""

    evidence_id: str
    case_id: str
    evidence_type: str
    source_type: str
    source_id: str
    authority: Literal["system_of_record", "official_policy", "approved_faq"]
    summary: str
    structured_data: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utc_now)
    valid_from: str | None = None
    valid_to: str | None = None
    version: str
    locator: dict[str, Any]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class SupportedStatement(BaseModel):
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    uncertainty: str | None = None


class DomainFinding(BaseModel):
    """Worker 输出；不会直接展示给旅客。"""

    task_id: str
    domain: DomainName
    status: Literal["completed", "degraded", "failed"]
    facts: list[SupportedStatement] = Field(default_factory=list)
    inferences: list[SupportedStatement] = Field(default_factory=list)
    policy_conclusions: list[SupportedStatement] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    human_action_needed: bool = False


class AvailableOption(BaseModel):
    option: str
    execution_status: Literal["not_executed"] = "not_executed"
    evidence_ids: list[str] = Field(default_factory=list)


class ServiceResponse(BaseModel):
    """设计 §11.5 定义的旅客可见结构化回答。"""

    response_status: Literal[
        "answered", "needs_clarification", "handoff_required", "degraded"
    ]
    answer: str
    verified_facts: list[SupportedStatement] = Field(default_factory=list)
    available_options: list[AvailableOption] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    handoff_required: bool = False
    handoff_reason: str | None = None
    must_not_claim: list[str] = Field(default_factory=list)


class HandoffPacket(BaseModel):
    """设计 §11.6 和 §20 定义的内部确定性人工接管提议。"""

    case_id: str
    reason_code: str
    target_queue: str
    priority: Literal["low", "normal", "high"] = "normal"
    customer_request: str
    verified_fact_refs: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    conversation_cursor: str | None = None
    status: Literal["proposed", "queued", "failed"] = "proposed"
    handoff_id: str | None = None


class QualityReport(BaseModel):
    """设计 §19.3 定义的确定性质检结果。"""

    decision: Literal["pass", "revise", "handoff", "block"]
    violations: list[str] = Field(default_factory=list)
    invalid_evidence_ids: list[str] = Field(default_factory=list)
    prohibited_phrases: list[str] = Field(default_factory=list)
    handoff_required: bool = False


class GraphError(BaseModel):
    code: str
    message: str
    node: str
    retryable: bool = False


class TraceEvent(BaseModel):
    """request→agent→tool→evidence 链路中的一个有序事件。"""

    model_config = ConfigDict(use_enum_values=True)

    event_id: str
    trace_id: str
    case_id: str
    event_type: str
    sequence_no: int
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class DomainDecision(BaseModel):
    """通用 Tool 循环使用的 Model Gateway 决策。

    真实 LLM 可以通过结构化输出填写该契约；离线 Gateway 会确定性地产生
    相同格式的结果。
    """

    action: Literal["call_tool", "finish"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str


class ChatRequest(BaseModel):
    """对外服务输入。"""

    message: str = Field(min_length=1, max_length=4_000)
    conversation_id: str | None = None
    verified_subject_id: str | None = "subject_demo"
    locale: str = "zh-CN"


class ChatResult(BaseModel):
    """与 FastAPI 无关的对外服务输出。"""

    request_id: str
    conversation_id: str
    case_id: str
    status: CaseStatus
    response: ServiceResponse | None = None
    clarification: dict[str, Any] | None = None
    handoff: HandoffPacket | None = None
