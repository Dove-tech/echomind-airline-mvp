"""设计 §9–§24 对应的 Graph 端到端测试。"""

from airline_mvp.models import ChatRequest, ToolStatus
from airline_mvp.service import build_service


COMPLEX_MESSAGE = (
    "CZ3101 航班 2026-07-29 取消，PNR AB12CD。"
    "一张票退款未到账，另一张请帮我退票。"
)


def _tool_events(service, case_id):
    return [
        event["payload"]
        for event in service.get_trace(case_id)
        if event["event_type"] == "tool.completed"
    ]


def test_complex_case_runs_two_workers_and_all_eight_read_tools(tmp_path) -> None:
    service = build_service(runtime_root=tmp_path, prefer_chroma=False)
    result = service.chat(ChatRequest(message=COMPLEX_MESSAGE))

    assert result.status.value == "waiting_for_human"
    assert result.response.handoff_required is True
    assert result.handoff.status == "queued"
    tools = {event["toolName"] for event in _tool_events(service, result.case_id)}
    assert {
        "get_flight_status",
        "get_booking",
        "get_ticket_status",
        "get_disruption_info",
        "get_payment_status",
        "get_refund_status",
        "search_airline_knowledge",
        "get_policy_clause",
    }.issubset(tools)
    assert "TKT1001 REFUNDED" in result.response.answer
    assert "TKT1002 OPEN" in result.response.answer
    assert "已退款成功" not in result.response.answer

    trace_types = [event["event_type"] for event in service.get_trace(result.case_id)]
    assert trace_types.count("agent.invoked") == 2
    assert "quality.checked" in trace_types
    assert "handoff.queued" in trace_types


def test_missing_reference_short_circuits_before_tools(tmp_path) -> None:
    service = build_service(runtime_root=tmp_path, prefer_chroma=False)
    result = service.chat(ChatRequest(message="我的退款为什么还没到账？"))
    assert result.response.response_status == "needs_clarification"
    assert _tool_events(service, result.case_id) == []


def test_tool_timeout_becomes_gap_not_fake_fact(tmp_path) -> None:
    service = build_service(
        runtime_root=tmp_path,
        prefer_chroma=False,
        forced_tool_statuses={"get_refund_status": ToolStatus.TIMEOUT},
    )
    result = service.chat(ChatRequest(message="PNR AB12CD 的退款进度是什么？"))
    assert result.response.response_status == "degraded"
    assert "结论仅基于已核验记录" in result.response.answer
    timeout = next(
        event
        for event in _tool_events(service, result.case_id)
        if event["toolName"] == "get_refund_status"
    )
    assert timeout["status"] == "timeout"
    assert timeout["attempt"] == 2


def test_public_flight_query_does_not_route_to_refund(tmp_path) -> None:
    service = build_service(runtime_root=tmp_path, prefer_chroma=False)
    result = service.chat(
        ChatRequest(
            message="请问 CZ8888 航班 2026-07-29 正常吗？",
            verified_subject_id=None,
        )
    )
    tools = {event["toolName"] for event in _tool_events(service, result.case_id)}
    assert "get_flight_status" in tools
    assert "get_refund_status" not in tools
    assert "当前状态为正常" in result.response.answer
