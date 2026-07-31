"""带版本的合成航司 System-of-Record Adapter。

设计映射
--------
- §15.7：JSON Fixture 布局与失败场景。
- §25.1：用户主体到业务记录的授权。

该 Adapter 有意模拟真实系统集成端口：Tool 依赖这里的方法，而不直接读取
JSON 文件。将此类替换为真实 PNR、客票或支付 Adapter 时，不需要修改
Graph 契约。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .paths import FIXTURE_ROOT


class FixtureAuthorizationError(PermissionError):
    """已认证主体不拥有所请求的业务记录。"""


class AirlineFixtureStore:
    """对版本库内合成记录提供只读索引视图。"""

    def __init__(self, root: Path = FIXTURE_ROOT) -> None:
        self.root = root
        manifest = self._read_json("manifest.json")
        self.dataset_version = manifest["datasetVersion"]
        self.flights = self._read_json("flights.json")
        self.bookings = self._read_json("bookings.json")
        self.tickets = self._read_json("tickets.json")
        self.payments = self._read_json("payments.json")
        self.refunds = self._read_json("refunds.json")

    def _read_json(self, filename: str) -> Any:
        with (self.root / filename).open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _one(records: Iterable[dict[str, Any]], **criteria: Any) -> dict[str, Any] | None:
        for record in records:
            if all(record.get(key) == value for key, value in criteria.items() if value is not None):
                return dict(record)
        return None

    @staticmethod
    def _authorized(record: dict[str, Any], subject_id: str | None) -> dict[str, Any]:
        if not subject_id or record.get("subjectId") != subject_id:
            raise FixtureAuthorizationError("The requested record is outside the verified subject scope")
        # ``subjectId`` 是授权属性，绝不能进入 LLM Context。
        sanitized = dict(record)
        sanitized.pop("subjectId", None)
        return sanitized

    def get_flight(self, flight_no: str, date: str) -> dict[str, Any] | None:
        """查询公开航班状态。"""

        return self._one(self.flights, flightNo=flight_no.upper(), date=date)

    def get_booking(self, pnr_ref: str, subject_id: str | None) -> dict[str, Any] | None:
        record = self._one(self.bookings, pnrRef=pnr_ref.upper())
        return None if record is None else self._authorized(record, subject_id)

    def get_tickets(
        self, ticket_refs: list[str], subject_id: str | None
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for ticket_ref in ticket_refs:
            record = self._one(self.tickets, ticketRef=ticket_ref.upper())
            if record is not None:
                found.append(self._authorized(record, subject_id))
        return found

    def get_refunds(
        self,
        *,
        subject_id: str | None,
        pnr_ref: str | None = None,
        ticket_ref: str | None = None,
        refund_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        candidates = [
            record
            for record in self.refunds
            if (
                (pnr_ref and record.get("pnrRef") == pnr_ref.upper())
                or (ticket_ref and record.get("ticketRef") == ticket_ref.upper())
                or (refund_ref and record.get("refundRef") == refund_ref.upper())
            )
        ]
        return [self._authorized(record, subject_id) for record in candidates]

    def get_payment(
        self,
        *,
        subject_id: str | None,
        pnr_ref: str | None = None,
        order_ref: str | None = None,
    ) -> dict[str, Any] | None:
        record = next(
            (
                item
                for item in self.payments
                if (pnr_ref and item.get("pnrRef") == pnr_ref.upper())
                or (order_ref and item.get("orderRef") == order_ref.upper())
            ),
            None,
        )
        return None if record is None else self._authorized(record, subject_id)
