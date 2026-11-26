"""WeChat Pay official bill adapter.

Supports the CSV export ("微信支付账单明细") and the same columns in XLSX.
The adapter is defensive: unknown status/type combinations are imported as
``pending_review`` instead of being guessed.
"""

from __future__ import annotations

import re

from ..core.constants import (
    REVIEW_REVERSAL_UNLINKED,
    SOURCE_WECHAT,
    Direction,
    TxnStatus,
    TxnType,
)
from ..core.dates import parse_datetime
from ..core.errors import ValidationError
from ..core.money import parse_money, to_minor
from ..core.logging import get_logger
from .base import AdapterResult, RawTable, RowError, make_draft
from .columns import find_column, get

log = get_logger("importers.wechat")

WECHAT_HEADER_MARKERS = ("交易时间", "交易类型", "交易对方", "收/支", "金额(元)")

_TYPE_MAP = {
    "商户消费": TxnType.EXPENSE,
    "扫二维码付款": TxnType.EXPENSE,
    "转账": TxnType.TRANSFER,
    "微信红包": TxnType.TRANSFER,
    "红包": TxnType.TRANSFER,
    "群收款": TxnType.TRANSFER,
    "信用卡还款": TxnType.CREDIT_CARD_REPAYMENT,
    "零钱提现": TxnType.WITHDRAWAL,
    "充值": TxnType.TOPUP,
    "退款": TxnType.REFUND,
    "投资收益": TxnType.INCOME,
    "零钱通": TxnType.TRANSFER,
    "理财通": TxnType.INVESTMENT_BUY,
}

_CARD_HINT_RE = re.compile(r"^(.*?)[（(](\d{4})[）)]$")


