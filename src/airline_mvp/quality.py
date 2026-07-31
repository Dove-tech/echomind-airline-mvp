"""确定性的回复质量与安全 Gate。

设计映射
--------
- 设计 §19.3：质量检查是规则节点，而不是装饰性的额外 Agent。
- 设计 §20：只有已经入队的 Handoff 才能描述为已转接。
- 设计 §25：只读助手绝不能声称写操作已经成功。

该 Gate 通过确定性规则校验 Evidence 引用和高风险话术。
离线流程可以增加 LLM Judge，但不能用它替代这些硬规则。
"""

from __future__ import annotations

from .models import EvidenceItem, QualityReport, ServiceResponse


PROHIBITED_EXECUTION_PHRASES = (
    "已退款成功",
    "退款已经成功",
    "已为您改签",
    "已经为您改签",
    "补偿已发放",
    "已完成赔付",
)


class QualityGate:
    """根据 Evidence 和权限不变量校验旅客回答。"""

    def evaluate(
        self,
        response: ServiceResponse,
        evidence: list[EvidenceItem],
        *,
        handoff_is_queued: bool = False,
    ) -> QualityReport:
        evidence_ids = {item.evidence_id for item in evidence}
        referenced = {
            reference
            for statement in response.verified_facts
            for reference in statement.evidence_ids
        }
        referenced.update(
            reference
            for option in response.available_options
            for reference in option.evidence_ids
        )
        invalid = sorted(referenced - evidence_ids)

        prohibited = [
            phrase for phrase in PROHIBITED_EXECUTION_PHRASES if phrase in response.answer
        ]
        if not handoff_is_queued and any(
            phrase in response.answer for phrase in ("已转人工", "已经转接人工")
        ):
            prohibited.append("未创建队列记录前声称已转人工")

        violations: list[str] = []
        if invalid:
            violations.append("答案引用了当前 Case 不存在的 evidenceId")
        if prohibited:
            violations.append("只读系统声称写操作已经完成")
        if response.handoff_required and not response.handoff_reason:
            violations.append("要求人工接管但未给出 reason code")
        if any(option.execution_status != "not_executed" for option in response.available_options):
            violations.append("MVP 只允许展示未执行的可选方案")

        if prohibited:
            decision = "block"
        elif invalid or violations:
            decision = "revise"
        elif response.handoff_required:
            decision = "handoff"
        else:
            decision = "pass"
        return QualityReport(
            decision=decision,
            violations=violations,
            invalid_evidence_ids=invalid,
            prohibited_phrases=prohibited,
            handoff_required=response.handoff_required,
        )

    def safe_fallback(self, response: ServiceResponse) -> ServiceResponse:
        """一次有边界修订仍失败时，返回保守回答。"""

        return response.model_copy(
            update={
                "response_status": "degraded",
                "answer": (
                    "当前只读系统暂时无法在证据约束内完整回答。"
                    "我不会声称任何退票、改签、退款或补偿操作已经完成；"
                    "如需继续办理，请由人工客服核验。"
                ),
                "verified_facts": [],
                "available_options": [],
                "handoff_required": True,
                "handoff_reason": "QUALITY_GATE_FALLBACK",
            }
        )
