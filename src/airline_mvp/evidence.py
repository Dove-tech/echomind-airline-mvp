"""将标准化 Tool 结果转换为可定位原始来源的 EvidenceItem。

设计 §19 要求每个重要结论都能回溯到 Tool 快照或确切政策条款。
失败或不可用的调用不会转化为 Evidence。
"""

from __future__ import annotations

import hashlib
from typing import Any

from .models import EvidenceItem, ToolResult, ToolStatus
from .tools import ToolDefinition


def _evidence_id(tool_call_id: str, suffix: str) -> str:
    digest = hashlib.sha256(f"{tool_call_id}:{suffix}".encode("utf-8")).hexdigest()
    return f"ev_{digest[:18]}"


def _source_id(tool_name: str, data: dict[str, Any], index: int) -> str:
    for key in (
        "ticketRef",
        "pnrRef",
        "flightNo",
        "refundRef",
        "paymentRef",
        "documentId",
    ):
        if key in data:
            return f"{tool_name}:{data[key]}"
    return f"{tool_name}:result:{index}"


def _summary(tool_name: str, data: dict[str, Any]) -> str:
    """生成简短且适合进入 Evidence 的摘要，而不是直接倾倒原始 JSON。"""

    if tool_name == "get_flight_status":
        flight = data.get("flight", {})
        return (
            f"{flight.get('flightNo')} {flight.get('date')} 状态为 "
            f"{flight.get('status')}"
        )
    if tool_name == "get_disruption_info":
        disruption = data.get("disruption", {})
        return (
            f"航班异常类型为 {disruption.get('type')}，"
            f"原因分类为 {disruption.get('reasonCategory')}"
        )
    if tool_name == "get_booking":
        booking = data.get("booking", {})
        return (
            f"PNR {booking.get('pnrRef')} 关联 "
            f"{len(booking.get('ticketRefs', []))} 张客票"
        )
    if tool_name == "get_ticket_status":
        statuses = [
            f"{ticket.get('ticketRef')}={ticket.get('couponStatus')}"
            for ticket in data.get("tickets", [])
        ]
        return "客票状态：" + "，".join(statuses)
    if tool_name == "get_refund_status":
        refunds = data.get("refunds", [])
        if not refunds:
            return "未找到匹配的退款申请记录"
        return "退款状态：" + "，".join(
            f"{item.get('ticketRef')}={item.get('refundStatus')}/{item.get('stage')}"
            for item in refunds
        )
    if tool_name == "get_payment_status":
        payment = data.get("payment", {})
        return (
            f"支付状态 {payment.get('paymentStatus')}，"
            f"退款网关状态 {payment.get('refundGatewayStatus')}"
        )
    if tool_name == "search_airline_knowledge":
        return "检索到政策候选，必须继续下钻原始条款"
    if tool_name == "get_policy_clause":
        clause = data.get("clause", {})
        return f"{clause.get('title')} {clause.get('version')} §{clause.get('section')}"
    return f"{tool_name} 返回可用记录"


def tool_result_to_evidence(
    *,
    case_id: str,
    definition: ToolDefinition,
    result: ToolResult,
) -> list[EvidenceItem]:
    """根据成功的只读结果创建一个或多个 EvidenceItem。

    ``NOT_FOUND`` 会被记录为 Evidence，因为 System of Record 的未找到观察
    本身有业务意义；``TIMEOUT`` 和 ``UNAVAILABLE`` 只能形成信息缺口，
    不能形成 Evidence。
    """

    if result.status not in {
        ToolStatus.SUCCESS,
        ToolStatus.PARTIAL,
        ToolStatus.NOT_FOUND,
    }:
        return []

    data = result.data
    items: list[dict[str, Any]]
    if definition.name == "search_airline_knowledge":
        items = data.get("results", [])
    elif definition.name == "get_ticket_status":
        items = data.get("tickets", []) or [{}]
    elif definition.name == "get_refund_status":
        items = data.get("refunds", []) or [{}]
    elif definition.name == "get_flight_status":
        items = [data.get("flight", {})]
    elif definition.name == "get_disruption_info":
        items = [data.get("disruption", {})]
    elif definition.name == "get_booking":
        items = [data.get("booking", {})]
    elif definition.name == "get_payment_status":
        items = [data.get("payment", {})]
    elif definition.name == "get_policy_clause":
        items = [data.get("clause", {})]
    else:
        items = [data]

    evidence: list[EvidenceItem] = []
    for index, item in enumerate(items):
        raw_authority = item.get(
            "authority",
            data.get("clause", {}).get("authority"),
        )
        authority = (
            raw_authority
            if definition.name in {"search_airline_knowledge", "get_policy_clause"}
            and raw_authority
            in {"official_policy", "airline_official_web", "approved_faq"}
            else "approved_faq"
            if definition.name in {"search_airline_knowledge", "get_policy_clause"}
            else "system_of_record"
        )
        version = (
            item.get("version")
            or data.get("clause", {}).get("version")
            or result.source.dataset_version
        )
        valid_from = item.get("validFrom") or data.get("clause", {}).get("validFrom")
        valid_to = item.get("validTo") or data.get("clause", {}).get("validTo")
        structured = item if item else {"notFound": True}
        evidence.append(
            EvidenceItem(
                evidence_id=_evidence_id(result.audit.tool_call_id, str(index)),
                case_id=case_id,
                evidence_type=definition.evidence_type,
                source_type=result.source.system,
                source_id=_source_id(definition.name, structured, index),
                authority=authority,
                summary=_summary(definition.name, data),
                structured_data=structured,
                observed_at=result.source.snapshot_at,
                valid_from=valid_from,
                valid_to=valid_to,
                version=str(version),
                locator={
                    "toolCallId": result.audit.tool_call_id,
                    "recordIndex": index,
                    "sourceUrl": item.get("sourceUrl"),
                    "sourcePath": item.get("sourcePath"),
                    "contentSha256": item.get("contentSha256"),
                },
                confidence=1.0 if result.status != ToolStatus.PARTIAL else 0.8,
            )
        )
    return evidence
