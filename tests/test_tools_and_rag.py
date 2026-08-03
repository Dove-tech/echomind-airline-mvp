"""设计 §15/§16/§25 对应边界的单元测试。"""

from pathlib import Path

import pytest

from airline_mvp.fixtures import AirlineFixtureStore
from airline_mvp.knowledge import LocalKnowledgeStore, KnowledgeService, load_policy_documents
from airline_mvp.models import DomainName, ToolExecutionContext, ToolStatus
from airline_mvp.tools import ToolExecutor, build_tool_registry


def _context(subject: str | None) -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req_test",
        case_id="case_test",
        invocation_id="inv_test",
        tool_call_id="tc_test",
        verified_subject_id=subject,
    )


def _executor() -> ToolExecutor:
    fixtures = AirlineFixtureStore()
    knowledge = KnowledgeService(LocalKnowledgeStore(load_policy_documents()))
    registry = build_tool_registry(fixtures, knowledge)
    return ToolExecutor(registry, fixtures.dataset_version)


def test_sensitive_tool_requires_verified_subject() -> None:
    executor = _executor()
    result = executor.execute(
        domain=DomainName.JOURNEY,
        allowed_tools=["get_booking"],
        tool_name="get_booking",
        arguments={"pnr_ref": "AB12CD"},
        context=_context(None),
    )
    assert result.status == ToolStatus.DENIED
    assert result.error_code == "SUBJECT_NOT_VERIFIED"


def test_cross_subject_record_is_denied() -> None:
    executor = _executor()
    result = executor.execute(
        domain=DomainName.JOURNEY,
        allowed_tools=["get_booking"],
        tool_name="get_booking",
        arguments={"pnr_ref": "PRIVATE1"},
        context=_context("subject_demo"),
    )
    assert result.status == ToolStatus.DENIED
    assert result.error_code == "RECORD_SCOPE_DENIED"


def test_refund_agent_cannot_call_journey_tool() -> None:
    executor = _executor()
    result = executor.execute(
        domain=DomainName.REFUND,
        allowed_tools=["get_flight_status"],
        tool_name="get_flight_status",
        arguments={"flight_no": "CZ3101", "date": "2026-07-29"},
        context=_context("subject_demo"),
    )
    assert result.status == ToolStatus.DENIED
    assert result.error_code == "TOOL_NOT_ALLOWED"


def test_function_call_schema_is_generated_from_runtime_input_model() -> None:
    fixtures = AirlineFixtureStore()
    knowledge = KnowledgeService(LocalKnowledgeStore(load_policy_documents()))
    registry = build_tool_registry(fixtures, knowledge)

    schemas = registry.function_call_schemas(
        domain=DomainName.JOURNEY,
        allowed_tools=["get_flight_status"],
    )

    function = schemas[0]["function"]
    assert function["name"] == "get_flight_status"
    assert set(function["parameters"]["required"]) == {"flight_no", "date"}
    assert function["parameters"]["additionalProperties"] is False


def test_function_call_schema_rejects_cross_domain_exposure() -> None:
    fixtures = AirlineFixtureStore()
    knowledge = KnowledgeService(LocalKnowledgeStore(load_policy_documents()))
    registry = build_tool_registry(fixtures, knowledge)

    with pytest.raises(ValueError, match="不允许由 refund Agent 使用"):
        registry.function_call_schemas(
            domain=DomainName.REFUND,
            allowed_tools=["get_flight_status"],
        )


def test_local_test_rag_is_not_labeled_as_postgresql() -> None:
    """离线测试的 Evidence 来源必须诚实反映 Local RAG Adapter。"""

    fixtures = AirlineFixtureStore()
    knowledge = KnowledgeService(LocalKnowledgeStore(load_policy_documents()))
    registry = build_tool_registry(fixtures, knowledge)

    definition = registry.get("search_airline_knowledge")

    assert definition is not None
    assert definition.source_system == "local_rag"


def test_tool_executor_rejects_legacy_wrapped_function_arguments() -> None:
    """防止旧式 ``parameters`` 包装再次被误当作真实业务参数。"""

    result = _executor().execute(
        domain=DomainName.JOURNEY,
        allowed_tools=["get_flight_status"],
        tool_name="get_flight_status",
        arguments={
            "tool_name": "get_flight_status",
            "parameters": {
                "flight_no": "CZ8888",
                "date": "2026-07-29",
            },
        },
        context=_context("subject_demo"),
    )

    assert result.status == ToolStatus.INVALID_INPUT
    assert result.error_code == "INPUT_SCHEMA_INVALID"


def test_knowledge_arguments_are_canonicalized_by_server_policy() -> None:
    executor = _executor()
    canonical = executor.canonicalize_arguments(
        domain=DomainName.JOURNEY,
        tool_name="search_airline_knowledge",
        arguments={
            "query": "EK302 航班取消后的退款和改签规定",
            "domains": ["hallucinated_policy_domain"],
            "as_of": "2026-08-15",
        },
    )

    assert canonical["domains"] == ["journey", "disruption", "ticketing"]
    assert canonical["carrier_codes"] == ["EK"]


def test_rag_filters_expired_policy_and_supports_exact_drilldown() -> None:
    service = KnowledgeService(LocalKnowledgeStore(load_policy_documents()))
    hits = service.search(
        "航班取消 非自愿退票",
        ["journey"],
        "2026-07-29",
        5,
    )
    ids = {hit["documentId"] for hit in hits}
    assert "journey_irrop_2026" in ids
    assert "journey_irrop_2025" not in ids

    clause = service.get_clause("journey_irrop_2026", "2026-06-01", "4.2")
    assert clause is not None
    assert clause["authority"] == "official_policy"
    assert "非自愿改签" in clause["text"]
