"""Generic bank statement adapter.

Chinese banks export wildly different layouts, so this adapter only claims high
confidence when it recognises the common debit/credit or income/expense
structures.  Otherwise the caller must confirm a column mapping.
"""

from __future__ import annotations

from ..core.constants import (
    REVIEW_AMBIGUOUS_TRANSFER,
    SOURCE_BANK,
    Direction,
    TxnStatus,
    TxnType,
)
from ..core.dates import parse_datetime
from ..core.errors import MappingRequiredError, ValidationError
from ..core.money import parse_money, to_minor
from .base import AdapterResult, RawTable, RowError, make_draft
from .columns import find_column, get, suggest_mapping

INFLOW_WORDS = ("贷", "收", "存", "入", "credit", "in")
OUTFLOW_WORDS = ("借", "付", "支", "出", "debit", "out")


class BankStatementAdapter:
    name = "bank"
    platform = SOURCE_BANK

    def score(self, table: RawTable) -> float:
        headers = table.headers
        has_date = find_column(headers, "date") is not None
        has_amount = (
            find_column(headers, "amount") is not None
            or find_column(headers, "inflow") is not None
            or find_column(headers, "outflow") is not None
        )
        has_direction = find_column(headers, "direction") is not None
        has_bankish = any(
            any(token in h for token in ("对方户名", "对方账户", "摘要", "余额", "借贷标志", "用途"))
            for h in headers
        )
        if not (has_date and has_amount):
            return 0.0
        score = 0.2
        if has_direction:
            score += 0.2
        if has_bankish:
            score += 0.3
        name = table.path.name.lower()
        if any(k in name for k in ("银行", "bank", "流水", "对账单", "明细")):
            score += 0.15
        return min(score, 1.0)

    def parse(self, table: RawTable, *, default_currency: str = "CNY") -> AdapterResult:
        cols = {
            "date": _pick_date_col(table.headers),
            "amount": find_column(table.headers, "amount"),
            "inflow": find_column(table.headers, "inflow"),
            "outflow": find_column(table.headers, "outflow"),
            "direction": find_column(table.headers, "direction"),
            "counterparty": find_column(table.headers, "counterparty"),
            "description": find_column(table.headers, "description"),
            "external_id": find_column(table.headers, "external_id"),
            "status": find_column(table.headers, "status"),
            "currency": find_column(table.headers, "currency"),
            "balance": find_column(table.headers, "balance"),
        }
        if not cols["date"] or not (cols["amount"] or cols["inflow"] or cols["outflow"]):
            raise MappingRequiredError(
                "银行账单列名无法自动识别，请确认字段映射后重试。",
                suggestion=suggest_mapping(table.headers),
            )
        result = AdapterResult(platform=self.platform, total_rows=len(table.rows))
        result.meta["columns"] = cols
        for idx, row in enumerate(table.rows, start=table.first_row_number()):
            try:
                draft = self._parse_row(
                    row,
                    idx=idx,
                    cols=cols,
                    default_currency=(get(row, cols["currency"]) or default_currency).upper(),
                )
            except (ValidationError, ValueError) as exc:
                result.errors.append(RowError(idx, str(exc), raw=row))
                continue
            result.drafts.append(draft)
        return result

    def _parse_row(self, row, *, idx, cols, default_currency):
        date_text = get(row, cols["date"])
        if not date_text:
            raise ValidationError("空交易日期")
        occurred_at = parse_datetime(date_text)
        direction, amount_minor = self._direction_and_amount(row, cols, default_currency)
        if amount_minor == 0:
            raise ValidationError("金额为 0，已跳过")
        description = get(row, cols["description"])
        counterparty = get(row, cols["counterparty"])
        txn_type = _infer_type(direction, counterparty, description)
        review = REVIEW_AMBIGUOUS_TRANSFER if txn_type == TxnType.TRANSFER else None
        return make_draft(
            occurred_at=occurred_at,
            direction=direction,
            amount_minor=amount_minor,
            currency=default_currency,
            platform=self.platform,
            external_id=get(row, cols["external_id"]) or None,
            counterparty=counterparty or None,
            description=description or None,
            txn_type=txn_type,
            status=TxnStatus.CONFIRMED,
            review_reason=review,
            raw=row,
            row_number=idx,
        )

    def _direction_and_amount(self, row, cols, currency: str) -> tuple[str, int]:
        if cols["inflow"] or cols["outflow"]:
            inflow_text = get(row, cols["inflow"])
            outflow_text = get(row, cols["outflow"])
            inflow = _try_amount(inflow_text, currency)
            outflow = _try_amount(outflow_text, currency)
            if inflow and outflow:
                raise ValidationError("同一行同时存在收入与支出金额")
            if inflow:
                return Direction.INFLOW, inflow
            if outflow:
                return Direction.OUTFLOW, outflow
            raise ValidationError("收入与支出金额均为空")
        raw_amount = get(row, cols["amount"])
        if not raw_amount:
            raise ValidationError("金额为空")
        dec = parse_money(raw_amount, currency=currency, allow_negative=True)
        amount = to_minor(abs(dec))
        direction_text = get(row, cols["direction"]).lower()
        if direction_text:
            if any(w in direction_text for w in OUTFLOW_WORDS) and not any(
                w in direction_text for w in ("贷",)
            ):
                return Direction.OUTFLOW, amount
            if any(w in direction_text for w in ("贷", "收", "存", "入")):
                return Direction.INFLOW, amount
        return (Direction.OUTFLOW if dec < 0 else Direction.INFLOW), amount


def _try_amount(text: str, currency: str) -> int:
    if not text or text in {"0", "0.00", "--", "-"}:
        return 0
    try:
        return to_minor(abs(parse_money(text, currency=currency)))
    except ValidationError:
        return 0


def _pick_date_col(headers: list[str]) -> str | None:
    for name in ("交易日期", "记账日期", "入账日期", "发生日期", "日期", "交易时间"):
        if name in headers:
            return name
    return find_column(headers, "date")


def _infer_type(direction: str, counterparty: str, description: str) -> str:
    text = f"{counterparty} {description}"
    if any(k in text for k in ("房贷", "车贷", "贷款还款")):
        return TxnType.LOAN_REPAY
    if any(k in text for k in ("信用卡还款", "信用卡", "还款")):
        return TxnType.CREDIT_CARD_REPAYMENT
    if any(k in text for k in ("贷款", "放款", "借款")):
        return TxnType.LOAN_OUT if direction == Direction.INFLOW else TxnType.LOAN_REPAY
    if any(k in text for k in ("工资", "薪资", "奖金", "利息", "分红")):
        return TxnType.INCOME
    if any(k in text for k in ("转账", "汇入", "汇出", "内部")):
        return TxnType.TRANSFER
    if any(k in text for k in ("理财", "基金", "证券", "股票")):
        return TxnType.INVESTMENT_BUY if direction == Direction.OUTFLOW else TxnType.INVESTMENT_SELL
    return TxnType.EXPENSE if direction == Direction.OUTFLOW else TxnType.INCOME
