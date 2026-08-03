"""Mock/真实基础设施切换边界测试；不会访问网络或 PostgreSQL。"""

import pytest
from langchain_core.messages import AIMessage

from airline_mvp.config import ConfigurationError, RuntimeSettings
from airline_mvp.domain_config import get_domain_config
from airline_mvp.fixtures import AirlineFixtureStore
from airline_mvp.knowledge import (
    KnowledgeService,
    LocalKnowledgeStore,
    load_policy_documents,
)
from airline_mvp.model_gateway import StructuredLLMGateway
from airline_mvp.models import (
    AirlineEntities,
    CasePlan,
    DomainName,
    DomainTask,
    RequestUnderstanding,
    ServiceResponse,
)
from airline_mvp.persistence import (
    _PostgreSQLConnectionAdapter,
    _translate_sqlite_sql_to_postgres,
)
from airline_mvp.tools import ToolRegistry, build_tool_registry


def test_real_llm_configuration_fails_fast_when_secret_is_missing() -> None:
    settings = RuntimeSettings(
        llm_backend="openai_compatible",
        llm_base_url="https://example.invalid/v1",
        llm_model="example-model",
        llm_api_key=None,
    )
    with pytest.raises(ConfigurationError, match="AIRLINE_MVP_LLM_API_KEY"):
        settings.validate()


def test_postgres_adapter_translates_only_repository_sql_subset() -> None:
    statement = (
        "INSERT OR IGNORE INTO messages(message_id, content) VALUES (?, ?)"
    )
    translated = _translate_sqlite_sql_to_postgres(statement)
    assert "INSERT INTO messages" in translated
    assert "VALUES (%s, %s)" in translated
    assert translated.endswith("ON CONFLICT DO NOTHING")


def test_postgres_adapter_does_not_parse_percent_in_parameterless_sql() -> None:
    """无参数 SQL 的 LIKE 百分号不能被 psycopg 当成参数占位符。"""

    class RecordingConnection:
        def __init__(self) -> None:
            self.calls = []

        def execute(self, *args):
            self.calls.append(args)
            return "ok"

    connection = RecordingConnection()
    adapter = _PostgreSQLConnectionAdapter(connection)

    result = adapter.execute(
        "SELECT 1 WHERE 'idx_name' LIKE 'idx_knowledge_%_hnsw'"
    )

    assert result == "ok"
    assert connection.calls == [
        ("SELECT 1 WHERE 'idx_name' LIKE 'idx_knowledge_%_hnsw'",)
    ]


class _FakeStructuredRunnable:
    """模拟“已经由远端模型返回结构化数据”的 LangChain Runnable。"""

    def __init__(self, schema, calls) -> None:
        self.schema = schema
        self.calls = calls

    def invoke(self, messages):
        self.calls.append((self.schema.__name__, messages))
        if self.schema is RequestUnderstanding:
            return {
                "user_goal": "查询航班状态",
                "intents": ["journey_support"],
                "entities": {
                    "flight_no": "CZ8888",
                    "travel_date": "2026-07-29",
                },
                # 部分兼容模型会用空字符串代替空数组项，Gateway 必须清洗。
                "missing_fields": [""],
                "risk_flags": [""],
                "requested_write_action": False,
            }
        if self.schema is ServiceResponse:
            return {
                "response_status": "answered",
                "answer": "这是来自结构化模型的旅客回复。",
                "verified_facts": [],
                "available_options": [],
                "missing_information": [],
                "handoff_required": False,
                "must_not_claim": [],
            }
        raise AssertionError(f"本测试未准备 {self.schema.__name__} 的返回值")


class _FakeChatModel:
    def __init__(self) -> None:
        self.calls = []
        self.bound_tools = []
        self.function_message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_flight_status",
                    "args": {
                        "flight_no": "CZ8888",
                        "date": "2026-07-29",
                    },
                    "id": "model_call_001",
                    "type": "tool_call",
                }
            ],
        )

    def with_structured_output(self, schema):
        return _FakeStructuredRunnable(schema, self.calls)

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append((tools, kwargs))
        message = self.function_message

        class _FakeFunctionRunnable:
            def invoke(self, _messages):
                return message

        return _FakeFunctionRunnable()


