"""命令行 Demo 和评测入口，参见设计 §21/§24。"""

from __future__ import annotations

import json

from .evaluation import run_offline_evaluation
from .models import ChatRequest
from .service import build_service


DEMO_MESSAGE = (
    "CZ3101 航班 2026-07-29 取消了，PNR AB12CD。"
    "其中一张票退款还没到账，另一张怎么办？请帮我退票。"
)


def run_demo() -> None:
    service = build_service()
    result = service.chat(ChatRequest(message=DEMO_MESSAGE))
    print(result.model_dump_json(indent=2))
    print("\n--- TRACE ---")
    print(json.dumps(service.get_trace(result.case_id), ensure_ascii=False, indent=2))


def run_eval() -> None:
    print(
        json.dumps(
            run_offline_evaluation(),
            ensure_ascii=False,
            indent=2,
        )
    )
