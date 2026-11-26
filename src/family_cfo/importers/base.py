"""Bill adapter contracts.

Every platform gets its own adapter.  Adapters only *parse*; they never touch
the database, so they are trivially testable with fixture files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.constants import Direction, TxnStatus, TxnType


@dataclass
class RawTable:
    """A tabular bill normalised to a header row plus string rows."""

    path: Path
    kind: str  # csv | xlsx | xls
    headers: list[str]
    rows: list[dict[str, Any]]
    preamble: list[str] = field(default_factory=list)
    encoding: str | None = None
    sheet: str | None = None
    header_row: int = 0  # 1-based row number of the header in the source file

    def header_text(self) -> str:
        return "|".join(self.headers)

    def has_any(self, *names: str) -> bool:
        joined = self.header_text()
        return any(name in joined for name in names)

    def first_row_number(self) -> int:
        return self.header_row + 1 if self.header_row else 2


@dataclass
class DraftTransaction:
    occurred_at: datetime
    direction: str
    amount_minor: int
    currency: str
    external_id: str | None
    counterparty: str | None
    description: str | None
    txn_type: str
    platform: str
    status: str = TxnStatus.CONFIRMED
    review_reason: str | None = None
    account_hint: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    row_number: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "occurred_at": self.occurred_at.isoformat(sep=" "),
            "direction": self.direction,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "external_id": self.external_id,
            "counterparty": self.counterparty,
            "description": self.description,
            "txn_type": self.txn_type,
            "platform": self.platform,
            "status": self.status,
            "review_reason": self.review_reason,
            "account_hint": self.account_hint,
            "row_number": self.row_number,
            "raw": self.raw,
        }


@dataclass
class RowError:
    row_number: int
    message: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResult:
    platform: str
    drafts: list[DraftTransaction] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    total_rows: int = 0
    account_hint: str | None = None
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BillAdapter(Protocol):
    name: str
    platform: str

    def score(self, table: RawTable) -> float:
        """Confidence in [0, 1] that this adapter understands the table."""

    def parse(self, table: RawTable, *, default_currency: str = "CNY") -> AdapterResult:
        ...


def make_draft(
    *,
    occurred_at: datetime,
    direction: str,
    amount_minor: int,
    currency: str,
    platform: str,
    external_id: str | None = None,
    counterparty: str | None = None,
    description: str | None = None,
    txn_type: str | None = None,
    status: str = TxnStatus.CONFIRMED,
    review_reason: str | None = None,
    account_hint: str | None = None,
    raw: dict[str, Any] | None = None,
    row_number: int = 0,
) -> DraftTransaction:
    if amount_minor < 0:
        raise ValueError("amount_minor must be non-negative; use direction")
    if direction not in (Direction.INFLOW, Direction.OUTFLOW):
        raise ValueError(f"invalid direction: {direction}")
    if txn_type is None:
        txn_type = TxnType.INCOME if direction == Direction.INFLOW else TxnType.EXPENSE
    return DraftTransaction(
        occurred_at=occurred_at,
        direction=direction,
        amount_minor=amount_minor,
        currency=currency,
        external_id=external_id,
        counterparty=counterparty,
        description=description,
        txn_type=txn_type,
        platform=platform,
        status=status,
        review_reason=review_reason,
        account_hint=account_hint,
        raw=raw or {},
        row_number=row_number,
    )