def _registry() -> ToolRegistry:
    fixtures = AirlineFixtureStore()
    knowledge = KnowledgeService(
        LocalKnowledgeStore(load_policy_documents())
    )
    return build_tool_registry(fixtures, knowledge)


def test_structured_gateway_uses_real_model_boundary_for_reply() -> None:
    chat_model = _FakeChatModel()
    gateway = StructuredLLMGateway(chat_model, _registry())

    understanding = gateway.understand("CZ8888 航班 2026-07-29 正常吗？")
    plan = gateway.plan(understanding)
    response = gateway.synthesize(
        user_message=understanding.user_goal,
        plan=CasePlan(
            case_type=plan.case_type,
            user_goal=plan.user_goal,
            intents=plan.intents,
            missing_fields=[],
            tasks=[],
        ),
        findings=[],
        evidence=[],
    )

    assert [name for name, _messages in chat_model.calls] == [
        "RequestUnderstanding",
        "ServiceResponse",
    ]
    assert response.answer == "这是来自结构化模型的旅客回复。"
    assert "不得声称退款、改签或补偿已经执行" in response.must_not_claim


def test_real_understanding_uses_deterministic_guard_for_missed_refund_intent() -> None:
    """模型漏掉明显退款意图时，Coordinator 仍必须创建 Refund 任务。"""

    gateway = StructuredLLMGateway(_FakeChatModel(), _registry())
    understanding = gateway.understand(
        "PNR EK7D3M，TKT3001 的退款进度如何？"
    )

    assert "refund_status" in understanding.intents
    assert understanding.entities.pnr_ref == "EK7D3M"
    assert understanding.entities.ticket_refs == ["TKT3001"]
    assert understanding.missing_fields == []
    assert understanding.risk_flags == []


def test_real_gateway_uses_native_function_call_with_exact_tool_schema() -> None:
    chat_model = _FakeChatModel()
    gateway = StructuredLLMGateway(chat_model, _registry())
    config = get_domain_config(DomainName.JOURNEY)
    task = DomainTask(
        task_id="task_fc_test",
        domain=DomainName.JOURNEY,
        objective="查询指定航班状态",
        entity_refs={
            "flight_no": "CZ8888",
            "travel_date": "2026-07-29",
        },
        allowed_tools=["get_flight_status"],
        required_evidence=["flight"],
    )

    decision = gateway.decide_domain_step(
        config=config,
        task=task,
        entities=AirlineEntities(
            flight_no="CZ8888",
            travel_date="2026-07-29",
        ),
        evidence=[],
        tool_calls=[],
    )

    assert decision.action == "call_tool"
    assert decision.tool_name == "get_flight_status"
    assert decision.arguments == {
        "flight_no": "CZ8888",
        "date": "2026-07-29",
    }
    assert decision.decision_source == "function_call"
    assert decision.model_tool_call_id == "model_call_001"

    bound_tools, options = chat_model.bound_tools[0]
    assert options == {"tool_choice": "auto"}
    assert [item["function"]["name"] for item in bound_tools] == [
        "get_flight_status"
    ]
    parameters = bound_tools[0]["function"]["parameters"]
    assert set(parameters["required"]) == {"flight_no", "date"}
    assert parameters["additionalProperties"] is False


def test_real_gateway_treats_no_function_call_as_domain_finish() -> None:
    chat_model = _FakeChatModel()
    chat_model.function_message = AIMessage(content="现有证据已经足够，无需继续查询。")
    gateway = StructuredLLMGateway(chat_model, _registry())
    config = get_domain_config(DomainName.JOURNEY)
    task = DomainTask(
        task_id="task_fc_finish",
        domain=DomainName.JOURNEY,
        objective="完成调查",
        allowed_tools=["get_flight_status"],
        required_evidence=["flight"],
    )

    decision = gateway.decide_domain_step(
        config=config,
        task=task,
        entities=AirlineEntities(),
        evidence=[],
        tool_calls=[],
    )

    assert decision.action == "finish"
    assert decision.decision_source == "function_call"
    assert decision.reason == "现有证据已经足够，无需继续查询。"
