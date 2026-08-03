"""设计 §17 对应的传输层 Adapter 测试。"""

from fastapi.testclient import TestClient

from airline_mvp.api import create_app
from airline_mvp.config import RuntimeSettings
from airline_mvp.service import build_service


def test_chat_and_trace_endpoints(tmp_path) -> None:
    service = build_service(
        runtime_root=tmp_path,
        settings=RuntimeSettings(
            llm_backend="mock",
            database_backend="sqlite",
            checkpoint_backend="memory",
            embedding_backend="mock",
            knowledge_backend="local",
        ),
    )
    client = TestClient(create_app(service))
    response = client.post(
        "/v1/chat",
        json={
            "message": "PNR AB12CD 的退款为什么还没到账？",
            "verified_subject_id": "subject_demo",
        },
    )
    assert response.status_code == 200
    body = response.json()
    trace = client.get(f"/v1/cases/{body['case_id']}/trace")
    assert trace.status_code == 200
    assert any(item["event_type"] == "quality.checked" for item in trace.json())
