"""Conservative reconciliation: duplicates, transfers, refunds, repayments.

Nothing is deleted automatically.  Suspicious pairs are flagged for review,
transfers are only reclassified when both sides exist in the ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import (
    REVIEW_AMBIGUOUS_TRANSFER,
    REVIEW_DUPLICATE,
    REVIEW_PLATFORM_CLEARING,
    REVIEW_REFUND_UNLINKED,
    Direction,
    MatchStatus,
    TxnStatus,
    TxnType,
)
from ..core.ids import new_id
from ..core.logging import get_logger
from ..database.audit import record_audit
from ..database.init import get_household
from ..database.models import Account, Refund, Transaction, TransferMatch
from .service import normalize_text

log = get_logger("transactions.reconcile")

TRANSFER_HINTS = ("转账", "内部转账", "提现", "充值", "还款", "零钱通", "余额宝", "转入", "转出")
REFUND_HINTS = ("退款", "退货", "冲正", "撤销", "原路退回")
CLEARING_HINTS = ("财付通", "微信支付", "支付宝", "网联", "银联", "快捷支付")
FLOW_TYPES = {TxnType.EXPENSE, TxnType.INCOME, TxnType.REFUND, TxnType.FEE}


@dataclass
class ReconcileResult:
    duplicates_flagged: int = 0
    clearing_flagged: int = 0
    transfers_matched: int = 0
    refunds_linked: int = 0
    repayments_matched: int = 0
    review_items: int = 0
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "duplicates_flagged": self.duplicates_flagged,
            "clearing_flagged": self.clearing_flagged,
            "transfers_matched": self.transfers_matched,
            "refunds_linked": self.refunds_linked,
            "repayments_matched": self.repayments_matched,
            "review_items": self.review_items,
            "details": self.details[:100],
        }


class Reconciler:
    def __init__(self, session: Session, settings: Settings, *, period: str | None = None) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id
        self.period = period

    # ------------------------------------------------------------------ public
    def run(self) -> ReconcileResult:
        result = ReconcileResult()
        txns = self._transactions()
        result.duplicates_flagged += self._flag_cross_platform_duplicates(txns)
        result.clearing_flagged += self._flag_platform_clearing(txns)
        result.transfers_matched = self._match_transfers(txns, result)
        result.refunds_linked += self._link_refunds(txns, result)
        result.review_items = len(
            [t for t in self._transactions() if t.status == TxnStatus.PENDING_REVIEW]
        )
        record_audit(
            self.session,
            household_id=self.household_id,
            action="update",
            entity_type="reconciliation",
            entity_id=self.period or "all",
            after=result.as_dict(),
            detail="reconcile",
        )
        return result

    # ----------------------------------------------------------------- queries
    def _transactions(self) -> list[Transaction]:
        stmt = select(Transaction).where(
            Transaction.household_id == self.household_id,
            Transaction.is_deleted.is_(False),
            Transaction.status != TxnStatus.VOID,
        )
        if self.period:
            from ..core.dates import month_bounds

            start, end = month_bounds(self.period)
            stmt = stmt.where(
                Transaction.occurred_at >= datetime.combine(start, datetime.min.time()),
                Transaction.occurred_at <= datetime.combine(end, datetime.max.time().replace(microsecond=0)),
            )
        return list(self.session.scalars(stmt.order_by(Transaction.occurred_at.asc())))

    # -------------------------------------------------------------- duplicates
    def _flag_cross_platform_duplicates(self, txns: Iterable[Transaction]) -> int:
        window = timedelta(days=max(self.settings.dedupe_window_days, 1))
        expenses = [
            t
            for t in txns
            if t.txn_type in {TxnType.EXPENSE, TxnType.REFUND}
            and not t.is_deleted
            and t.status != TxnStatus.VOID
        ]
        grouped: dict[tuple[str, int], list[Transaction]] = {}
        for txn in expenses:
            grouped.setdefault((txn.currency, txn.amount_minor), []).append(txn)
        flagged = 0
        for group in grouped.values():
            if len(group) < 2:
                continue
            for i, first in enumerate(group):
                for second in group[i + 1 :]:
                    if first.source_platform == second.source_platform:
                        continue
                    if abs(first.occurred_at - second.occurred_at) > window:
                        continue
                    if first.direction != second.direction:
                        continue
                    if not self._merchant_similar(first, second):
                        continue
                    if first.duplicate_of_id or second.duplicate_of_id:
                        continue
                    # Flag the later/less trusted record; never delete.
                    # A bank row that merely clears a payment platform should
                    # lose to the detailed platform transaction, while a real
                    # bank/credit-card statement wins over a mirror.
                    def is_clearing(txn: Transaction) -> bool:
                        text = f"{txn.counterparty or ''} {txn.description or ''}"
                        return txn.source_platform in {"bank", "credit_card"} and any(
                            hint in text for hint in CLEARING_HINTS
                        )

                    if is_clearing(first) and not is_clearing(second):
                        keep, dup = second, first
                    elif is_clearing(second) and not is_clearing(first):
                        keep, dup = first, second
                    else:
                        keep, dup = (
                            (first, second)
                            if first.source_platform in {"bank", "credit_card"}
                            else (second, first)
                        )
                    dup.duplicate_of_id = keep.id
                    dup.stats_included = False
                    dup.status = TxnStatus.PENDING_REVIEW
                    dup.review_reason = REVIEW_DUPLICATE
                    flagged += 1
        if flagged:
            self.session.flush()
        return flagged

    def _merchant_similar(self, a: Transaction, b: Transaction) -> bool:
        na = normalize_text(a.counterparty) or normalize_text(a.description)
        nb = normalize_text(b.counterparty) or normalize_text(b.description)
        if not na or not nb:
            return False
        if na == nb:
            return True
        if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
            return True
        # A bank statement often shows "财付通-微信支付"; only accept when the
        # amount and the wallet description both reference the same merchant.
        tokens_a = {t for t in re.split(r"[\s\-/|]+", na) if len(t) >= 3}
        tokens_b = {t for t in re.split(r"[\s\-/|]+", nb) if len(t) >= 3}
        return bool(tokens_a & tokens_b)

    # --------------------------------------------------------- platform clearing
    def _flag_platform_clearing(self, txns: list[Transaction]) -> int:
        """Bank rows that merely settle a WeChat/Alipay purchase.

        If the corresponding platform transaction is present, keeping both
        would double count.  The bank row is flagged for review (never deleted)
        so the user can confirm it as the same payment.
        """
        window = timedelta(days=max(self.settings.dedupe_window_days, 1))
        platform_pool = list(txns)
        if self.period:
            from ..core.dates import month_bounds

            start, end = month_bounds(self.period)
            platform_pool.extend(
                self.session.scalars(
                    select(Transaction).where(
                        Transaction.household_id == self.household_id,
                        Transaction.is_deleted.is_(False),
                        Transaction.status != TxnStatus.VOID,
                        Transaction.source_platform.in_(["wechat", "alipay"]),
                        Transaction.occurred_at
                        >= datetime.combine(start, datetime.min.time()) - window,
                        Transaction.occurred_at
                        <= datetime.combine(end, datetime.max.time().replace(microsecond=0))
                        + window,
                    )
                )
            )
        platforms = [
            t
            for t in {x.id: x for x in platform_pool}.values()
            if t.source_platform in {"wechat", "alipay"}
            and t.txn_type in {TxnType.EXPENSE, TxnType.REFUND}
            and not t.duplicate_of_id
        ]
        flagged = 0
        for bank in txns:
            if bank.source_platform not in {"bank", "credit_card"} or bank.duplicate_of_id:
                continue
            if bank.txn_type not in {TxnType.EXPENSE, TxnType.REFUND}:
                continue
            text = f"{bank.counterparty or ''} {bank.description or ''}"
            if not any(hint in text for hint in CLEARING_HINTS):
                continue
            for platform in platforms:
                if platform.direction != bank.direction:
                    continue
                if platform.amount_minor != bank.amount_minor or platform.currency != bank.currency:
                    continue
                if abs(platform.occurred_at - bank.occurred_at) > window:
                    continue
                bank.duplicate_of_id = platform.id
                bank.stats_included = False
                bank.status = TxnStatus.PENDING_REVIEW
                bank.review_reason = REVIEW_PLATFORM_CLEARING
                flagged += 1
                break
        if flagged:
            self.session.flush()
        return flagged

    # ----------------------------------------------------------------- transfers
    def _match_transfers(self, txns: list[Transaction], result: ReconcileResult) -> int:
        window = timedelta(days=max(self.settings.transfer_window_days, 1))
        if self.period:
            # A transfer can straddle midnight / a month boundary.  Include a
            # window around the period and deduplicate by id.
            from ..core.dates import month_bounds

            start, end = month_bounds(self.period)
            extra = list(
                self.session.scalars(
                    select(Transaction).where(
                        Transaction.household_id == self.household_id,
                        Transaction.is_deleted.is_(False),
                        Transaction.status != TxnStatus.VOID,
                        Transaction.occurred_at
                        >= datetime.combine(start, datetime.min.time()) - window,
                        Transaction.occurred_at
                        <= datetime.combine(end, datetime.max.time().replace(microsecond=0))
                        + window,
                    )
                )
            )
            merged = {t.id: t for t in extra}
            merged.update({t.id: t for t in txns})
            txns = list(merged.values())
        outflows = [t for t in txns if t.direction == Direction.OUTFLOW and t.txn_type in FLOW_TYPES | {TxnType.TRANSFER, TxnType.CREDIT_CARD_REPAYMENT, TxnType.TOPUP, TxnType.WITHDRAWAL}]
        inflows = [t for t in txns if t.direction == Direction.INFLOW and t.txn_type in FLOW_TYPES | {TxnType.TRANSFER, TxnType.TOPUP, TxnType.WITHDRAWAL}]
        used_out: set[str] = set()
        used_in: set[str] = set()
        matched = 0
        for out in outflows:
            if out.id in used_out or out.transfer_group_id:
                continue
            for inflow in inflows:
                if inflow.id in used_in or inflow.transfer_group_id:
                    continue
                if inflow.account_id == out.account_id:
                    continue
                if inflow.amount_minor != out.amount_minor or inflow.currency != out.currency:
                    continue
                if abs(inflow.occurred_at - out.occurred_at) > window:
                    continue
                # Both sides must look like an internal movement.  One transfer
                # hint on a salary/rent pair is not enough and would silently
                # remove real income and expense.
                if not (self._is_transfer_like(out) and self._is_transfer_like(inflow)):
                    continue
                group = new_id("txn") if not out.transfer_group_id else out.transfer_group_id
                out.transfer_group_id = group
                inflow.transfer_group_id = group
                target_account = self.session.get(Account, inflow.account_id)
                is_card = target_account is not None and target_account.account_type == "credit_card"
                out.txn_type = TxnType.CREDIT_CARD_REPAYMENT if is_card else TxnType.TRANSFER
                inflow.txn_type = TxnType.TRANSFER
                out.stats_included = False
                inflow.stats_included = False
                out.status = TxnStatus.CONFIRMED
                inflow.status = TxnStatus.CONFIRMED
                internal_reasons = {REVIEW_AMBIGUOUS_TRANSFER, "internal_flow_candidate"}
                if out.review_reason in internal_reasons:
                    out.review_reason = None
                if inflow.review_reason in internal_reasons:
                    inflow.review_reason = None
                self.session.add(
                    TransferMatch(
                        id=new_id("transfer"),
                        household_id=self.household_id,
                        outflow_txn_id=out.id,
                        inflow_txn_id=inflow.id,
                        amount_minor=out.amount_minor,
                        currency=out.currency,
                        confidence_bps=9000 if self._is_transfer_like(out) and self._is_transfer_like(inflow) else 7000,
                        match_method="amount+window+hint",
                        status=MatchStatus.CONFIRMED,
                    )
                )
                used_out.add(out.id)
                used_in.add(inflow.id)
                matched += 1
                if is_card:
                    result.repayments_matched += 1
                result.details.append(
                    {
                        "kind": "transfer",
                        "outflow": out.id,
                        "inflow": inflow.id,
                        "amount_minor": out.amount_minor,
                    }
                )
                break
        self.session.flush()
        return matched

    def _is_transfer_like(self, txn: Transaction) -> bool:
        text = f"{txn.counterparty or ''} {txn.description or ''}"
        return txn.txn_type in {
            TxnType.TRANSFER,
            TxnType.CREDIT_CARD_REPAYMENT,
            TxnType.TOPUP,
            TxnType.WITHDRAWAL,
            TxnType.INVESTMENT_BUY,
            TxnType.INVESTMENT_SELL,
        } or any(hint in text for hint in TRANSFER_HINTS)

    # ------------------------------------------------------------------- refunds
    def _link_refunds(self, txns: list[Transaction], result: ReconcileResult) -> int:
        linked = 0
        already = {
            r.refund_txn_id
            for r in self.session.scalars(
                select(Refund).where(Refund.household_id == self.household_id)
            )
        }
        candidates = [
            t
            for t in txns
            if t.direction == Direction.INFLOW
            and t.id not in already
            and (
                t.txn_type == TxnType.REFUND
                or any(hint in f"{t.counterparty or ''} {t.description or ''}" for hint in REFUND_HINTS)
            )
        ]
        if not candidates:
            return 0
        earliest = min(t.occurred_at for t in candidates) - timedelta(days=90)
        latest = max(t.occurred_at for t in candidates)
        expenses = list(
            self.session.scalars(
                select(Transaction).where(
                    Transaction.household_id == self.household_id,
                    Transaction.is_deleted.is_(False),
                    Transaction.status != TxnStatus.VOID,
                    Transaction.stats_included.is_(True),
                    Transaction.direction == Direction.OUTFLOW,
                    Transaction.txn_type.in_([TxnType.EXPENSE, TxnType.FEE]),
                    Transaction.occurred_at >= earliest,
                    Transaction.occurred_at <= latest,
                )
            )
        )
        for refund in candidates:
            best: tuple[float, Transaction] | None = None
            for original in expenses:
                if original.occurred_at > refund.occurred_at:
                    continue
                if (refund.occurred_at - original.occurred_at).days > 90:
                    continue
                if refund.amount_minor > original.amount_minor:
                    continue
                similarity = self._merchant_similar(refund, original)
                amount_score = 1.0 if refund.amount_minor == original.amount_minor else (
                    refund.amount_minor / original.amount_minor
                )
                hint_score = 1.0 if any(
                    h in f"{refund.counterparty or ''} {refund.description or ''}" for h in REFUND_HINTS
                ) else 0.0
                days = (refund.occurred_at - original.occurred_at).days
                if similarity:
                    score = amount_score * 0.5 + 0.3 + hint_score * 0.2 - days * 0.001
                else:
                    # Without a merchant match this is only a weak candidate;
                    # keep it below the auto-confirm threshold.
                    score = amount_score * 0.2 + hint_score * 0.2 - days * 0.001
                if hint_score:
                    if best is None or score > best[0]:
                        best = (score, original)
            if best is None:
                refund.status = TxnStatus.PENDING_REVIEW
                refund.review_reason = REVIEW_REFUND_UNLINKED
                continue
            score, original = best
            refund.txn_type = TxnType.REFUND
            refund.refund_of_id = original.id
            if original.category_id and not refund.category_id:
                refund.category_id = original.category_id
            if (
                original.status == TxnStatus.PENDING_REVIEW
                and original.review_reason == REVIEW_REFUND_UNLINKED
            ):
                original.status = TxnStatus.CONFIRMED
                original.review_reason = None
            refund.stats_included = True
            refund.status = TxnStatus.CONFIRMED if score >= 0.7 else TxnStatus.PENDING_REVIEW
            refund.review_reason = None if score >= 0.7 else REVIEW_REFUND_UNLINKED
            self.session.add(
                Refund(
                    id=new_id("refund"),
                    household_id=self.household_id,
                    refund_txn_id=refund.id,
                    original_txn_id=original.id,
                    amount_minor=refund.amount_minor,
                    currency=refund.currency,
                    method="amount+merchant+window",
                    confidence_bps=int(max(0.0, min(1.0, score)) * 10000),
                    status=MatchStatus.CONFIRMED if score >= 0.7 else MatchStatus.PROPOSED,
                )
            )
            linked += 1
            result.details.append(
                {
                    "kind": "refund",
                    "refund": refund.id,
                    "original": original.id,
                    "amount_minor": refund.amount_minor,
                    "confidence_bps": int(max(0.0, min(1.0, score)) * 10000),
                }
            )
        self.session.flush()
        return linked

    # ------------------------------------------------------------------ review
    def confirm_duplicate(self, txn_id: str, *, distinct: bool = False, actor: str = "cli") -> Transaction:
        txn = self.session.get(Transaction, txn_id)
        if txn is None:
            raise ValueError(f"交易不存在: {txn_id}")
        before = {"status": txn.status, "stats_included": txn.stats_included}
        if distinct:
            txn.duplicate_of_id = None
            txn.status = TxnStatus.CONFIRMED
            txn.review_reason = None
            txn.stats_included = True
        else:
            txn.status = TxnStatus.VOID
            txn.stats_included = False
            txn.review_reason = REVIEW_DUPLICATE
        record_audit(
            self.session,
            household_id=self.household_id,
            action="update",
            entity_type="transaction",
            entity_id=txn.id,
            before=before,
            after={"status": txn.status, "stats_included": txn.stats_included},
            detail="confirm_duplicate" if not distinct else "confirm_distinct",
            actor=actor,
        )
        self.session.flush()
        return txn
