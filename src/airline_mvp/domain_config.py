"""业务域 Agent 定义。

设计映射
--------
- §4：按业务域粒度划分 Agent。
- §12：Journey、Refund 和 Baggage 的职责。
- §13：通过数据配置一个可复用的 Worker Graph。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .models import DomainName


class DomainAgentConfig(BaseModel):
    domain: DomainName
    role: str
    allowed_tools: list[str]
    knowledge_domains: list[str]
    required_evidence_types: list[str]
    max_tool_calls: int = Field(default=4, ge=1, le=6)
    timeout_ms: int = 10_000


DOMAIN_CONFIGS: dict[DomainName, DomainAgentConfig] = {
    DomainName.JOURNEY: DomainAgentConfig(
        domain=DomainName.JOURNEY,
        role="调查航班、PNR、航段、票证状态和航变政策",
        allowed_tools=[
            "get_flight_status",
            "get_booking",
            "get_ticket_status",
            "get_disruption_info",
            "search_airline_knowledge",
            "get_policy_clause",
        ],
        knowledge_domains=["journey", "disruption", "ticketing"],
        required_evidence_types=["flight", "booking", "ticket", "policy"],
        # 复杂 Demo 路径：航班 + PNR + 客票 + 航变 + RAG + 原始条款。
        # 设计 §14.3 仍将循环限制为最多六次调用。
        max_tool_calls=6,
    ),
    DomainName.REFUND: DomainAgentConfig(
        domain=DomainName.REFUND,
        role="调查退款申请、支付网关、退款阶段和退款时效政策",
        allowed_tools=[
            "get_payment_status",
            "get_refund_status",
            "search_airline_knowledge",
            "get_policy_clause",
        ],
        knowledge_domains=["refund", "payment"],
        required_evidence_types=["refund", "payment", "policy"],
        max_tool_calls=4,
    ),
    # 设计 §12.5：这里仅作为扩展示例注册。除非启用对应 Feature Flag，
    # 否则 MVP 路由不会选择该业务域。
    DomainName.BAGGAGE: DomainAgentConfig(
        domain=DomainName.BAGGAGE,
        role="调查行李工单、追踪状态和行李政策",
        allowed_tools=[
            "get_baggage_case",
            "get_baggage_tracking",
            "search_airline_knowledge",
            "get_policy_clause",
        ],
        knowledge_domains=["baggage"],
        required_evidence_types=["baggage", "policy"],
    ),
}


def get_domain_config(domain: DomainName) -> DomainAgentConfig:
    try:
        return DOMAIN_CONFIGS[domain]
    except KeyError as exc:
        raise ValueError(f"Unregistered domain: {domain}") from exc
