"""只读 Tool 注册、权限、执行与审计。

设计映射
--------
- §15：ToolDefinition、ToolContext、标准化状态、权限检查、
  只读超时重试一次，以及 Fixture Adapter。
- §25：敏感业务记录授权。

模型可以提出调用建议，但该执行器才是最终权限裁决方。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .domain_config import get_domain_config
from .fixtures import AirlineFixtureStore, FixtureAuthorizationError
from .knowledge import KnowledgeService
from .models import (
    DomainName,
    ToolAudit,
    ToolExecutionContext,
    ToolResult,
    ToolSource,
    ToolStatus,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictToolInput(BaseModel):
    """所有业务 Function 的严格输入基类。

    ``extra="forbid"`` 会进入 Function Calling JSON Schema，并在服务端再次
    生效。因此模型不能把 ``tool``、``tool_name`` 或 ``parameters`` 等包装字段
    混入真正的业务参数。
    """

    model_config = ConfigDict(extra="forbid")


class FlightInput(StrictToolInput):
    flight_no: str = Field(description="航班号，例如 CZ3101")
    date: str = Field(description="航班日期，格式为 YYYY-MM-DD")

    @field_validator("flight_no")
    @classmethod
    def normalize_flight(cls, value: str) -> str:
        return value.upper().replace(" ", "")


class BookingInput(StrictToolInput):
    pnr_ref: str = Field(description="已经从旅客消息中提取出的 6-8 位 PNR")

    @field_validator("pnr_ref")
    @classmethod
    def normalize_pnr(cls, value: str) -> str:
        return value.upper()


class TicketInput(StrictToolInput):
    ticket_refs: list[str] = Field(
        min_length=1,
        description="已经从 PNR 或旅客消息中获得的客票号列表",
    )


class RefundInput(StrictToolInput):
    pnr_ref: str | None = Field(default=None, description="可选 PNR")
    ticket_ref: str | None = Field(default=None, description="可选客票号")
    refund_ref: str | None = Field(default=None, description="可选退款单号")

    @model_validator(mode="after")
    def require_reference(self) -> "RefundInput":
        if not any((self.pnr_ref, self.ticket_ref, self.refund_ref)):
            raise ValueError("One refund lookup reference is required")
        return self


class PaymentInput(StrictToolInput):
    pnr_ref: str | None = Field(default=None, description="可选 PNR")
    order_ref: str | None = Field(default=None, description="可选订单号")

    @model_validator(mode="after")
    def require_reference(self) -> "PaymentInput":
        if not self.pnr_ref and not self.order_ref:
            raise ValueError("pnr_ref or order_ref is required")
        return self


class KnowledgeSearchInput(BaseModel):
    query: str = Field(min_length=2)
    domains: list[str] = Field(min_length=1)
    as_of: str
    top_k: int = Field(default=3, ge=1, le=6)


class PolicyClauseInput(BaseModel):
    document_id: str
    version: str
    section: str


ToolHandler = Callable[[BaseModel, ToolExecutionContext], tuple[ToolStatus, dict[str, Any], list[str]]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    risk: Literal["public_read", "sensitive_read"]
    allowed_domains: frozenset[DomainName]
    evidence_type: str
    handler: ToolHandler
    source_system: str = "airline_fixture"
    retry_once: bool = True

    def as_function_call_schema(self) -> dict[str, Any]:
        """生成 OpenAI/LangChain 通用 Function Calling Schema。

        Schema 直接来自 ToolExecutor 最终使用的 Pydantic ``input_model``，
        避免 Prompt 提示和运行时校验规则各维护一份后逐渐漂移。
        """

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"Tool already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def list_for_domain(self, domain: DomainName) -> list[ToolDefinition]:
        return [
            definition
            for definition in self._definitions.values()
            if domain in definition.allowed_domains
        ]

    def function_call_schemas(
        self,
        *,
        domain: DomainName,
        allowed_tools: list[str],
    ) -> list[dict[str, Any]]:
        """只导出当前 Domain 被授权看到的 Function Schema。

        ``allowed_tools`` 来自服务端 CasePlan，不接受模型修改。注册缺失、领域
        越权等问题属于应用配置错误，因此在调用模型前直接失败，绝不把越权
        Function 暴露给模型后再补救。
        """

        schemas: list[dict[str, Any]] = []
        for name in allowed_tools:
            definition = self.get(name)
            if definition is None:
                raise ValueError(f"计划引用了未注册工具：{name}")
            if domain not in definition.allowed_domains:
                raise ValueError(f"工具 {name} 不允许由 {domain.value} Agent 使用")
            schemas.append(definition.as_function_call_schema())
        return schemas


class ToolExecutor:
    """负责强制执行策略的 Tool Executor。

    ``forced_status_by_tool`` 用于确定性失败 Demo 和评测；
    生产 Adapter 应根据真实集成结果报告这些状态。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        dataset_version: str,
        forced_status_by_tool: dict[str, ToolStatus] | None = None,
    ) -> None:
        self.registry = registry
        self.dataset_version = dataset_version
        self.forced_status_by_tool = forced_status_by_tool or {}

    def canonicalize_arguments(
        self,
        *,
        domain: DomainName,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """把模型提议收敛成服务端可信的上下文参数。

        Function Calling Schema 能约束字段形状，但模型仍可能自创知识域名称。
        ``domains`` 属于 Agent 权限配置，不属于模型自由输入，因此始终由服务端
        覆盖。承运人代码缺失时，只从 Query 中已经出现的航班号提取，不猜测。
        """

        canonical = dict(arguments)
        if tool_name != "search_airline_knowledge":
            return canonical

        canonical["domains"] = list(get_domain_config(domain).knowledge_domains)
        carrier_codes = [
            str(code).upper()
            for code in canonical.get("carrier_codes", [])
            if re.fullmatch(r"[A-Za-z0-9]{2,3}", str(code))
        ]
        if not carrier_codes:
            flight = re.search(
                r"(?<![A-Z0-9])([A-Z]{2})\s?\d{3,4}(?![A-Z0-9])",
                str(canonical.get("query", "")).upper(),
            )
            if flight:
                carrier_codes = [flight.group(1)]
        canonical["carrier_codes"] = carrier_codes
        return canonical

    def execute(
        self,
        *,
        domain: DomainName,
        allowed_tools: list[str],
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        started = time.perf_counter()
        arguments = self.canonicalize_arguments(
            domain=domain,
            tool_name=tool_name,
            arguments=arguments,
        )
        definition = self.registry.get(tool_name)

        # 设计 §15.5：在模型绑定阶段和此处分别进行一次校验。
        if (
            definition is None
            or tool_name not in allowed_tools
            or domain not in definition.allowed_domains
        ):
            return self._result(
                context=context,
                started=started,
                status=ToolStatus.DENIED,
                error_code="TOOL_NOT_ALLOWED",
                error_message=f"{domain} cannot call {tool_name}",
            )

        if definition.risk == "sensitive_read" and not context.verified_subject_id:
            return self._result(
                context=context,
                started=started,
                status=ToolStatus.DENIED,
                error_code="SUBJECT_NOT_VERIFIED",
                error_message="Sensitive airline data requires a verified subject",
            )

        try:
            validated = definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            return self._result(
                context=context,
                started=started,
                status=ToolStatus.INVALID_INPUT,
                error_code="INPUT_SCHEMA_INVALID",
                error_message=str(exc),
            )

        attempts = 2 if definition.retry_once else 1
        for attempt in range(1, attempts + 1):
            forced = self.forced_status_by_tool.get(tool_name)
            if forced is not None:
                status, data, warnings = forced, {}, [f"forced {forced.value} for demo"]
            else:
                try:
                    status, data, warnings = definition.handler(validated, context)
                except FixtureAuthorizationError as exc:
                    status, data, warnings = ToolStatus.DENIED, {}, []
                    return self._result(
                        context=context,
                        started=started,
                        status=status,
                        error_code="RECORD_SCOPE_DENIED",
                        error_message=str(exc),
                        attempt=attempt,
                    )
                except Exception as exc:  # Adapter 边界：绝不向 LLM 泄露原始异常。
                    status, data, warnings = ToolStatus.UNAVAILABLE, {}, []
                    return self._result(
                        context=context,
                        started=started,
                        status=status,
                        error_code="ADAPTER_ERROR",
                        error_message=type(exc).__name__,
                        attempt=attempt,
                    )

            if status != ToolStatus.TIMEOUT or attempt == attempts:
                return self._result(
                    context=context,
                    started=started,
                    status=status,
                    data=data,
                    warnings=warnings,
                    attempt=attempt,
                    system=f"fixture_{tool_name}",
                )

        raise AssertionError("Tool retry loop did not return")

    def _result(
        self,
        *,
        context: ToolExecutionContext,
        started: float,
        status: ToolStatus,
        data: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        attempt: int = 1,
        system: str = "tool_registry",
    ) -> ToolResult:
        return ToolResult(
            status=status,
            data=data or {},
            source=ToolSource(
                system=system,
                dataset_version=self.dataset_version,
            ),
            warnings=warnings or [],
            error_code=error_code,
            error_message=error_message,
            audit=ToolAudit(
                tool_call_id=context.tool_call_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                attempt=attempt,
            ),
        )


def build_tool_registry(
    fixtures: AirlineFixtureStore, knowledge: KnowledgeService
) -> ToolRegistry:
    registry = ToolRegistry()

    def get_flight_status(
        data: BaseModel, _context: ToolExecutionContext
    ) -> tuple[ToolStatus, dict[str, Any], list[str]]:
        args = FlightInput.model_validate(data)
        record = fixtures.get_flight(args.flight_no, args.date)
        return (
            (ToolStatus.SUCCESS, {"flight": record}, [])
            if record
            else (ToolStatus.NOT_FOUND, {}, [])
        )

    def get_disruption_info(
        data: BaseModel, _context: ToolExecutionContext
    ) -> tuple[ToolStatus, dict[str, Any], list[str]]:
        args = FlightInput.model_validate(data)
        record = fixtures.get_flight(args.flight_no, args.date)
        if record is None:
            return ToolStatus.NOT_FOUND, {}, []
        return ToolStatus.SUCCESS, {
            "disruption": {
                "flightNo": record["flightNo"],
                "date": record["date"],
                "type": record["disruptionType"],
                "reasonCategory": record["reasonCategory"],
            }
        }, []

    def get_booking(
        data: BaseModel, context: ToolExecutionContext
    ) -> tuple[ToolStatus, dict[str, Any], list[str]]:
        args = BookingInput.model_validate(data)
        record = fixtures.get_booking(args.pnr_ref, context.verified_subject_id)
        return (
            (ToolStatus.SUCCESS, {"booking": record}, [])
            if record
            else (ToolStatus.NOT_FOUND, {}, [])
        )

    def get_ticket_status(
        data: BaseModel, context: ToolExecutionContext
    ) -> tuple[ToolStatus, dict[str, Any], list[str]]:
        args = TicketInput.model_validate(data)
        records = fixtures.get_tickets(args.ticket_refs, context.verified_subject_id)
        status = (
            ToolStatus.SUCCESS
            if len(records) == len(args.ticket_refs)
            else ToolStatus.PARTIAL
            if records
            else ToolStatus.NOT_FOUND
        )
        return status, {"tickets": records}, []

    def get_refund_status(
        data: BaseModel, context: ToolExecutionContext
    ) -> tuple[ToolStatus, dict[str, Any], list[str]]:
        args = RefundInput.model_validate(data)
        records = fixtures.get_refunds(
            subject_id=context.verified_subject_id,
            pnr_ref=args.pnr_ref,
            ticket_ref=args.ticket_ref,
            refund_ref=args.refund_ref,
        )
        return (
            (ToolStatus.SUCCESS, {"refunds": records}, [])
            if records
            else (ToolStatus.NOT_FOUND, {"refunds": []}, [])
        )

    def get_payment_status(
        data: BaseModel, context: ToolExecutionContext
    ) -> tuple[ToolStatus, dict[str, Any], list[str]]:
        args = PaymentInput.model_validate(data)
        record = fixtures.get_payment(
            subject_id=context.verified_subject_id,
            pnr_ref=args.pnr_ref,
            order_ref=args.order_ref,
        )
        return (
            (ToolStatus.SUCCESS, {"payment": record}, [])
            if record
            else (ToolStatus.NOT_FOUND, {}, [])
        )

    def search_knowledge(
        data: BaseModel, _context: ToolExecutionContext
    ) -> tuple[ToolStatus, dict[str, Any], list[str]]:
        args = KnowledgeSearchInput.model_validate(data)
        hits = knowledge.search(args.query, args.domains, args.as_of, args.top_k)
        return (
            (ToolStatus.SUCCESS, {"results": hits}, [])
            if hits
            else (ToolStatus.NOT_FOUND, {"results": []}, [])
        )

    def get_clause(
        data: BaseModel, _context: ToolExecutionContext
    ) -> tuple[ToolStatus, dict[str, Any], list[str]]:
        args = PolicyClauseInput.model_validate(data)
        clause = knowledge.get_clause(args.document_id, args.version, args.section)
        return (
            (ToolStatus.SUCCESS, {"clause": clause}, [])
            if clause
            else (ToolStatus.NOT_FOUND, {}, [])
        )

    all_domains = frozenset(
        {DomainName.JOURNEY, DomainName.REFUND, DomainName.BAGGAGE}
    )
    definitions = [
        ToolDefinition(
            "get_flight_status",
            "查询指定日期航班的计划与实际状态",
            FlightInput,
            "public_read",
            frozenset({DomainName.JOURNEY}),
            "flight",
            get_flight_status,
        ),
        ToolDefinition(
            "get_booking",
            "查询已验证旅客的PNR、航段和票号引用",
            BookingInput,
            "sensitive_read",
            frozenset({DomainName.JOURNEY}),
            "booking",
            get_booking,
        ),
        ToolDefinition(
            "get_ticket_status",
            "查询已验证旅客的电子客票票联状态",
            TicketInput,
            "sensitive_read",
            frozenset({DomainName.JOURNEY}),
            "ticket",
            get_ticket_status,
        ),
        ToolDefinition(
            "get_disruption_info",
            "查询航班异常类型和原因分类",
            FlightInput,
            "public_read",
            frozenset({DomainName.JOURNEY}),
            "flight",
            get_disruption_info,
        ),
        ToolDefinition(
            "get_payment_status",
            "查询已验证订单的支付和退款网关状态",
            PaymentInput,
            "sensitive_read",
            frozenset({DomainName.REFUND}),
            "payment",
            get_payment_status,
        ),
        ToolDefinition(
            "get_refund_status",
            "查询已验证客票或订单的退款处理阶段",
            RefundInput,
            "sensitive_read",
            frozenset({DomainName.REFUND}),
            "refund",
            get_refund_status,
        ),
        ToolDefinition(
            "search_airline_knowledge",
            "按业务域和生效日期检索航司政策候选",
            KnowledgeSearchInput,
            "public_read",
            all_domains,
            "policy_candidate",
            search_knowledge,
        ),
        ToolDefinition(
            "get_policy_clause",
            "下钻到指定版本和章节的政策原文",
            PolicyClauseInput,
            "public_read",
            all_domains,
            "policy",
            get_clause,
        ),
    ]
    for definition in definitions:
        registry.register(definition)
    return registry


def new_tool_call_id() -> str:
    return f"tc_{uuid.uuid4().hex[:16]}"