class WeChatAdapter:
    name = "wechat"
    platform = SOURCE_WECHAT

    def score(self, table: RawTable) -> float:
        headers = set(table.headers)
        hits = sum(1 for m in WECHAT_HEADER_MARKERS if m in headers)
        score = hits / len(WECHAT_HEADER_MARKERS)
        name = table.path.name.lower()
        if "微信" in name or "wechat" in name or "wx" in name:
            score += 0.3
        if any("微信支付账单" in line for line in table.preamble):
            score += 0.4
        return min(score, 1.0)

    def parse(self, table: RawTable, *, default_currency: str = "CNY") -> AdapterResult:
        col_time = find_column(table.headers, "date")
        col_type = find_column(table.headers, "type")
        col_counterparty = find_column(table.headers, "counterparty")
        col_desc = find_column(table.headers, "description")
        col_dir = find_column(table.headers, "direction")
        col_amount = find_column(table.headers, "amount")
        col_pay = find_column(table.headers, "payment") or _find_payment_col(table.headers)
        col_status = find_column(table.headers, "status")
        col_ext = find_column(table.headers, "external_id")
        col_currency = find_column(table.headers, "currency")
        if not (col_time and col_dir and col_amount):
            raise ValidationError(
                "微信账单缺少必要列：交易时间 / 收/支 / 金额(元)"
            )

        result = AdapterResult(platform=self.platform, total_rows=len(table.rows))
        result.meta["columns"] = {
            "time": col_time,
            "type": col_type,
            "counterparty": col_counterparty,
            "description": col_desc,
            "direction": col_dir,
            "amount": col_amount,
            "payment": col_pay,
            "status": col_status,
            "external_id": col_ext,
        }
        for idx, row in enumerate(table.rows, start=table.first_row_number()):
            try:
                draft = self._parse_row(
                    row,
                    idx=idx,
                    cols=result.meta["columns"],
                    default_currency=(get(row, col_currency) or default_currency).upper(),
                )
            except (ValidationError, ValueError) as exc:
                result.errors.append(RowError(idx, str(exc), raw=row))
                continue
            result.drafts.append(draft)
        return result

    def _parse_row(self, row, *, idx, cols, default_currency) -> object:
        time_text = get(row, cols["time"])
        if not time_text:
            raise ValidationError("空交易时间")
        occurred_at = parse_datetime(time_text)
        amount_text = get(row, cols["amount"])
        amount_minor = to_minor(parse_money(amount_text, currency=default_currency))
        if amount_minor == 0:
            raise ValidationError("金额为 0，已跳过")
        dir_text = get(row, cols["direction"])
        txn_text = get(row, cols["type"])
        status_text = get(row, cols["status"])
        pay_text = get(row, cols["payment"])
        description = get(row, cols["description"])
        counterparty = get(row, cols["counterparty"])
        external_id = get(row, cols["external_id"]) or None
        currency = default_currency

        direction, txn_type, status, review = self._classify(
            dir_text, txn_text, status_text
        )
        account_hint = "微信零钱"
        match = _CARD_HINT_RE.match(pay_text)
        if match:
            account_hint = f"微信-{match.group(1)}{match.group(2)}"
        elif "零钱通" in pay_text:
            account_hint = "微信零钱通"

        desc_parts = [p for p in (description, txn_text) if p]
        return make_draft(
            occurred_at=occurred_at,
            direction=direction,
            amount_minor=amount_minor,
            currency=currency,
            platform=self.platform,
            external_id=external_id,
            counterparty=counterparty or None,
            description=" / ".join(desc_parts) if desc_parts else None,
            txn_type=txn_type,
            status=status,
            review_reason=review,
            account_hint=account_hint,
            raw=row,
            row_number=idx,
        )

    def _classify(self, dir_text: str, txn_text: str, status_text: str):
        direction = Direction.OUTFLOW
        if dir_text in {"收入", "收"}:
            direction = Direction.INFLOW
        elif dir_text in {"不计收支", "不计入收支"}:
            direction = Direction.OUTFLOW
        txn_type = _TYPE_MAP.get(txn_text)
        if txn_type is None:
            for key, value in _TYPE_MAP.items():
                if key and key in txn_text:
                    txn_type = value
                    break
        if dir_text in {"不计收支", "不计入收支"}:
            # "不计收支" hides the wallet-relative direction; infer it from the
            # operation instead of guessing.
            if txn_type == TxnType.TOPUP or "充值" in txn_text:
                direction = Direction.INFLOW
            elif txn_type in {TxnType.CREDIT_CARD_REPAYMENT, TxnType.INVESTMENT_BUY}:
                direction = Direction.OUTFLOW
            elif "转出" in txn_text:
                direction = Direction.INFLOW
            else:
                direction = Direction.OUTFLOW
        review = None
        status = TxnStatus.CONFIRMED
        if "退款" in txn_text or "退款" in status_text:
            if direction == Direction.INFLOW:
                txn_type = TxnType.REFUND
            else:
                # The original purchase keeps its expense type; the inbound
                # refund row is linked by the reconciliation engine.
                txn_type = txn_type or TxnType.EXPENSE
        if dir_text in {"不计收支", "不计入收支"} and txn_type is None:
            txn_type = TxnType.UNKNOWN
            status = TxnStatus.PENDING_REVIEW
            review = "unknown_type"
        if txn_type is None:
            txn_type = TxnType.INCOME if direction == Direction.INFLOW else TxnType.EXPENSE
        if txn_type in {TxnType.TRANSFER, TxnType.TOPUP, TxnType.WITHDRAWAL, TxnType.CREDIT_CARD_REPAYMENT}:
            review = review or "internal_flow_candidate"
        if status_text and any(k in status_text for k in ("失败", "已关闭", "已撤销")):
            txn_type = TxnType.REVERSAL
            status = TxnStatus.PENDING_REVIEW
            review = REVIEW_REVERSAL_UNLINKED
        return direction, txn_type, status, review


def _find_payment_col(headers: list[str]) -> str | None:
    for header in headers:
        if "支付方式" in header:
            return header
    return None
