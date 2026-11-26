"""Alipay official bill adapter (CSV/XLSX export)."""

from __future__ import annotations

from ..core.constants import (
    REVIEW_REFUND_UNLINKED,
    SOURCE_ALIPAY,
    Direction,
    TxnStatus,
    TxnType,
)
from ..core.dates import parse_datetime
from ..core.errors import ValidationError
from ..core.money import parse_money, to_minor
from .base import AdapterResult, RawTable, RowError, make_draft
from .columns import find_column, get

ALIPAY_MARKERS = ("交易号", "商家订单号", "商品名称", "收/支")

_TYPE_MAP = {
    "即时到账交易": TxnType.EXPENSE,
    "转账": TxnType.TRANSFER,
    "退款": TxnType.REFUND,
    "提现": TxnType.WITHDRAWAL,
    "充值": TxnType.TOPUP,
    "信用卡还款": TxnType.CREDIT_CARD_REPAYMENT,
    "花呗还款": TxnType.CREDIT_CARD_REPAYMENT,
    "余额宝-转入": TxnType.INVESTMENT_BUY,
    "余额宝-转出": TxnType.INVESTMENT_SELL,
    "理财买入": TxnType.INVESTMENT_BUY,
    "理财卖出": TxnType.INVESTMENT_SELL,
}


class AlipayAdapter:
    name = "alipay"
    platform = SOURCE_ALIPAY

    def score(self, table: RawTable) -> float:
        headers = set(table.headers)
        hits = sum(1 for m in ALIPAY_MARKERS if m in headers)
        score = hits / len(ALIPAY_MARKERS) * 0.8
        name = table.path.name.lower()
        if "支付宝" in name or "alipay" in name:
            score += 0.3
        if any("支付宝" in line for line in table.preamble):
            score += 0.4
        if "交易创建时间" in headers and "付款时间" in headers:
            score += 0.2
        return min(score, 1.0)

    def parse(self, table: RawTable, *, default_currency: str = "CNY") -> AdapterResult:
        col_time = _pick_date_col(table.headers)
        col_dir = find_column(table.headers, "direction")
        col_amount = find_column(table.headers, "amount")
        if not (col_time and col_dir and col_amount):
            raise ValidationError("支付宝账单缺少必要列：交易时间 / 收/支 / 金额")
        cols = {
            "time": col_time,
            "type": find_column(table.headers, "type"),
            "counterparty": find_column(table.headers, "counterparty"),
            "description": find_column(table.headers, "description"),
            "direction": col_dir,
            "amount": col_amount,
            "payment": _find_payment_col(table.headers),
            "status": find_column(table.headers, "status"),
            "external_id": find_column(table.headers, "external_id"),
            "currency": find_column(table.headers, "currency"),
            "refunded": _find_refunded_col(table.headers),
        }
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
        time_text = get(row, cols["time"])
        if not time_text:
            raise ValidationError("空交易时间")
        occurred_at = parse_datetime(time_text)
        amount_minor = to_minor(parse_money(get(row, cols["amount"]), currency=default_currency))
        if amount_minor == 0:
            raise ValidationError("金额为 0")
        dir_text = get(row, cols["direction"])
        type_text = get(row, cols["type"])
        status_text = get(row, cols["status"])
        description = get(row, cols["description"])
        counterparty = get(row, cols["counterparty"])
        payment = get(row, cols["payment"])
        external_id = get(row, cols["external_id"]) or None
        refunded_text = get(row, cols["refunded"])

        if "关闭" in status_text or "失败" in status_text:
            status = TxnStatus.VOID
        else:
            status = TxnStatus.CONFIRMED
        direction = (
            Direction.INFLOW if dir_text in {"收入", "收"} else Direction.OUTFLOW
        )
        txn_type = _TYPE_MAP.get(type_text)
        if txn_type is None:
            for key, value in _TYPE_MAP.items():
                if key and key in type_text:
                    txn_type = value
                    break
        review = None
        if "退款" in type_text or "退款" in status_text:
            if direction == Direction.INFLOW:
                txn_type = TxnType.REFUND
            elif txn_type is None:
                txn_type = TxnType.EXPENSE
        if refunded_text:
            try:
                refunded_minor = to_minor(parse_money(refunded_text, currency=default_currency))
            except ValidationError:
                refunded_minor = 0
            if refunded_minor > 0:
                note = f"导出显示成功退款 ¥{refunded_minor / 100:.2f}"
                description = f"{description} ({note})" if description else note
                review = review or REVIEW_REFUND_UNLINKED
                status = TxnStatus.PENDING_REVIEW
        if dir_text in {"不计收支", "不计入收支"}:
            if txn_type is None:
                txn_type = TxnType.UNKNOWN
                status = TxnStatus.PENDING_REVIEW
                review = review or "unknown_type"
        if txn_type is None:
            txn_type = TxnType.INCOME if direction == Direction.INFLOW else TxnType.EXPENSE
        account_hint = "支付宝余额"
        if "花呗" in payment:
            account_hint = "支付宝花呗"
        elif "余额宝" in payment:
            account_hint = "支付宝余额宝"
        elif "银行卡" in payment:
            account_hint = "支付宝-银行卡"
        return make_draft(
            occurred_at=occurred_at,
            direction=direction,
            amount_minor=amount_minor,
            currency=default_currency,
            platform=self.platform,
            external_id=external_id,
            counterparty=counterparty or None,
            description=description or None,
            txn_type=txn_type,
            status=status,
            review_reason=review,
            account_hint=account_hint,
            raw=row,
            row_number=idx,
        )


def _pick_date_col(headers: list[str]) -> str | None:
    for name in ("付款时间", "交易时间", "交易创建时间", "创建时间", "交易日期"):
        if name in headers:
            return name
    return find_column(headers, "date")


def _find_payment_col(headers: list[str]) -> str | None:
    for name in ("收/付款方式", "付款方式", "收付款方式"):
        if name in headers:
            return name
    for header in headers:
        if "付款方式" in header:
            return header
    return None


def _find_refunded_col(headers: list[str]) -> str | None:
    for header in headers:
        if "成功退款" in header:
            return header
    return None
