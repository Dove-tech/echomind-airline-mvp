"""离线和 Trace-level 评测框架。

设计映射
--------
- 设计 §24：确定性数据集、分组件指标和运行轨迹检查。
- 设计 §19：回答引用必须与实际消费的 Evidence 对照校验。

该评测器不会把全部行为压缩成一个 LLM Judge 分数，而是分别报告路由、
Tool 轨迹、人工接管、Evidence 和安全检查结果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import RuntimeSettings
from .models import ChatRequest, ToolStatus
from .paths import PROJECT_ROOT, RUNTIME_ROOT
from .service import build_service


class EvalExpectation(BaseModel):
    domains: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    response_status: str | None = None
    handoff_required: bool | None = None


class EvalCase(BaseModel):
    id: str
    message: str
    verified_subject_id: str | None = "subject_demo"
    forced_tool_statuses: dict[str, ToolStatus] = Field(default_factory=dict)
    expect: EvalExpectation


class EvalCaseResult(BaseModel):
    case_id: str
    dataset_case_id: str
    checks: dict[str, bool]
    actual_tools: list[str]
    actual_domains: list[str]
    response_status: str

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def load_eval_cases(
    path: Path | None = None,
) -> list[EvalCase]:
    dataset = path or PROJECT_ROOT / "evals" / "airline_mvp_cases.json"
    with dataset.open("r", encoding="utf-8") as handle:
        return [EvalCase.model_validate(item) for item in json.load(handle)]


def run_offline_evaluation(
    *,
    cases: list[EvalCase] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """在相互隔离、仅追加的 SQLite 目录中运行每个评测 Case。"""

    dataset = cases or load_eval_cases()
    root = runtime_root or RUNTIME_ROOT / "eval"
    results: list[EvalCaseResult] = []
    for case in dataset:
        service = build_service(
            runtime_root=root / case.id,
            prefer_chroma=False,
            # 离线基准必须与开发者本机 .env 隔离，避免真实 LLM 的随机性、
            # API 费用或外部 PostgreSQL 状态污染固定回归结果。
            settings=RuntimeSettings(
                llm_backend="mock",
                database_backend="sqlite",
                checkpoint_backend="sqlite",
                embedding_backend="mock",
                knowledge_backend="local",
            ),
            forced_tool_statuses=case.forced_tool_statuses,
        )
        result = service.chat(
            ChatRequest(
                message=case.message,
                verified_subject_id=case.verified_subject_id,
            )
        )
        trace = service.get_trace(result.case_id)
        tool_events = [
            event["payload"]
            for event in trace
            if event["event_type"] == "tool.completed"
        ]
        actual_tools = [event["toolName"] for event in tool_events]
        planned = next(
            (
                event["payload"]
                for event in trace
                if event["event_type"] == "coordinator.planned"
            ),
            {"tasks": []},
        )
        actual_domains = [task["domain"] for task in planned["tasks"]]
        response = result.response
        response_evidence = {
            ref
            for statement in response.verified_facts
            for ref in statement.evidence_ids
        }
        produced_evidence = {
            evidence_id
            for event in tool_events
            for evidence_id in event.get("evidenceIds", [])
        }
        checks = {
            "routing": set(actual_domains) == set(case.expect.domains),
            "required_tools": set(case.expect.required_tools).issubset(actual_tools),
            "forbidden_tools": not set(case.expect.forbidden_tools).intersection(
                actual_tools
            ),
            "response_status": (
                case.expect.response_status is None
                or response.response_status == case.expect.response_status
            ),
            "handoff": (
                case.expect.handoff_required is None
                or response.handoff_required == case.expect.handoff_required
            ),
            "evidence_grounding": response_evidence.issubset(produced_evidence),
            "no_write_success_claim": not any(
                phrase in response.answer
                for phrase in ("已退款成功", "已为您改签", "补偿已发放")
            ),
            "no_duplicate_tool_signature": len(
                [
                    (
                        event.get("invocationId"),
                        event.get("toolName"),
                        tuple(event.get("argumentKeys", [])),
                    )
                    for event in tool_events
                ]
            )
            == len(
                set(
                    (
                        event.get("invocationId"),
                        event.get("toolName"),
                        tuple(event.get("argumentKeys", [])),
                    )
                    for event in tool_events
                )
            ),
        }
        results.append(
            EvalCaseResult(
                case_id=result.case_id,
                dataset_case_id=case.id,
                checks=checks,
                actual_tools=actual_tools,
                actual_domains=actual_domains,
                response_status=response.response_status,
            )
        )

    check_names = list(results[0].checks) if results else []
    metrics = {
        name: (
            sum(1 for result in results if result.checks[name]) / len(results)
            if results
            else 0.0
        )
        for name in check_names
    }
    return {
        "summary": {
            "caseCount": len(results),
            "passedCases": sum(1 for result in results if result.passed),
            "passRate": (
                sum(1 for result in results if result.passed) / len(results)
                if results
                else 0.0
            ),
        },
        "metrics": metrics,
        "cases": [result.model_dump() | {"passed": result.passed} for result in results],
    }
