"""Mapping-driven adapter for any other platform / bank export."""

from __future__ import annotations

from typing import Any

from ..core.constants import (
    REVIEW_AMBIGUOUS_TRANSFER,
    SOURCE_OTHER,
    Direction,
    TxnStatus,
    TxnType,
)
from ..core.dates import parse_datetime
from ..core.errors import MappingRequiredError, ValidationError
from ..core.money import parse_money, to_minor
from .base import AdapterResult, RawTable, RowError, make_draft
from .columns import get, suggest_mapping

VALID_FIELDS = {
    "date", "amount", "inflow", "outflow", "direction", "counterparty",
    "description", "external_id", "status", "currency", "type", "balance",
}


class MappingAdapter:
    """Parses a table using a user-confirmed ``{field: column}`` mapping."""

    name = "mapping"
    platform = SOURCE_OTHER

    def __init__(self, mapping: dict[str, str], *, platform: str = SOURCE_OTHER) -> None:
        unknown = set(mapping) - VALID_FIELDS
        if unknown:
            raise ValidationError(f"未知映射字段: {sorted(unknown)}")
        if "date" not in mapping:
            raise ValidationError("映射缺少 date")
        if not ({"amount", "inflow", "outflow"} & set(mapping)):
            raise ValidationError("映射缺少 amount / inflow / outflow")
        self.mapping = mapping
        self.platform = platform

    def score(self, table: RawTable) -> float:
        return 0.05

    def parse(self, table: RawTable, *, default_currency: str = "CNY") -> AdapterResult:
        missing_cols = [c for c in self.mapping.values() if c not in table.headers]
        if missing_cols:
            raise MappingRequiredError(
                "映射引用了不存在的列，请重新确认。",
                suggestion=suggest_mapping(table.headers),
            )
        result = AdapterResult(platform=self.platform, total_rows=len(table.rows))
        result.meta["columns"] = dict(self.mapping)
        for idx, row in enumerate(table.rows, start=table.first_row_number()):
            try:
                result.drafts.append(
                    self._parse_row(
                        row,
                        idx=idx,
                        default_currency=(get(row, self.mapping.get("currency")) or default_currency).upper(),
                    )
                )
            except (ValidationError, ValueError) as exc:
                result.errors.append(RowError(idx, str(exc), raw=row))
        return result

    def _parse_row(self, row: dict[str, Any], *, idx: int, default_currency: str) -> object:
        date_text = get(row, self.mapping["date"])
        if not date_text:
            raise ValidationError("空日期")
        occurred_at = parse_datetime(date_text)
        direction, amount_minor = self._direction_amount(row, default_currency)
        if amount_minor == 0:
            raise ValidationError("金额为 0")
        status = TxnStatus.CONFIRMED
        status_text = get(row, self.mapping.get("status"))
        if status_text and any(k in status_text for k in ("失败", "关闭", "撤销")):
            status = TxnStatus.VOID
        type_text = get(row, self.mapping.get("type"))
        txn_type = _map_type(type_text, direction)
        description = get(row, self.mapping.get("description")) or None
        return make_draft(
            occurred_at=occurred_at,
            direction=direction,
            amount_minor=amount_minor,
            currency=default_currency,
            platform=self.platform,
            external_id=get(row, self.mapping.get("external_id")) or None,
            counterparty=get(row, self.mapping.get("counterparty")) or None,
            description=description,
            txn_type=txn_type,
            status=status,
            review_reason=REVIEW_AMBIGUOUS_TRANSFER if txn_type == TxnType.TRANSFER else None,
            raw=row,
            row_number=idx,
        )

    def _direction_amount(self, row, currency) -> tuple[str, int]:
        if self.mapping.get("inflow") or self.mapping.get("outflow"):
            inflow = _amount(get(row, self.mapping.get("inflow")), currency)
            outflow = _amount(get(row, self.mapping.get("outflow")), currency)
            if inflow and outflow:
                raise ValidationError("同一行同时存在收入与支出")
            if inflow:
                return Direction.INFLOW, inflow
            if outflow:
                return Direction.OUTFLOW, outflow
            raise ValidationError("收入/支出金额为空")
        dec = parse_money(get(row, self.mapping["amount"]), currency=currency)
        amount = to_minor(abs(dec))
        text = get(row, self.mapping.get("direction"))
        if any(k in text for k in ("收入", "收", "贷", "入")):
            return Direction.INFLOW, amount
        if any(k in text for k in ("支出", "支", "借", "出")):
            return Direction.OUTFLOW, amount
        return (Direction.OUTFLOW if dec < 0 else Direction.INFLOW), amount


def _amount(text: str, currency: str) -> int:
    if not text or text in {"0", "0.00", "--", "-"}:
        return 0
    try:
        return to_minor(abs(parse_money(text, currency=currency)))
    except ValidationError:
        return 0


def _map_type(text: str, direction: str) -> str:
    table = {
        "转账": TxnType.TRANSFER,
        "还款": TxnType.CREDIT_CARD_REPAYMENT,
        "退款": TxnType.REFUND,
        "工资": TxnType.INCOME,
        "提现": TxnType.WITHDRAWAL,
        "充值": TxnType.TOPUP,
        "理财": TxnType.INVESTMENT_BUY,
    }
    for key, value in table.items():
        if key in text:
            return value
    return TxnType.EXPENSE if direction == Direction.OUTFLOW else TxnType.INCOME
