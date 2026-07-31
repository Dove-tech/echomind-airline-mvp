"""设计 §19/§20 对应的硬规则与幂等性测试。"""

from airline_mvp.models import HandoffPacket, ServiceResponse
from airline_mvp.persistence import (
    CaseRepository,
    HandoffRepository,
    SQLiteDatabase,
)
from airline_mvp.quality import QualityGate


def test_quality_gate_blocks_false_write_success_claim() -> None:
    report = QualityGate().evaluate(
        ServiceResponse(
            response_status="answered",
            answer="已退款成功，请注意查收。",
        ),
        [],
    )
    assert report.decision == "block"
    assert report.prohibited_phrases


def test_handoff_is_idempotent(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "handoff.sqlite3")
    cases = CaseRepository(database)
    cases.start_case(
        conversation_id="conv_test",
        case_id="case_test",
        request_id="req_test",
        message="请帮我退票",
        verified_subject_id="subject_demo",
        locale="zh-CN",
    )
    repository = HandoffRepository(database)
    packet = HandoffPacket(
        case_id="case_test",
        reason_code="WRITE_ACTION",
        target_queue="airline_general_service",
        customer_request="请帮我退票",
    )
    first = repository.queue(packet)
    second = repository.queue(packet)
    assert first.status == "queued"
    assert second.handoff_id == first.handoff_id
