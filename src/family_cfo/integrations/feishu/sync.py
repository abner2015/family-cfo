"""Bidirectional sync between the SQLite ledger and Feishu Bitable.

Rules enforced here:

* SQLite is authoritative; a failed push never mutates the ledger.
* Every remote write is tracked in ``sync_records`` with a payload hash.
* A remote record modified after our last successful sync is a **conflict**,
  never a silent overwrite.
* Pull-back is limited to user-editable entities (goals, budgets) and is
  validated with Pydantic before touching SQLite.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...budgets.service import BudgetService
from ...core.config import Settings
from ...core.constants import SyncStatus, TxnStatus
from ...core.dates import parse_period
from ...core.errors import ConflictError, FeishuError, ValidationError
from ...core.ids import new_id
from ...core.logging import get_logger
from ...core.money import from_minor
from ...database.audit import record_audit
from ...database.base import Database
from ...database.init import get_household
from ...database.models import (
    Account,
    Asset,
    Budget,
    BudgetItem,
    FinancialGoal,
    Liability,
    SyncRecord,
    SystemConfig,
    Transaction,
    TransactionCategory,
)
from ...finance.metrics import build_metrics
from .client import FeishuClient
from .schema import TableSpec, ensure_remote_tables, load_schema

log = get_logger("feishu.sync")

PUSH_ENTITIES = (
    "transactions",
    "assets",
    "liabilities",
    "budgets",
    "goals",
    "monthly_reports",
    "annual_summaries",
    "category_annual_summaries",
)


class GoalPull(BaseModel):
    target_minor: int = Field(gt=0)
    current_minor: int = Field(ge=0)
    monthly_saving_minor: int | None = Field(default=None, ge=0)
    target_date: date | None = None


class BudgetPullItem(BaseModel):
    category: str = Field(min_length=1)
    amount_minor: int = Field(gt=0)


class BudgetPull(BaseModel):
    period: str
    version: int = Field(ge=1)
    items: list[BudgetPullItem]


@dataclass
class SyncReport:
    entity: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    dry_run: bool = False
    message: str | None = None

    def as_dict(self) -> dict:
        return {
            "entity": self.entity,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "message": self.message,
        }


def _ms(value: datetime | date | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return int(datetime.combine(value, datetime.min.time()).timestamp() * 1000)


def _yuan(minor: int) -> float:
    return float(from_minor(minor))


def _normalize(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _hash(fields: dict) -> str:
    payload = _normalize(fields)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class FeishuSyncService:
    def __init__(
        self, db: Database, settings: Settings, *, client: FeishuClient | None = None
    ) -> None:
        self.db = db
        self.settings = settings
        self.client = client or FeishuClient(settings)
        self.specs: dict[str, TableSpec] = load_schema(settings.project_root)

    def close(self) -> None:
        self.client.close()

    # ---------------------------------------------------------------- tables
    def ensure_tables(self, *, persist: bool = True) -> dict[str, str]:
        mapping = ensure_remote_tables(self.client, self.specs)
        if persist:
            with self.db.session() as session:
                for key, table_id in mapping.items():
                    self._set_config(session, f"feishu.table_id.{key}", table_id)
        return mapping

    def table_ids(self, *, ensure: bool = False) -> dict[str, str]:
        ids: dict[str, str] = {}
        missing = []
        with self.db.session() as session:
            for key in self.specs:
                row = session.get(SystemConfig, f"feishu.table_id.{key}")
                if row is not None:
                    ids[key] = row.value
                else:
                    missing.append(key)
        if missing:
            if not ensure:
                raise FeishuError(
                    "飞书数据表尚未初始化，请先运行 `family-cfo feishu-sync --ensure-tables`。",
                    detail="缺少: " + ", ".join(missing),
                )
            ids.update(self.ensure_tables())
        return ids

    # ------------------------------------------------------- periodic tables
    def _period_table_id(self, entity: str, period: str = "") -> str:
        """Resolve the remote table that holds rows for this entity.

        The Feishu app used here has no bitable table-management role
        (creating or renaming tables fails with 1254302 RolePermNotAllow),
        so periodic entities share ONE table and are separated by a
        ``月份`` field plus one filtered view per month.
        """
        spec = self.specs[entity]
        config_key = f"feishu.table_id.{entity}"
        with self.db.session() as session:
            row = session.get(SystemConfig, config_key)
            if row is not None:
                return row.value
        existing = {t.get("name"): t for t in self.client.list_tables()}
        table = existing.get(spec.name)
        if table is None:
            raise FeishuError(
                f"飞书数据表不存在: {spec.name}",
                detail="应用缺少数据表管理权限，无法自动建表；请先在飞书网页端创建该表。",
            )
        table_id = table.get("table_id") or table.get("id")
        if not table_id:
            raise FeishuError(f"无法获取飞书数据表 ID: {spec.name}")
        with self.db.session() as session:
            self._set_config(session, config_key, table_id)
        return table_id

    def ensure_month_views(self, entity: str = "transactions") -> dict[str, str]:
        """Create one filtered view per month so each month is browsable."""
        spec = self.specs[entity]
        table_id = self._period_table_id(entity, "0000-00")
        periods = self._remote_periods(entity, table_id)
        existing = self.client.list_views(table_id)
        by_name = {v.get("view_name"): v for v in existing}
        created: dict[str, str] = {}
        for period in sorted(periods):
            view_name = f"{period}"
            if view_name in by_name:
                created[period] = by_name[view_name].get("view_id")
                continue
            view = self.client.create_view(table_id, view_name, view_type="grid")
            view_id = (view or {}).get("view_id")
            if view_id:
                self.client.set_view_filter(
                    table_id, view_id, "月份", [period], view_name=period
                )
                created[period] = view_id
                log.info("created view %s for %s", view_name, spec.name)
        return created

    def _remote_periods(self, entity: str, table_id: str) -> set[str]:
        """Distinct 月份 values currently stored remotely."""
        found: set[str] = set()
        token = None
        while True:
            res = self.client.list_records(table_id, page_token=token, page_size=500)
            for item in res.get("items", []):
                fields = item.get("fields") or {}
                value = fields.get("月份")
                if isinstance(value, list):
                    value = "".join(
                        part.get("text", "") for part in value if isinstance(part, dict)
                    )
                if value:
                    found.add(str(value))
            token = res.get("page_token")
            if not res.get("has_more"):
                break
        return found

    def archive_old_periods(
        self,
        entity: str = "transactions",
        *,
        keep_months: int = 12,
        row_limit: int = 1800,
    ) -> dict:
        """Trim the remote table so it never hits Feishu's row cap.

        Rows older than ``keep_months`` are deleted remotely — they stay in
        the authoritative SQLite ledger and in the local CSV/XLSX exports, so
        nothing is lost. Deletion only happens when the table exceeds
        ``row_limit``.
        """
        spec = self.specs[entity]
        table_id = self._period_table_id(entity)
        total = 0
        token = None
        while True:
            res = self.client.list_records(table_id, page_token=token, page_size=500)
            total += len(res.get("items", []))
            token = res.get("page_token")
            if not res.get("has_more"):
                break
        if total <= row_limit:
            return {"entity": entity, "remote_rows": total, "deleted": 0, "skipped": "under_limit"}

        cutoff = _cutoff_period(keep_months)
        to_delete: list[str] = []
        token = None
        while True:
            res = self.client.list_records(table_id, page_token=token, page_size=500)
            for item in res.get("items", []):
                fields = item.get("fields") or {}
                value = fields.get("月份")
                if isinstance(value, list):
                    value = "".join(
                        part.get("text", "") for part in value if isinstance(part, dict)
                    )
                if value and str(value) < cutoff:
                    to_delete.append(item.get("record_id"))
            token = res.get("page_token")
            if not res.get("has_more"):
                break

        deleted = 0
        for record_id in to_delete:
            try:
                self.client.delete_record(table_id, record_id)
                deleted += 1
            except FeishuError as exc:
                log.warning("archive delete failed for %s: %s", record_id, exc)
        # Forget the local mapping so archived rows get re-pushed if needed.
        with self.db.session() as session:
            row = session.get(SystemConfig, f"feishu.table_id.{entity}")
            if row is not None:
                session.delete(row)
        log.info(
            "archived %d rows from %s (cutoff %s, was %d rows)",
            deleted, spec.name, cutoff, total,
        )
        return {
            "entity": entity,
            "remote_rows": total,
            "deleted": deleted,
            "cutoff": cutoff,
        }

    # ----------------------------------------------------------------- push
    def push(self, entity: str, *, dry_run: bool = False) -> SyncReport:
        if entity not in PUSH_ENTITIES:
            raise ValidationError(f"不支持同步的实体: {entity}")
        spec = self.specs.get(entity)
        builder = getattr(self, f"_payloads_{entity}")
        with self.db.session() as session:
            household = get_household(session)
            payloads = list(builder(session, household.id))
        report = SyncReport(entity=entity, dry_run=dry_run)

        if spec is not None and spec.periodic == "month":
            self._push_monthly(entity, payloads, dry_run=dry_run, report=report)
            return report

        table_ids = self.table_ids(ensure=not dry_run)
        table_id = table_ids.get(entity)
        if not table_id:
            raise FeishuError(f"缺少数据表映射: {entity}")
        for entity_id, fields in payloads:
            self._push_one_safe(entity, entity_id, fields, table_id, dry_run=dry_run, report=report)
        return report

    def _push_monthly(
        self,
        entity: str,
        payloads: list[tuple[str, dict]],
        *,
        dry_run: bool,
        report: SyncReport,
    ) -> None:
        """Push rows into the shared table, tagged with a ``月份`` field.

        The remote table is shared across months (the app cannot create
        tables), so each row carries its ``YYYY-MM`` period and one filtered
        view per month is maintained for browsing.
        """
        table_id = "" if dry_run else self._period_table_id(entity, "")
        for entity_id, fields in payloads:
            period = _period_of(fields)
            if not period:
                report.failed.append({"entity_id": entity_id, "error": "缺少日期，无法确定月份"})
                continue
            fields = {**fields, "月份": period}
            self._push_one_safe(
                entity, entity_id, fields, table_id, dry_run=dry_run, report=report
            )

    def _push_one_safe(
        self,
        entity: str,
        entity_id: str,
        fields: dict,
        table_id: str,
        *,
        dry_run: bool,
        report: SyncReport,
    ) -> None:
        try:
            self._push_one(
                entity, entity_id, fields, table_id, dry_run=dry_run, report=report
            )
        except ConflictError as exc:
            report.conflicts.append({"entity_id": entity_id, "reason": str(exc)})
            self._mark(
                entity, entity_id, direction="push", status=SyncStatus.CONFLICT,
                error=str(exc), attempted=True,
            )
        except (FeishuError, ValidationError) as exc:
            report.failed.append({"entity_id": entity_id, "error": str(exc)})
            self._mark(
                entity, entity_id, direction="push", status=SyncStatus.FAILED,
                error=str(exc), attempted=True,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one bad record
            log.exception("unexpected sync error for %s/%s", entity, entity_id)
            report.failed.append({"entity_id": entity_id, "error": str(exc)})
            self._mark(
                entity, entity_id, direction="push", status=SyncStatus.FAILED,
                error=str(exc), attempted=True,
            )

    def _push_one(
        self,
        entity: str,
        entity_id: str,
        fields: dict,
        table_id: str,
        *,
        dry_run: bool,
        report: SyncReport,
        force: bool = False,
    ) -> None:
        digest = _hash(fields)
        sync = self._get_sync(entity, entity_id, "push")
        if sync and sync.status == SyncStatus.SYNCED and sync.payload_hash == digest and not force:
            report.skipped += 1
            return
        if dry_run:
            report.created += 0 if (sync and sync.remote_record_id) else 1
            report.updated += 1 if (sync and sync.remote_record_id) else 0
            return
        if sync and sync.remote_record_id:
            if not force:
                remote = self.client.get_record(table_id, sync.remote_record_id)
                remote_fields = {k: v for k, v in (remote.get("fields") or {}).items() if k in fields}
                remote_digest = _hash(remote_fields)
                remote_modified = int(remote.get("last_modified_time") or 0)
                last_synced_ms = int(sync.last_synced_at.timestamp() * 1000) if sync.last_synced_at else 0
                if remote_modified > last_synced_ms and remote_digest != (sync.payload_hash or ""):
                    raise ConflictError(
                        f"远端记录在 {sync.last_synced_at} 之后被修改，拒绝覆盖（record {sync.remote_record_id}）"
                    )
            self.client.update_record(table_id, sync.remote_record_id, fields)
            report.updated += 1
            self._mark(
                entity, entity_id, direction="push", status=SyncStatus.SYNCED,
                remote_table=table_id, remote_record_id=sync.remote_record_id,
                payload=digest, synced=True,
            )
        else:
            record = self.client.create_record(
                table_id, fields, client_token=_client_token(entity, entity_id)
            )
            record_id = record.get("record_id") or record.get("id")
            report.created += 1
            self._mark(
                entity, entity_id, direction="push", status=SyncStatus.SYNCED,
                remote_table=table_id, remote_record_id=record_id,
                payload=digest, synced=True,
            )

    # ------------------------------------------------------------- push data
    def _payloads_transactions(self, session: Session, household_id: str):
        categories = {c.id: c.name for c in session.scalars(select(TransactionCategory))}
        accounts = {a.id: a.name for a in session.scalars(select(Account))}
        for txn in session.scalars(
            select(Transaction).where(
                Transaction.household_id == household_id,
                Transaction.is_deleted.is_(False),
                Transaction.status != TxnStatus.VOID,
            )
        ):
            yield txn.id, {
                "业务ID": txn.id,
                "日期": _ms(txn.occurred_at),
                "方向": "收入" if txn.direction == "inflow" else "支出",
                "类型": txn.txn_type,
                "金额": _yuan(txn.amount_minor),
                "币种": txn.currency,
                "分类": categories.get(txn.category_id, "未分类"),
                "账户": accounts.get(txn.account_id, ""),
                "对方": txn.counterparty or "",
                "说明": txn.description or "",
                "状态": txn.status,
                "来源": txn.source_platform,
                "纳入统计": bool(txn.stats_included),
            }

    def _payloads_assets(self, session: Session, household_id: str):
        for asset in session.scalars(
            select(Asset).where(Asset.household_id == household_id, Asset.is_active.is_(True))
        ):
            yield asset.id, {
                "业务ID": asset.id,
                "名称": asset.name,
                "类型": asset.asset_type,
                "估值": _yuan(asset.current_value_minor),
                "成本": _yuan(asset.acquisition_cost_minor),
                "流动资产": bool(asset.is_liquid),
                "自住房": bool(asset.is_primary_residence),
                "估值日期": _ms(asset.valuation_date),
                "估值来源": asset.valuation_source or "",
            }

    def _payloads_liabilities(self, session: Session, household_id: str):
        for item in session.scalars(
            select(Liability).where(
                Liability.household_id == household_id, Liability.is_active.is_(True)
            )
        ):
            yield item.id, {
                "业务ID": item.id,
                "名称": item.name,
                "类型": item.liability_type,
                "余额": _yuan(item.balance_minor),
                "利率": float(Decimal(item.annual_rate_bps) / 10000 * 100),
                "月供": _yuan(item.monthly_payment_minor),
                "下次还款日": _ms(item.next_payment_date),
                "剩余月数": item.remaining_months,
            }

    def _payloads_budgets(self, session: Session, household_id: str):
        budgets = list(
            session.scalars(
                select(Budget).where(
                    Budget.household_id == household_id, Budget.status == "active"
                )
            )
        )
        for budget in budgets:
            period = f"{budget.period_start.year:04d}-{budget.period_start.month:02d}"
            execution = BudgetService(session, self.settings).execution(period)
            actual_by_category = {
                line.category_id: line.actual_minor
                for line in (execution.lines if execution else [])
            }
            items = list(
                session.scalars(select(BudgetItem).where(BudgetItem.budget_id == budget.id))
            )
            for item in items:
                category = (
                    session.get(TransactionCategory, item.category_id)
                    if item.category_id
                    else None
                )
                actual = actual_by_category.get(item.category_id, 0)
                yield f"budget:{period}:{item.category_id}", {
                    "业务ID": item.id,
                    "账期": period,
                    "版本": budget.version,
                    "分类": category.name if category else "未指定",
                    "预算金额": _yuan(item.amount_minor),
                    "实际金额": _yuan(actual),
                    "执行率": float(Decimal(actual) / Decimal(item.amount_minor))
                    if item.amount_minor
                    else 0.0,
                    "预算ID": budget.id,
                }

    def _payloads_goals(self, session: Session, household_id: str):
        for goal in session.scalars(
            select(FinancialGoal).where(FinancialGoal.household_id == household_id)
        ):
            yield goal.id, _goal_fields(goal)

    def _payloads_monthly_reports(self, session: Session, household_id: str):

        periods = session.scalars(
            select(Transaction.occurred_at).where(Transaction.household_id == household_id)
        ).all()
        seen = sorted({dt.strftime("%Y-%m") for dt in periods})
        for period in seen:
            metrics = build_metrics(session, self.settings, period=period)
            summary = metrics["summary"]
            bs = metrics["balance_sheet"]
            report_id = f"report:{period}"
            yield report_id, {
                "业务ID": report_id,
                "账期": period,
                "结算状态": "generated",
                "收入": float(Decimal(summary["income"])),
                "支出": float(Decimal(summary["expense"])),
                "结余": float(Decimal(summary["net"])),
                "储蓄率": float(Decimal(summary["savings_rate"])) if summary["savings_rate"] else 0.0,
                "总资产": float(Decimal(bs["total_assets"])),
                "总负债": float(Decimal(bs["liabilities"])),
                "净资产": float(Decimal(bs["net_worth"])),
                "生成日期": _ms(date.today()),
            }

    def _payloads_annual_summaries(self, session: Session, household_id: str):
        """One row per year: income/expense/net/savings rate plus year-end balance."""
        from ...finance.metrics import balance_sheet, period_summary

        years = sorted(
            {
                dt.strftime("%Y")
                for dt in session.scalars(
                    select(Transaction.occurred_at).where(
                        Transaction.household_id == household_id,
                        Transaction.is_deleted.is_(False),
                    )
                )
            }
        )
        for year in years:
            income = expense = count = 0
            for month in range(1, 13):
                period = f"{year}-{month:02d}"
                summary = period_summary(session, household_id, period)
                income += summary.income_minor
                expense += summary.expense_minor
                count += summary.transaction_count
            net = income - expense
            sheet = balance_sheet(
                session, household_id, as_of=date(int(year), 12, 31)
            )
            report_id = f"annual:{year}"
            yield report_id, {
                "业务ID": report_id,
                "年份": year,
                "笔数": count,
                "收入": _yuan(income),
                "支出": _yuan(expense),
                "结余": _yuan(net),
                "储蓄率": float(Decimal(net) / Decimal(income)) if income else 0.0,
                "总资产": _yuan(sheet.total_assets_minor),
                "总负债": _yuan(sheet.liabilities_minor),
                "净资产": _yuan(sheet.net_worth_minor),
                "生成日期": _ms(date.today()),
            }

    def _payloads_category_annual_summaries(self, session: Session, household_id: str):
        """One row per (year, category): spend, count and share of annual spend."""
        from ...finance.metrics import net_flow_by_category, period_summary

        years = sorted(
            {
                dt.strftime("%Y")
                for dt in session.scalars(
                    select(Transaction.occurred_at).where(
                        Transaction.household_id == household_id,
                        Transaction.is_deleted.is_(False),
                    )
                )
            }
        )
        for year in years:
            yearly: dict[str, dict] = {}
            for month in range(1, 13):
                period = f"{year}-{month:02d}"
                for row in net_flow_by_category(session, household_id, period):
                    if row.get("direction") != "outflow":
                        continue
                    name = row.get("category") or "未分类"
                    bucket = yearly.setdefault(
                        name, {"amount_minor": 0, "count": 0}
                    )
                    bucket["amount_minor"] += int(row.get("amount_minor") or 0)
                    bucket["count"] += int(row.get("count") or 0)
            total_expense = 0
            for month in range(1, 13):
                summary = period_summary(session, household_id, f"{year}-{month:02d}")
                total_expense += summary.expense_minor
            for name, bucket in sorted(
                yearly.items(), key=lambda kv: kv[1]["amount_minor"], reverse=True
            ):
                if bucket["amount_minor"] <= 0:
                    continue
                report_id = f"category_annual:{year}:{name}"
                yield report_id, {
                    "业务ID": report_id,
                    "年份": year,
                    "分类": name,
                    "笔数": bucket["count"],
                    "金额": _yuan(bucket["amount_minor"]),
                    "占比": float(Decimal(bucket["amount_minor"]) / Decimal(total_expense))
                    if total_expense
                    else 0.0,
                    "生成日期": _ms(date.today()),
                }

    # ------------------------------------------------------------------ pull
    def pull(self, entity: str) -> SyncReport:
        if entity not in {"goals", "budgets"}:
            raise ValidationError(
                f"{entity} 为只读同步，不支持回写；可回写的实体: goals, budgets"
            )
        table_ids = self.table_ids(ensure=False)
        table_id = table_ids.get(entity)
        if not table_id:
            raise FeishuError(f"缺少数据表映射: {entity}")
        report = SyncReport(entity=entity)
        remote = self.client.iter_records(table_id)
        with self.db.session() as session:
            household = get_household(session)
            if entity == "goals":
                self._pull_goals(session, household.id, remote, report)
            else:
                self._pull_budgets(session, household.id, remote, report)
        return report

    def _pull_goals(self, session, household_id, remote: list[dict], report: SyncReport) -> None:
        goals = {g.id: g for g in session.scalars(select(FinancialGoal))}
        for record in remote:
            fields = record.get("fields", {})
            goal_id = _text(fields.get("业务ID"))
            if not goal_id or goal_id not in goals:
                report.skipped += 1
                continue
            goal = goals[goal_id]
            try:
                payload = GoalPull(
                    target_minor=int(round(float(fields.get("目标金额", 0)) * 100)),
                    current_minor=int(round(float(fields.get("当前金额", 0)) * 100)),
                    monthly_saving_minor=(
                        int(round(float(fields["每月储蓄"]) * 100))
                        if fields.get("每月储蓄") is not None
                        else None
                    ),
                    target_date=_from_ms(fields.get("目标日期")),
                )
            except (PydanticValidationError, TypeError, ValueError) as exc:
                report.failed.append({"entity_id": goal_id, "error": f"校验失败: {exc}"})
                continue
            local_fields = _goal_fields(goal)
            sync_push = self._get_sync("goals", goal_id, "push", session=session)
            local_dirty = bool(
                sync_push and sync_push.payload_hash and _hash(local_fields) != sync_push.payload_hash
            )
            if local_dirty:
                report.conflicts.append(
                    {"entity_id": goal_id, "reason": "本地目标在上次同步后被修改，拒绝覆盖"}
                )
                continue
            if (
                payload.target_minor == goal.target_minor
                and payload.current_minor == goal.current_minor
                and payload.monthly_saving_minor == goal.monthly_saving_minor
                and payload.target_date == goal.target_date
            ):
                report.skipped += 1
                continue
            before = {
                "target_minor": goal.target_minor,
                "current_minor": goal.current_minor,
                "monthly_saving_minor": goal.monthly_saving_minor,
                "target_date": str(goal.target_date),
            }
            goal.target_minor = payload.target_minor
            goal.current_minor = payload.current_minor
            goal.monthly_saving_minor = payload.monthly_saving_minor
            goal.target_date = payload.target_date
            if goal.status in {"active", "achieved"}:
                goal.status = (
                    "achieved" if goal.current_minor >= goal.target_minor else "active"
                )
            record_audit(
                session,
                household_id=household_id,
                action="sync",
                entity_type="financial_goal",
                entity_id=goal.id,
                before=before,
                after={
                    "target_minor": goal.target_minor,
                    "current_minor": goal.current_minor,
                    "monthly_saving_minor": goal.monthly_saving_minor,
                    "target_date": str(goal.target_date),
                },
                detail="feishu_pull",
                actor="feishu",
            )
            new_fields = _goal_fields(goal)
            self._mark(
                "goals", goal_id, direction="pull", status=SyncStatus.SYNCED,
                remote_table=None, remote_record_id=record.get("record_id"),
                payload=_hash(new_fields),
                synced=True,
                session=session,
            )
            self._mark(
                "goals", goal_id, direction="push", status=SyncStatus.SYNCED,
                payload=_hash(new_fields), synced=True, session=session,
            )
            report.updated += 1

    def _pull_budgets(self, session, household_id, remote: list[dict], report: SyncReport) -> None:
        grouped: dict[str, list[dict]] = {}
        for record in remote:
            fields = record.get("fields", {})
            period = _text(fields.get("账期"))
            if not period:
                report.skipped += 1
                continue
            grouped.setdefault(period, []).append(fields)
        service = BudgetService(session, self.settings)
        for period, rows in grouped.items():
            version = max(int(float(r.get("版本", 0) or 0)) for r in rows)
            # Feishu keeps rows from older budget versions.  Only the latest
            # version represents the current budget.
            rows = [r for r in rows if int(float(r.get("版本", 0) or 0)) == version]
            try:
                payload = BudgetPull(
                    period=parse_period(period),
                    version=max(version, 1),
                    items=[
                        BudgetPullItem(
                            category=_text(r.get("分类")) or "",
                            amount_minor=int(round(float(r.get("预算金额", 0)) * 100)),
                        )
                        for r in rows
                    ],
                )
            except (PydanticValidationError, ValueError, TypeError) as exc:
                report.failed.append({"entity_id": period, "error": f"校验失败: {exc}"})
                continue
            names = [item.category for item in payload.items]
            if len(names) != len(set(names)):
                report.failed.append({"entity_id": period, "error": "飞书预算存在重复分类"})
                continue
            remote_items = sorted((i.category, i.amount_minor) for i in payload.items)
            local = service.active(payload.period)
            name = f"{payload.period} 家庭预算"
            if local is not None:
                local_items = list(
                    session.scalars(select(BudgetItem).where(BudgetItem.budget_id == local.id))
                )
                local_pairs = []
                for item in local_items:
                    category = (
                        session.get(TransactionCategory, item.category_id)
                        if item.category_id
                        else None
                    )
                    local_pairs.append(
                        (category.name if category else "未指定", item.amount_minor)
                    )
                if sorted(local_pairs) == remote_items:
                    report.skipped += 1
                    continue
                last_synced = max(
                    (
                        record.last_synced_at
                        for record in session.scalars(
                            select(SyncRecord).where(
                                SyncRecord.entity_type == "budgets",
                                SyncRecord.entity_id.in_([i.id for i in local_items]),
                                SyncRecord.direction == "push",
                            )
                        )
                        if record.last_synced_at
                    ),
                    default=None,
                )
                if last_synced is not None and local.updated_at > last_synced:
                    report.conflicts.append(
                        {
                            "entity_id": local.id,
                            "reason": "本地预算在上次同步后被修改，拒绝覆盖",
                        }
                    )
                    continue
                name = local.name
            try:
                new_budget = service.set_budget(
                    period=payload.period,
                    items=[
                        {"category": i.category, "amount_minor": i.amount_minor}
                        for i in payload.items
                    ],
                    name=name,
                    actor="feishu",
                )
            except ValidationError as exc:
                report.failed.append({"entity_id": period, "error": str(exc)})
                continue
            self._mark(
                "budgets", new_budget.id, direction="pull", status=SyncStatus.SYNCED,
                remote_record_id=None,
                synced=True,
                session=session,
            )
            report.updated += 1

    # -------------------------------------------------------------- conflicts
    def conflicts(self) -> list[dict]:
        with self.db.session() as session:
            rows = session.scalars(
                select(SyncRecord).where(SyncRecord.status == SyncStatus.CONFLICT)
            )
            return [
                {
                    "entity_type": r.entity_type,
                    "entity_id": r.entity_id,
                    "direction": r.direction,
                    "remote_record_id": r.remote_record_id,
                    "reason": r.last_error,
                    "updated_at": r.updated_at.isoformat(timespec="seconds"),
                }
                for r in rows
            ]

    def resolve(
        self, entity: str, entity_id: str, *, strategy: str = "local"
    ) -> SyncReport:
        if strategy not in {"local", "remote"}:
            raise ValidationError("strategy 只能是 local 或 remote")
        if entity not in {"goals", "budgets"} and strategy == "remote":
            raise ValidationError("只有 goals/budgets 支持以远端为准")
        report = SyncReport(entity=entity)
        if strategy == "local":
            builder = getattr(self, f"_payloads_{entity}")
            with self.db.session() as session:
                household = get_household(session)
                fields = dict(builder(session, household.id)).get(entity_id)
            if fields is None:
                raise ValidationError(f"本地记录不存在: {entity_id}")
            spec = self.specs.get(entity)
            if spec is not None and spec.periodic == "month":
                period = _period_of(fields)
                if not period:
                    raise ValidationError(f"记录缺少日期，无法确定月份表: {entity_id}")
                table_id = self._period_table_id(entity, period)
            else:
                table_id = self.table_ids(ensure=False)[entity]
            self._push_one(
                entity, entity_id, fields, table_id, dry_run=False, report=report, force=True
            )
            return report
        # remote wins: clear the conflict marker and pull that entity
        with self.db.session() as session:
            sync = self._get_sync(entity, entity_id, "push", session=session) or self._get_sync(
                entity, entity_id, "pull", session=session
            )
            if sync is not None:
                sync.status = SyncStatus.PENDING
                sync.last_synced_at = None
                sync.payload_hash = None
        return self.pull(entity)

    # ---------------------------------------------------------------- helpers
    def _get_sync(
        self, entity: str, entity_id: str, direction: str, *, session: Session | None = None
    ) -> SyncRecord | None:
        statement = select(SyncRecord).where(
            SyncRecord.entity_type == entity,
            SyncRecord.entity_id == entity_id,
            SyncRecord.direction == direction,
        )
        if session is not None:
            return session.scalar(statement)
        with self.db.session() as own_session:
            return own_session.scalar(statement)

    def _mark(
        self,
        entity: str,
        entity_id: str,
        *,
        direction: str,
        status: str,
        remote_table: str | None = None,
        remote_record_id: str | None = None,
        payload: str | None = None,
        error: str | None = None,
        synced: bool = False,
        attempted: bool = False,
        session: Session | None = None,
    ) -> None:
        if session is not None:
            self._apply_mark(
                session, entity, entity_id, direction=direction, status=status,
                remote_table=remote_table, remote_record_id=remote_record_id,
                payload=payload, error=error, synced=synced, attempted=attempted,
            )
            return
        with self.db.session() as own_session:
            self._apply_mark(
                own_session, entity, entity_id, direction=direction, status=status,
                remote_table=remote_table, remote_record_id=remote_record_id,
                payload=payload, error=error, synced=synced, attempted=attempted,
            )

    def _apply_mark(
        self,
        session: Session,
        entity: str,
        entity_id: str,
        *,
        direction: str,
        status: str,
        remote_table: str | None,
        remote_record_id: str | None,
        payload: str | None,
        error: str | None,
        synced: bool,
        attempted: bool,
    ) -> None:
        record = session.scalar(
            select(SyncRecord).where(
                SyncRecord.entity_type == entity,
                SyncRecord.entity_id == entity_id,
                SyncRecord.direction == direction,
            )
        )
        if record is None:
            record = SyncRecord(
                id=new_id("sync"),
                household_id=get_household(session).id,
                entity_type=entity,
                entity_id=entity_id,
                direction=direction,
                attempts=0,
            )
            session.add(record)
        record.status = status
        record.last_error = error
        if remote_table is not None:
            record.remote_table = remote_table
        if remote_record_id is not None:
            record.remote_record_id = remote_record_id
        if payload is not None:
            record.payload_hash = payload
        if attempted:
            record.attempts += 1
        elif synced:
            record.attempts = 0
        if synced:
            record.last_synced_at = datetime.now().replace(microsecond=0)

    def _set_config(self, session: Session, key: str, value: str) -> None:
        row = session.get(SystemConfig, key)
        if row is None:
            session.add(SystemConfig(key=key, value=value))
        else:
            row.value = value


def _cutoff_period(keep_months: int) -> str:
    """Return the oldest ``YYYY-MM`` period that should stay on Feishu."""
    today = date.today()
    total = today.year * 12 + (today.month - 1) - keep_months
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}"


def _period_of(fields: dict) -> str | None:
    """Extract ``YYYY-MM`` from a payload's date field (ms epoch or string)."""
    raw = fields.get("日期")
    if raw in (None, "", 0):
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(int(raw) / 1000).strftime("%Y-%m")
    text = str(raw)
    if len(text) >= 7 and text[4] == "-":
        return text[:7]
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts).strip() or None
    return str(value).strip() or None


def _from_ms(value: Any) -> date | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000).date()
    except (ValueError, OSError, OverflowError):
        return None


def _client_token(entity: str, entity_id: str) -> str:
    """Deterministic UUIDv4-shaped idempotency key.

    Feishu deduplicates repeated creates with the same client_token, so a
    timeout/retry cannot create two remote rows for one local entity.
    """
    digest = bytearray(hashlib.sha256(f"family-cfo:{entity}:{entity_id}".encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _goal_fields(goal: FinancialGoal) -> dict:
    gap = max(goal.target_minor - goal.current_minor, 0)
    return {
        "业务ID": goal.id,
        "名称": goal.name,
        "类型": goal.goal_type,
        "目标金额": _yuan(goal.target_minor),
        "当前金额": _yuan(goal.current_minor),
        "目标日期": _ms(goal.target_date),
        "每月储蓄": _yuan(goal.monthly_saving_minor) if goal.monthly_saving_minor else 0.0,
        "缺口": _yuan(gap),
        "完成率": float(Decimal(goal.current_minor) / Decimal(goal.target_minor))
        if goal.target_minor
        else 0.0,
        "风险承受": goal.risk_tolerance,
    }
