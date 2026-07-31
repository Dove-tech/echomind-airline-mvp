"""Mock/真实基础设施切换边界测试；不会访问网络或 PostgreSQL。"""

import pytest

from airline_mvp.config import ConfigurationError, RuntimeSettings
from airline_mvp.model_gateway import StructuredLLMGateway
from airline_mvp.models import CasePlan, RequestUnderstanding, ServiceResponse
from airline_mvp.persistence import _translate_sqlite_sql_to_postgres


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
                "missing_fields": [],
                "risk_flags": [],
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

    def with_structured_output(self, schema):
        return _FakeStructuredRunnable(schema, self.calls)


def test_structured_gateway_uses_real_model_boundary_for_reply() -> None:
    chat_model = _FakeChatModel()
    gateway = StructuredLLMGateway(chat_model)

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

