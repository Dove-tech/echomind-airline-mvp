"""可复用的 LangGraph 领域 Worker 子图。

设计映射
--------
- 设计 §12：JourneyServiceAgent 和 RefundServiceAgent 持有各自领域 Tool。
- 设计 §13：两个 Agent 都是同一个可配置 Worker Graph 的实例。
- 设计 §14.3：Tool 预算与重复签名循环防护。
- 设计 §15/§19：先标准化 Tool 结果，再转换为 Evidence。

该 Graph 就是 Agent Runtime。``DomainAgentConfig`` 提供角色、Tool 权限和预算，
因此新增业务域时不需要复制 Graph 代码。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph

from .domain_config import DomainAgentConfig
from .evidence import tool_result_to_evidence
from .model_gateway import ModelGateway
from .models import (
    DomainDecision,
    GraphError,
    ToolCallRecord,
    ToolExecutionContext,
    utc_now,
)
from .persistence import TraceRepository
from .state import DomainWorkerState
from .tools import ToolExecutor, ToolRegistry, new_tool_call_id


@dataclass(frozen=True)
class WorkerDependencies:
    model: ModelGateway
    executor: ToolExecutor
    registry: ToolRegistry
    traces: TraceRepository


def _signature(tool_name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()
    return digest[:20]


def build_domain_worker_graph(
    config: DomainAgentConfig, dependencies: WorkerDependencies
) -> Any:
    """编译一个相互隔离的领域 Worker Graph。

    这里不绑定 Checkpointer：会话 Checkpoint 由 Parent Graph 统一管理。
    子图只返回 DomainFinding、Evidence 和 ToolCall 契约，防止私有临时 State
    在不同业务域之间泄漏。
    """

    def prepare(state: DomainWorkerState) -> dict[str, Any]:
        invocation_id = state.get("invocation_id") or f"inv_{uuid.uuid4().hex[:16]}"
        dependencies.traces.append(
            trace_id=state["trace_id"],
            case_id=state["case_id"],
            event_type="agent.invoked",
            payload={
                "invocationId": invocation_id,
                "agent": f"{config.domain.value}ServiceAgent",
                "taskId": state["task"].task_id,
                "allowedTools": state["task"].allowed_tools,
            },
        )
        return {
            "invocation_id": invocation_id,
            "evidence": [],
            "tool_calls": [],
            "called_signatures": [],
            "errors": [],
        }

    def decide(state: DomainWorkerState) -> dict[str, Any]:
        task = state["task"]
        if len(state.get("tool_calls", [])) >= min(
            task.max_tool_calls, config.max_tool_calls
        ):
            decision = DomainDecision(
                action="finish", reason="已达到 domain tool-call budget"
            )
        else:
            decision = dependencies.model.decide_domain_step(
                config=config,
                task=task,
                entities=state["entities"],
                evidence=state.get("evidence", []),
                tool_calls=state.get("tool_calls", []),
            )

        # 设计 §14.3：模型不能持续发出完全相同的调用。
        if decision.action == "call_tool" and decision.tool_name:
            call_signature = _signature(decision.tool_name, decision.arguments)
            if call_signature in state.get("called_signatures", []):
                decision = DomainDecision(
                    action="finish",
                    reason="检测到重复工具签名，终止潜在循环",
                )
        dependencies.traces.append(
            trace_id=state["trace_id"],
            case_id=state["case_id"],
            event_type="agent.decision",
            payload={
                "invocationId": state["invocation_id"],
                "taskId": task.task_id,
                "action": decision.action,
                "toolName": decision.tool_name,
                "reason": decision.reason,
            },
        )
        return {"next_decision": decision}

    def route_after_decision(state: DomainWorkerState) -> str:
        decision = DomainDecision.model_validate(state["next_decision"])
        return "execute_tool" if decision.action == "call_tool" else "finalize"

    def execute_tool(state: DomainWorkerState) -> dict[str, Any]:
        decision = DomainDecision.model_validate(state["next_decision"])
        assert decision.tool_name is not None
        tool_call_id = new_tool_call_id()
        started_at = utc_now()
        context = ToolExecutionContext(
            request_id=state["request_id"],
            case_id=state["case_id"],
            invocation_id=state["invocation_id"],
            tool_call_id=tool_call_id,
            verified_subject_id=state.get("verified_subject_id"),
        )
        result = dependencies.executor.execute(
            domain=config.domain,
            allowed_tools=state["task"].allowed_tools,
            tool_name=decision.tool_name,
            arguments=decision.arguments,
            context=context,
        )
        ended_at = utc_now()
        record = ToolCallRecord(
            tool_call_id=tool_call_id,
            invocation_id=state["invocation_id"],
            task_id=state["task"].task_id,
            domain=config.domain,
            tool_name=decision.tool_name,
            arguments=decision.arguments,
            status=result.status,
            started_at=started_at,
            ended_at=ended_at,
            error_code=result.error_code,
        )
        definition = dependencies.registry.get(decision.tool_name)
        evidence = (
            tool_result_to_evidence(
                case_id=state["case_id"],
                definition=definition,
                result=result,
            )
            if definition is not None
            else []
        )
        dependencies.traces.append(
            trace_id=state["trace_id"],
            case_id=state["case_id"],
            event_type="tool.completed",
            payload={
                "toolCallId": tool_call_id,
                "invocationId": state["invocation_id"],
                "toolName": decision.tool_name,
                # 当前参数是安全的 Demo 引用。生产 Trace Exporter
                # 应在这里对 PNR 和票号进行脱敏。
                "argumentKeys": sorted(decision.arguments),
                "status": result.status.value,
                "attempt": result.audit.attempt,
                "evidenceIds": [item.evidence_id for item in evidence],
                "durationMs": result.audit.duration_ms,
            },
        )
        errors: list[GraphError] = []
        if result.error_code:
            errors.append(
                GraphError(
                    code=result.error_code,
                    message=result.error_message or result.status.value,
                    node="execute_tool",
                    retryable=result.status.value in {"timeout", "unavailable"},
                )
            )
        return {
            "tool_calls": [record],
            "evidence": evidence,
            "called_signatures": [
                _signature(decision.tool_name, decision.arguments)
            ],
            "errors": errors,
        }

    def finalize(state: DomainWorkerState) -> dict[str, Any]:
        finding = dependencies.model.finalize_finding(
            task=state["task"],
            evidence=state.get("evidence", []),
            tool_calls=state.get("tool_calls", []),
        )
        dependencies.traces.append(
            trace_id=state["trace_id"],
            case_id=state["case_id"],
            event_type="agent.completed",
            payload={
                "invocationId": state["invocation_id"],
                "taskId": state["task"].task_id,
                "status": finding.status,
                "factCount": len(finding.facts),
                "policyCount": len(finding.policy_conclusions),
                "gapCount": len(finding.gaps),
            },
        )
        return {"finding": finding}

    graph = StateGraph(DomainWorkerState)
    graph.add_node("prepare", prepare)
    graph.add_node("decide", decide)
    graph.add_node("execute_tool", execute_tool)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "decide")
    graph.add_conditional_edges(
        "decide",
        route_after_decision,
        {"execute_tool": "execute_tool", "finalize": "finalize"},
    )
    graph.add_edge("execute_tool", "decide")
    graph.add_edge("finalize", END)
    return graph.compile()
