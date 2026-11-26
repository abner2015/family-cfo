"""Data integrity checks.

Each check returns a row with a severity so the CLI / settlement engine can
decide whether the ledger is safe to close.  Checks never modify data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..core.constants import SettlementStatus, TxnStatus
from .models import (
    Household,
    Account,
    Asset,
    AssetValuation,
    ImportBatch,
    Liability,
    RawTransaction,
    Refund,
    Settlement,
    Transaction,
    TransferMatch,
)


@dataclass
class CheckResult:
    name: str
    severity: str  # ok | info | warning | error
    count: int
    detail: str = ""
    sample: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.severity not in {"error"}


@dataclass
class IntegrityReport:
    checks: list[CheckResult]
    generated_at: datetime

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def errors(self) -> list[CheckResult]:
        return [c for c in self.checks if c.severity == "error"]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.severity == "warning"]

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "checks": [
                {
                    "name": c.name,
                    "severity": c.severity,
                    "count": c.count,
                    "detail": c.detail,
                    "sample": c.sample,
                }
                for c in self.checks
            ],
        }


def run_integrity_checks(session: Session, household_id: str, *, period: str | None = None) -> IntegrityReport:
    checks: list[CheckResult] = []

    fk_rows = session.execute(text("PRAGMA foreign_key_check")).fetchall()
    checks.append(
        CheckResult(
            "foreign_keys",
            "error" if fk_rows else "ok",
            len(fk_rows),
            "外键约束检查" if not fk_rows else "存在悬空外键",
            [str(r) for r in fk_rows[:5]],
        )
    )

    txns = session.scalars(
        select(Transaction).where(
            Transaction.household_id == household_id, Transaction.is_deleted.is_(False)
        )
    ).all()

    household = session.get(Household, household_id)
    base_currency = household.base_currency if household else "CNY"
    mismatch = [
        t
        for t in txns
        if t.currency == base_currency and t.amount_minor != t.amount_base_minor
    ]
    checks.append(
        CheckResult(
            "base_amount_consistency",
            "error" if mismatch else "ok",
            len(mismatch),
            "本币交易的原币金额与折算金额一致",
            [t.id for t in mismatch[:5]],
        )
    )

    negative = [t for t in txns if t.amount_minor < 0]
    checks.append(
        CheckResult(
            "non_negative_amounts",
            "error" if negative else "ok",
            len(negative),
            "金额以正数存储，方向由 direction 表达",
            [t.id for t in negative[:5]],
        )
    )

    bad_direction = [
        t
        for t in txns
        if t.amount_minor > 0
        and t.direction not in {"inflow", "outflow"}
    ]
    checks.append(
        CheckResult(
            "direction_values",
            "error" if bad_direction else "ok",
            len(bad_direction),
            "交易方向取值合法",
        )
    )

    pending = [t for t in txns if t.status == TxnStatus.PENDING_REVIEW]
    checks.append(
        CheckResult(
            "pending_review",
            "warning" if pending else "ok",
            len(pending),
            "待人工确认交易",
            [t.id for t in pending[:5]],
        )
    )

    undefined_cat = [
        t
        for t in txns
        if t.category_id is None
        and t.txn_type not in {"transfer", "credit_card_repayment", "topup", "withdrawal"}
        and t.status != TxnStatus.VOID
    ]
    checks.append(
        CheckResult(
            "uncategorized",
            "warning" if undefined_cat else "ok",
            len(undefined_cat),
            "未分类交易（不得由模型臆测）",
            [t.id for t in undefined_cat[:5]],
        )
    )

    orphan_raw = session.scalar(
        select(func.count(RawTransaction.id)).where(
            RawTransaction.transaction_id.is_(None), RawTransaction.parse_status == "parsed"
        )
    )
    checks.append(
        CheckResult(
            "raw_rows_linked",
            "warning" if orphan_raw else "ok",
            int(orphan_raw or 0),
            "已解析但未落账的原始行",
        )
    )

    failed_batches = session.scalars(
        select(ImportBatch).where(
            ImportBatch.household_id == household_id, ImportBatch.failed_rows > 0
        )
    ).all()
    checks.append(
        CheckResult(
            "import_failures",
            "warning" if failed_batches else "ok",
            sum(b.failed_rows for b in failed_batches),
            "导入失败行数",
            [b.id for b in failed_batches[:5]],
        )
    )

    dangling_dupes = [
        t for t in txns if t.duplicate_of_id and session.get(Transaction, t.duplicate_of_id) is None
    ]
    checks.append(
        CheckResult(
            "duplicate_links",
            "error" if dangling_dupes else "ok",
            len(dangling_dupes),
            "重复交易指向的原交易存在",
        )
    )

    bad_refunds = []
    for refund in session.scalars(
        select(Refund).where(Refund.household_id == household_id)
    ):
        original = session.get(Transaction, refund.original_txn_id)
        refund_txn = session.get(Transaction, refund.refund_txn_id)
        if original is None or refund_txn is None or original.direction != "outflow":
            bad_refunds.append(refund.id)
    checks.append(
        CheckResult(
            "refund_links",
            "error" if bad_refunds else "ok",
            len(bad_refunds),
            "退款必须关联到一笔原始支出",
            bad_refunds[:5],
        )
    )

    bad_transfers = []
    for match in session.scalars(
        select(TransferMatch).where(TransferMatch.household_id == household_id)
    ):
        out_txn = session.get(Transaction, match.outflow_txn_id)
        in_txn = session.get(Transaction, match.inflow_txn_id)
        if out_txn is None or in_txn is None:
            bad_transfers.append(match.id)
    checks.append(
        CheckResult(
            "transfer_links",
            "error" if bad_transfers else "ok",
            len(bad_transfers),
            "转账匹配双边交易存在",
            bad_transfers[:5],
        )
    )

    # Asset current value must match the latest valuation for that asset.
    stale_assets = []
    for asset in session.scalars(select(Asset).where(Asset.household_id == household_id)):
        latest = session.scalar(
            select(AssetValuation)
            .where(AssetValuation.asset_id == asset.id)
            .order_by(AssetValuation.valued_on.desc())
            .limit(1)
        )
        if latest and latest.value_minor != asset.current_value_minor:
            stale_assets.append(asset.id)
    checks.append(
        CheckResult(
            "asset_valuation_consistency",
            "warning" if stale_assets else "ok",
            len(stale_assets),
            "资产当前估值与最新估值记录一致",
            stale_assets[:5],
        )
    )

    negative_liability = session.scalars(
        select(Liability).where(
            Liability.household_id == household_id, Liability.balance_minor < 0
        )
    ).all()
    checks.append(
        CheckResult(
            "liability_balances",
            "error" if negative_liability else "ok",
            len(negative_liability),
            "负债余额不得为负",
        )
    )

    # Account balance vs. opening + signed transactions (informational).
    drift = []
    for account in session.scalars(select(Account).where(Account.household_id == household_id)):
        if account.balance_as_of is None:
            continue
        signed = session.execute(
            text(
                """
                SELECT COALESCE(SUM(CASE WHEN direction = 'inflow' THEN amount_minor
                                         ELSE -amount_minor END), 0)
                FROM transactions
                WHERE account_id = :account_id AND is_deleted = 0
                  AND status != 'void'
                """
            ),
            {"account_id": account.id},
        ).scalar()
        expected = account.opening_balance_minor + int(signed or 0)
        if expected != account.balance_minor:
            drift.append(account.id)
    checks.append(
        CheckResult(
            "account_balance_drift",
            "info" if drift else "ok",
            len(drift),
            "账户余额与流水推算不一致（若已手工对账可忽略）",
            drift[:5],
        )
    )

    if period:
        closed = session.scalar(
            select(Settlement).where(
                Settlement.household_id == household_id, Settlement.period == period
            )
        )
        if closed and closed.status == SettlementStatus.CLOSED:
            period_txns = [t for t in txns if t.occurred_at.strftime("%Y-%m") == period]
            late_pending = [t for t in period_txns if t.status == TxnStatus.PENDING_REVIEW]
            checks.append(
                CheckResult(
                    "closed_period_pending",
                    "error" if late_pending else "ok",
                    len(late_pending),
                    f"{period} 已结算但仍存在待确认交易",
                    [t.id for t in late_pending[:5]],
                )
            )

    checks.append(
        CheckResult(
            "transaction_count",
            "info",
            len(txns),
            "当前交易总数（参考）",
        )
    )
    return IntegrityReport(checks=checks, generated_at=datetime.now().replace(microsecond=0))


def settlement_blockers(report: IntegrityReport) -> list[str]:
    blockers: list[str] = []
    for check in report.errors:
        blockers.append(f"{check.name}: {check.detail or check.count}")
    return blockers


def report_json(report: IntegrityReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str)
