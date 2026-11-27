"""Monthly settlement engine (COLLECTING → … → CLOSED).

Closing is a gated operation: every blocking check must pass, or the user must
explicitly authorise an override / suspension policy, which is recorded in the
audit log and in ``settlement_adjustments``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import (
    REVIEW_AMBIGUOUS_TRANSFER,
    REVIEW_DUPLICATE,
    REVIEW_REFUND_UNLINKED,
    SettlementStatus,
    TxnStatus,
    TxnType,
)
from ..core.dates import month_bounds, month_end, month_start, parse_period
from ..core.errors import SettlementError, ValidationError
from ..core.ids import new_id
from ..core.logging import get_logger
from ..core.money import from_minor
from ..database.audit import record_audit
from ..database.init import get_household
from ..database.integrity import run_integrity_checks
from ..database.models import ImportBatch, Settlement, SettlementAdjustment, Transaction
from ..finance.metrics import capture_snapshot, period_summary

log = get_logger("finance.settlement")

DEFAULT_SOURCES = ["wechat", "alipay", "bank"]

# 用户确认（2026-11-27）：招行流水与微信/支付宝高度重叠，不作为每月必需渠道。
# 只有需要房贷扣款、工资入账、理财申购等「支付平台看不见的钱」时才补导。
OPTIONAL_SOURCES = {"bank"}

BANK_OPTIONAL_REASON = (
    "用户确认不导招行流水（与微信/支付宝账单重叠）。"
    "仅当需要房贷扣款、工资入账、理财申购、余额宝转入等银行独有流水时补导。"
)


def default_source_entries() -> list[dict]:
    """默认渠道清单：必需渠道 + 可选渠道（可选不阻塞关账）。"""
    return [
        {
            "source": s,
            "status": "optional" if s in OPTIONAL_SOURCES else "expected",
            "reason": BANK_OPTIONAL_REASON if s in OPTIONAL_SOURCES else None,
        }
        for s in DEFAULT_SOURCES
    ]


@dataclass
class ChecklistItem:
    key: str
    label: str
    passed: bool
    blocking: bool
    detail: str = ""
    severity: str = "error"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "blocking": self.blocking,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass
class SettlementEvaluation:
    period: str
    status: str
    ready: bool
    items: list[ChecklistItem] = field(default_factory=list)
    expected_sources: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "status": self.status,
            "ready": self.ready,
            "blockers": self.blockers,
            "expected_sources": self.expected_sources,
            "totals": self.totals,
            "checklist": [i.as_dict() for i in self.items],
        }


class SettlementService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id

    # ------------------------------------------------------------------ setup
    def ensure(self, period: str) -> Settlement:
        period = parse_period(period)
        settlement = self.get(period)
        if settlement is not None:
            return settlement
        expected = self._initial_sources(period)
        settlement = Settlement(
            id=new_id("settlement"),
            household_id=self.household_id,
            period=period,
            status=SettlementStatus.COLLECTING,
            expected_sources=json.dumps(expected, ensure_ascii=False),
        )
        self.session.add(settlement)
        self.session.flush()
        return settlement

    def get(self, period: str) -> Settlement | None:
        return self.session.scalar(
            select(Settlement).where(
                Settlement.household_id == self.household_id,
                Settlement.period == parse_period(period),
            )
        )

    def _initial_sources(self, period: str) -> list[dict]:
        configured = (
            (self.settings.raw or {}).get("settlement", {}).get("expected_sources")
            if isinstance((self.settings.raw or {}).get("settlement", {}), dict)
            else None
        )
        sources: list[str] = list(configured) if configured else []
        if not sources:
            # Derive from the previous three months, then fall back to defaults.
            from ..core.dates import previous_period

            seen: list[str] = []
            for offset in range(1, 4):
                prev = previous_period(period, offset)
                rows = self.session.scalars(
                    select(ImportBatch.detected_platform).where(
                        ImportBatch.household_id == self.household_id,
                        ImportBatch.period == prev,
                        ImportBatch.detected_platform.is_not(None),
                    )
                ).all()
                for source in rows:
                    if source and source not in seen:
                        seen.append(source)
            sources = seen or list(DEFAULT_SOURCES)
        return default_source_entries()

    def expected_sources(self, period: str) -> list[dict]:
        settlement = self.ensure(period)
        try:
            data = json.loads(settlement.expected_sources or "[]")
        except json.JSONDecodeError:
            data = []
        return data or default_source_entries()

    def mark_source(
        self,
        period: str,
        source: str,
        *,
        status: str = "missing",
        reason: str,
        actor: str = "cli",
    ) -> Settlement:
        if status not in {"expected", "missing", "optional"}:
            raise ValidationError("status 只能是 expected、missing 或 optional")
        if status in {"missing", "optional"} and not reason.strip():
            raise ValidationError("标记缺失/可选渠道必须填写原因")
        settlement = self.ensure(period)
        sources = self.expected_sources(period)
        found = False
        for item in sources:
            if item["source"] == source:
                item["status"] = status
                item["reason"] = reason
                found = True
        if not found:
            sources.append({"source": source, "status": status, "reason": reason})
        settlement.expected_sources = json.dumps(sources, ensure_ascii=False)
        record_audit(
            self.session,
            household_id=self.household_id,
            action="update",
            entity_type="settlement",
            entity_id=settlement.id,
            after={"source": source, "status": status, "reason": reason},
            detail="mark_source",
            actor=actor,
        )
        return settlement

    # --------------------------------------------------------------- evaluate
    def evaluate(self, period: str) -> SettlementEvaluation:
        period = parse_period(period)
        settlement = self.ensure(period)
        sources = self.expected_sources(period)
        start, end = month_bounds(period)
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time().replace(microsecond=0))

        txns = list(
            self.session.scalars(
                select(Transaction).where(
                    Transaction.household_id == self.household_id,
                    Transaction.occurred_at >= start_dt,
                    Transaction.occurred_at <= end_dt,
                    Transaction.is_deleted.is_(False),
                )
            )
        )
        batches = list(
            self.session.scalars(
                select(ImportBatch).where(
                    ImportBatch.household_id == self.household_id,
                    ImportBatch.period == period,
                )
            )
        )
        items: list[ChecklistItem] = []

        missing = []
        for item in sources:
            if item["status"] in {"missing", "optional"}:
                continue
            has_batch = any(
                b.detected_platform == item["source"] and b.status in {"imported"}
                for b in batches
            )
            has_txn = any(t.source_platform == item["source"] for t in txns)
            if not (has_batch or has_txn):
                missing.append(item["source"])
        items.append(
            ChecklistItem(
                "sources_present",
                "应纳入的账单渠道均已导入或标记缺失",
                not missing,
                True,
                "缺少渠道: " + ", ".join(missing) if missing else "全部渠道已有数据",
            )
        )

        failed_rows = sum(b.failed_rows for b in batches)
        items.append(
            ChecklistItem(
                "import_errors",
                "导入过程无未处理错误",
                failed_rows == 0,
                True,
                f"失败行数: {failed_rows}" if failed_rows else "无导入错误",
            )
        )

        pending = [t for t in txns if t.status == TxnStatus.PENDING_REVIEW]
        # 已用 `tx resolve --duplicate` 处理的交易 stats_included=False，
        # 视为已人工确认，不应再计入「疑似重复」阻塞项。
        duplicate_suspects = [
            t
            for t in txns
            if (t.review_reason == REVIEW_DUPLICATE or t.duplicate_of_id)
            and t.stats_included
        ]
        unlinked_refunds = [
            t
            for t in txns
            if t.review_reason == REVIEW_REFUND_UNLINKED and t.direction == "inflow"
        ]
        items.append(
            ChecklistItem(
                "pending_review",
                "待确认交易已处理或明确暂挂",
                not pending,
                True,
                f"待确认 {len(pending)} 笔",
            )
        )
        items.append(
            ChecklistItem(
                "duplicates",
                "重复交易候选已处理",
                not duplicate_suspects,
                True,
                f"疑似重复 {len(duplicate_suspects)} 笔",
            )
        )
        internal_reasons = {REVIEW_AMBIGUOUS_TRANSFER, "internal_flow_candidate"}
        unreviewed_transfers = [
            t
            for t in txns
            if t.review_reason in internal_reasons
            and not t.transfer_group_id
            and t.status != TxnStatus.VOID
        ]
        items.append(
            ChecklistItem(
                "transfers_reviewed",
                "单边转账/还款已人工确认",
                not unreviewed_transfers,
                True,
                f"待确认 {len(unreviewed_transfers)} 笔",
            )
        )
        items.append(
            ChecklistItem(
                "refund_links",
                "退款已关联原交易",
                not unlinked_refunds,
                True,
                f"未关联退款 {len(unlinked_refunds)} 笔",
            )
        )

        integrity = run_integrity_checks(self.session, self.household_id, period=period)
        integrity_errors = integrity.errors
        items.append(
            ChecklistItem(
                "integrity",
                "数据完整性校验通过",
                not integrity_errors,
                True,
                "; ".join(f"{c.name}({c.count})" for c in integrity_errors) or "通过",
            )
        )

        summary = period_summary(self.session, self.household_id, period)
        sql_income = self.session.execute(
            select(func.coalesce(func.sum(Transaction.amount_base_minor), 0)).where(
                Transaction.household_id == self.household_id,
                Transaction.occurred_at >= start_dt,
                Transaction.occurred_at <= end_dt,
                Transaction.is_deleted.is_(False),
                Transaction.status != TxnStatus.VOID,
                Transaction.stats_included.is_(True),
                Transaction.txn_type == TxnType.INCOME,
                Transaction.direction == "inflow",
            )
        ).scalar_one()
        sql_expense = self.session.execute(
            select(func.coalesce(func.sum(Transaction.amount_base_minor), 0)).where(
                Transaction.household_id == self.household_id,
                Transaction.occurred_at >= start_dt,
                Transaction.occurred_at <= end_dt,
                Transaction.is_deleted.is_(False),
                Transaction.status != TxnStatus.VOID,
                Transaction.stats_included.is_(True),
                Transaction.txn_type.in_([TxnType.EXPENSE, TxnType.FEE]),
                Transaction.direction == "outflow",
            )
        ).scalar_one()
        sql_refund = self.session.execute(
            select(func.coalesce(func.sum(Transaction.amount_base_minor), 0)).where(
                Transaction.household_id == self.household_id,
                Transaction.occurred_at >= start_dt,
                Transaction.occurred_at <= end_dt,
                Transaction.is_deleted.is_(False),
                Transaction.status != TxnStatus.VOID,
                Transaction.stats_included.is_(True),
                Transaction.txn_type == TxnType.REFUND,
                Transaction.direction == "inflow",
            )
        ).scalar_one()
        sql_net_expense = int(sql_expense) - int(sql_refund)
        consistent = int(sql_income) == summary.income_minor and sql_net_expense == summary.expense_minor
        items.append(
            ChecklistItem(
                "amount_validation",
                "收支金额双重校验一致",
                consistent,
                True,
                (
                    f"收入 {from_minor(int(sql_income))} vs {from_minor(summary.income_minor)}；"
                    f"支出 {from_minor(sql_net_expense)} vs {from_minor(summary.expense_minor)}"
                ),
            )
        )

        blockers = [f"{i.label}: {i.detail}" for i in items if i.blocking and not i.passed]
        ready = not blockers

        status = settlement.status
        if status != SettlementStatus.CLOSED:
            if not batches and not txns:
                status = SettlementStatus.COLLECTING
            elif blockers:
                status = SettlementStatus.REVIEW_REQUIRED
            elif any(b.status == "importing" for b in batches):
                status = SettlementStatus.IMPORTING
            elif ready:
                status = SettlementStatus.READY_TO_CLOSE
            else:
                status = SettlementStatus.RECONCILING
            settlement.status = status
            settlement.checklist_json = json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "items": [i.as_dict() for i in items],
                    "blockers": blockers,
                },
                ensure_ascii=False,
            )

        return SettlementEvaluation(
            period=period,
            status=status,
            ready=ready,
            items=items,
            expected_sources=sources,
            totals={
                "income": from_minor(summary.income_minor),
                "expense": from_minor(summary.expense_minor),
                "net": from_minor(summary.net_minor),
                "transaction_count": len(txns),
            },
            blockers=blockers,
        )

    # -------------------------------------------------------------------- close
    def close(
        self,
        period: str,
        *,
        actor: str = "cli",
        override_reason: str | None = None,
        pending_policy: str | None = None,
    ) -> Settlement:
        period = parse_period(period)
        settlement = self.ensure(period)
        evaluation = self.evaluate(period)
        if settlement.status == SettlementStatus.CLOSED:
            raise SettlementError(f"{period} 已结算；如需修改请先 reopen 并记录原因。")

        remaining = list(evaluation.blockers)
        suspended = False
        if pending_policy:
            if pending_policy not in {"suspend", "exclude"}:
                raise ValidationError("pending_policy 只能是 suspend 或 exclude")
            # Only the review-related blockers are curable by an explicit policy.
            policy_keys = {"pending_review", "duplicates", "refund_links"}
            remaining = [
                f"{i.label}: {i.detail}"
                for i in evaluation.items
                if i.blocking and not i.passed and i.key not in policy_keys
            ]
            suspended = True
        if remaining and not override_reason:
            raise SettlementError(
                f"{period} 尚不满足结算条件：" + "；".join(remaining)
            )
        if remaining and override_reason is not None and not override_reason.strip():
            raise ValidationError("覆盖结算必须填写原因")

        closed_at = datetime.now().replace(microsecond=0)
        settlement.status = SettlementStatus.CLOSED
        settlement.closed_at = closed_at
        settlement.closed_by = actor
        settlement.override_reason = override_reason
        if suspended:
            settlement.notes = (
                (settlement.notes + "\n" if settlement.notes else "")
                + f"待确认交易按 {pending_policy} 策略暂挂，未计入本次结算统计。"
            )
            for txn in self.session.scalars(
                select(Transaction).where(
                    Transaction.household_id == self.household_id,
                    Transaction.occurred_at >= datetime.combine(month_start(period), datetime.min.time()),
                    Transaction.occurred_at
                    <= datetime.combine(month_end(period), datetime.max.time().replace(microsecond=0)),
                    Transaction.status == TxnStatus.PENDING_REVIEW,
                )
            ):
                txn.stats_included = False
        snapshot = capture_snapshot(
            self.session,
            self.household_id,
            as_of=month_end(period),
            settings=self.settings,
        )
        self.session.add(
            SettlementAdjustment(
                id=new_id("adjustment"),
                settlement_id=settlement.id,
                reason=override_reason or ("pending_policy:" + pending_policy if pending_policy else "monthly_close"),
                payload_json=json.dumps(
                    {
                        "totals": evaluation.totals,
                        "checklist": [i.as_dict() for i in evaluation.items],
                        "snapshot_id": snapshot.id,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        )
        record_audit(
            self.session,
            household_id=self.household_id,
            action="settle",
            entity_type="settlement",
            entity_id=settlement.id,
            after={
                "period": period,
                "status": settlement.status,
                "override": bool(override_reason),
                "pending_policy": pending_policy,
                "totals": evaluation.totals,
            },
            detail=override_reason,
            actor=actor,
        )
        return settlement

    def reopen(self, period: str, *, reason: str, actor: str = "cli") -> Settlement:
        if not reason.strip():
            raise ValidationError("重新打开已结算月份必须填写原因")
        settlement = self.get(period)
        if settlement is None or settlement.status != SettlementStatus.CLOSED:
            raise SettlementError(f"{parse_period(period)} 尚未结算，无法 reopen。")
        settlement.status = SettlementStatus.RECONCILING
        settlement.reopened_count += 1
        self.session.add(
            SettlementAdjustment(
                id=new_id("adjustment"),
                settlement_id=settlement.id,
                reason=f"reopen: {reason}",
                payload_json=json.dumps({"reopened_at": datetime.now().isoformat(timespec="seconds")}),
            )
        )
        record_audit(
            self.session,
            household_id=self.household_id,
            action="reopen",
            entity_type="settlement",
            entity_id=settlement.id,
            before={"status": SettlementStatus.CLOSED},
            after={"status": settlement.status, "reopened_count": settlement.reopened_count},
            detail=reason,
            actor=actor,
        )
        return settlement

    def list(self, *, limit: int = 24) -> list[Settlement]:
        return list(
            self.session.scalars(
                select(Settlement)
                .where(Settlement.household_id == self.household_id)
                .order_by(Settlement.period.desc())
                .limit(limit)
            )
        )
