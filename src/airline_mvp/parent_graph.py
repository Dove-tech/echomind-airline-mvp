"""实现旅客服务控制流的 Parent LangGraph。

设计映射
--------
- 设计 §9：请求生命周期，以及有边界的质量检查/人工接管分支。
- 设计 §10：共享 State，以及通过 Reducer 安全合并的并行 Worker 输出。
- 设计 §14：动态路由；简单请求无需经过所有 Agent。
- 设计 §20：人工接管由确定性应用代码创建。

ServiceCoordinatorAgent 负责理解、规划和最终沟通，不持有业务 Tool。
领域 Worker 通过 ``Send`` 接收边界明确的任务；当计划包含两个相互独立的
业务域时，可以并行运行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .domain_config import DOMAIN_CONFIGS
from .model_gateway import ModelGateway
from .models import (
    CaseStatus,
    HandoffPacket,
    ServiceResponse,
    TokenUsage,
)
from .persistence import CaseRepository, HandoffRepository, TraceRepository
from .quality import QualityGate
from .state import AirlineMVPState
from .worker_graph import WorkerDependencies, build_domain_worker_graph


@dataclass(frozen=True)
class ParentDependencies:
    model: ModelGateway
    worker_dependencies: WorkerDependencies
    cases: CaseRepository
    handoffs: HandoffRepository
    traces: TraceRepository
    quality: QualityGate


def _trace(
    dependencies: ParentDependencies,
    state: AirlineMVPState,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    dependencies.traces.append(
        trace_id=state["trace_id"],
        case_id=state["case_id"],
        event_type=event_type,
        payload=payload or {},
    )


def build_parent_graph(dependencies: ParentDependencies, checkpointer: Any) -> Any:
    """构建并编译主 Graph。

    Worker 实例只根据配置编译一次。Parent Graph 不会根据模型厂商、
    Prompt 实现或 Tool Adapter 类型建立业务分支。
    """

    worker_graphs = {
        domain: build_domain_worker_graph(config, dependencies.worker_dependencies)
        for domain, config in DOMAIN_CONFIGS.items()
        # Baggage 是文档中预留的扩展点；MVP 尚未注册模拟行李系统，
        # 因此这里有意不允许向该业务域分派任务。
        if domain.value in {"journey", "refund"}
    }

    def validate_and_load(state: AirlineMVPState) -> dict[str, Any]:
        _trace(
            dependencies,
            state,
            "request.received",
            {
                "requestId": state["request_id"],
                "conversationId": state["conversation_id"],
                "messageLength": len(state["current_message"]),
            },
        )
        dependencies.cases.update_case(
            case_id=state["case_id"], status=CaseStatus.UNDERSTANDING
        )
        return {
            "status": CaseStatus.UNDERSTANDING.value,
            "replan_count": state.get("replan_count", 0),
            "revision_count": state.get("revision_count", 0),
        }

    def understand_and_plan(state: AirlineMVPState) -> dict[str, Any]:
        understanding = dependencies.model.understand(state["current_message"])
        plan = dependencies.model.plan(understanding)
        _trace(
            dependencies,
            state,
            "coordinator.planned",
            {
                "intents": understanding.intents,
                "entities": understanding.entities.model_dump(
                    mode="json", exclude_none=True
                ),
                "missingFields": understanding.missing_fields,
                "riskFlags": understanding.risk_flags,
                "tasks": [
                    {
                        "taskId": task.task_id,
                        "domain": task.domain.value,
                        "maxToolCalls": task.max_tool_calls,
                    }
                    for task in plan.tasks
                ],
                "parallel": plan.parallel,
            },
        )
        dependencies.cases.update_case(
            case_id=state["case_id"],
            status=(
                CaseStatus.WAITING_FOR_INFORMATION
                if plan.missing_fields
                else CaseStatus.RESEARCHING
            ),
            user_goal=understanding.user_goal,
            plan=plan,
        )
        return {
            "intents": understanding.intents,
            "entities": understanding.entities,
            "missing_fields": understanding.missing_fields,
            "risk_flags": understanding.risk_flags,
            "user_goal": understanding.user_goal,
            "plan": plan,
            "domain_tasks": plan.tasks,
            "status": (
                CaseStatus.WAITING_FOR_INFORMATION.value
                if plan.missing_fields
                else CaseStatus.RESEARCHING.value
            ),
        }

    def route_after_plan(state: AirlineMVPState) -> str:
        plan = state["plan"]
        if plan is None or plan.missing_fields:
            return "clarify"
        if not plan.tasks:
            return "unsupported"
        return "dispatch"

    def clarify(state: AirlineMVPState) -> dict[str, Any]:
        missing = state.get("missing_fields", [])
        labels = {
            "pnr_ref_or_flight_and_date": "PNR，或航班号和乘机日期",
            "pnr_or_ticket_or_refund_reference": "PNR、票号或退款申请号",
        }
        requested = [labels.get(item, item) for item in missing]
        response = ServiceResponse(
            response_status="needs_clarification",
            answer="为了查询准确记录，请补充：" + "；".join(requested) + "。",
            missing_information=missing,
        )
        _trace(
            dependencies,
            state,
            "coordinator.clarification_requested",
            {"missingFields": missing},
        )
        return {
            "service_response": response,
            "status": CaseStatus.WAITING_FOR_INFORMATION.value,
        }

    def unsupported(state: AirlineMVPState) -> dict[str, Any]:
        response = ServiceResponse(
            response_status="degraded",
            answer=(
                "当前演示版支持航班异常、客票和退款状态查询。"
                "这个问题超出已接入的只读业务域，需要人工客服继续处理。"
            ),
            handoff_required=True,
            handoff_reason="UNSUPPORTED_MVP_DOMAIN",
        )
        return {"service_response": response}

    def dispatch(_state: AirlineMVPState) -> dict[str, Any]:
        # 实际 Fan-out 由下方的 ``Send`` 表达。保留一个具名节点，
        # 可以让 Trace 和 Graph 图在面试讲解时更清晰。
        return {}

    def fan_out(state: AirlineMVPState) -> list[Send]:
        sends: list[Send] = []
        for task in state["plan"].tasks:
            sends.append(
                Send(
                    "run_domain_worker",
                    {
                        "active_task": task,
                        "entities": state["entities"],
                        "case_id": state["case_id"],
                        "request_id": state["request_id"],
                        "trace_id": state["trace_id"],
                        "conversation_id": state["conversation_id"],
                        "verified_subject_id": state.get("verified_subject_id"),
                    },
                )
            )
        _trace(
            dependencies,
            state,
            "coordinator.dispatched",
            {
                "taskIds": [task.task_id for task in state["plan"].tasks],
                "parallel": len(sends) > 1,
            },
        )
        return sends

    def run_domain_worker(state: AirlineMVPState) -> dict[str, Any]:
        task = state["active_task"]
        result = worker_graphs[task.domain].invoke(
            {
                "task": task,
                "entities": state["entities"],
                "case_id": state["case_id"],
                "request_id": state["request_id"],
                "trace_id": state["trace_id"],
                "conversation_id": state["conversation_id"],
                "verified_subject_id": state.get("verified_subject_id"),
                "evidence": [],
                "tool_calls": [],
                "called_signatures": [],
                "errors": [],
            },
            config={"recursion_limit": 30},
        )
        return {
            "findings": [result["finding"]],
            "evidence": result.get("evidence", []),
            "tool_calls": result.get("tool_calls", []),
            "errors": result.get("errors", []),
        }

    def synthesize(state: AirlineMVPState) -> dict[str, Any]:
        dependencies.cases.update_case(
            case_id=state["case_id"], status=CaseStatus.SYNTHESIZING
        )
        response = dependencies.model.synthesize(
            user_message=state["current_message"],
            plan=state["plan"],
            findings=state.get("findings", []),
            evidence=state.get("evidence", []),
        )
        _trace(
            dependencies,
            state,
            "coordinator.synthesized",
            {
                "responseStatus": response.response_status,
                "findingCount": len(state.get("findings", [])),
                "evidenceIds": [
                    item.evidence_id for item in state.get("evidence", [])
                ],
            },
        )
        return {
            "service_response": response,
            "status": CaseStatus.SYNTHESIZING.value,
        }

    def quality_check(state: AirlineMVPState) -> dict[str, Any]:
        response = state["service_response"]
        report = dependencies.quality.evaluate(
            response,
            state.get("evidence", []),
            handoff_is_queued=bool(state.get("handoff_id")),
        )
        update: dict[str, Any] = {"quality_report": report}
        if report.decision in {"revise", "block"}:
            # 设计 §19.3：只进行一次有边界的确定性修正，
            # 不允许 answer↔review Agent 之间无限乒乓。
            update["service_response"] = dependencies.quality.safe_fallback(response)
            update["revision_count"] = state.get("revision_count", 0) + 1
        _trace(
            dependencies,
            state,
            "quality.checked",
            {
                "decision": report.decision,
                "violations": report.violations,
                "invalidEvidenceIds": report.invalid_evidence_ids,
            },
        )
        return update

    def route_after_quality(state: AirlineMVPState) -> str:
        response = state["service_response"]
        report = state["quality_report"]
        return (
            "queue_handoff"
            if response.handoff_required or report.decision == "handoff"
            else "persist"
        )

    def queue_handoff(state: AirlineMVPState) -> dict[str, Any]:
        response = state["service_response"]
        packet = HandoffPacket(
            case_id=state["case_id"],
            reason_code=response.handoff_reason or "HUMAN_REVIEW_REQUIRED",
            target_queue="airline_general_service",
            priority="high"
            if "immediate_human_escalation" in state.get("risk_flags", [])
            else "normal",
            customer_request=state.get("user_goal", state["current_message"]),
            verified_fact_refs=[
                ref
                for statement in response.verified_facts
                for ref in statement.evidence_ids
            ],
            unresolved_items=[
                gap
                for finding in state.get("findings", [])
                for gap in finding.gaps
            ],
            conversation_cursor=state["conversation_id"],
        )
        queued = dependencies.handoffs.queue(packet)
        _trace(
            dependencies,
            state,
            "handoff.queued",
            {
                "handoffId": queued.handoff_id,
                "reasonCode": queued.reason_code,
                "targetQueue": queued.target_queue,
            },
        )
        return {
            "handoff_packet": queued,
            "handoff_id": queued.handoff_id,
            "status": CaseStatus.WAITING_FOR_HUMAN.value,
        }

    def persist(state: AirlineMVPState) -> dict[str, Any]:
        response = state["service_response"]
        evidence = state.get("evidence", [])
        calls = state.get("tool_calls", [])
        dependencies.cases.save_tool_calls(state["case_id"], calls)
        dependencies.cases.save_evidence(evidence)
        dependencies.cases.save_response(state["case_id"], response)
        final_status = (
            CaseStatus.WAITING_FOR_HUMAN
            if state.get("handoff_id")
            else CaseStatus.WAITING_FOR_INFORMATION
            if response.response_status == "needs_clarification"
            else CaseStatus.RESPONDED
        )
        summary = (
            f"intent={','.join(state.get('intents', []))}; "
            f"evidence={len(evidence)}; tools={len(calls)}; "
            f"response={response.response_status}"
        )
        dependencies.cases.update_case(
            case_id=state["case_id"],
            status=final_status,
            user_goal=state.get("user_goal"),
            case_summary=summary,
            plan=state.get("plan"),
        )
        _trace(
            dependencies,
            state,
            "case.completed",
            {
                "status": final_status.value,
                "toolCallCount": len(calls),
                "evidenceCount": len(evidence),
                "handoffId": state.get("handoff_id"),
            },
        )
        return {"status": final_status.value, "case_summary": summary}

    graph = StateGraph(AirlineMVPState)
    graph.add_node("validate_and_load", validate_and_load)
    graph.add_node("understand_and_plan", understand_and_plan)
    graph.add_node("clarify", clarify)
    graph.add_node("unsupported", unsupported)
    graph.add_node("dispatch", dispatch)
    graph.add_node("run_domain_worker", run_domain_worker)
    graph.add_node("synthesize", synthesize)
    graph.add_node("quality_check", quality_check)
    graph.add_node("queue_handoff", queue_handoff)
    graph.add_node("persist", persist)

    graph.add_edge(START, "validate_and_load")
    graph.add_edge("validate_and_load", "understand_and_plan")
    graph.add_conditional_edges(
        "understand_and_plan",
        route_after_plan,
        {
            "clarify": "clarify",
            "unsupported": "unsupported",
            "dispatch": "dispatch",
        },
    )
    graph.add_conditional_edges("dispatch", fan_out, ["run_domain_worker"])
    graph.add_edge("run_domain_worker", "synthesize")
    graph.add_edge("unsupported", "quality_check")
    graph.add_edge("synthesize", "quality_check")
    graph.add_conditional_edges(
        "quality_check",
        route_after_quality,
        {"queue_handoff": "queue_handoff", "persist": "persist"},
    )
    graph.add_edge("queue_handoff", "persist")
    graph.add_edge("clarify", "persist")
    graph.add_edge("persist", END)
    return graph.compile(checkpointer=checkpointer)
