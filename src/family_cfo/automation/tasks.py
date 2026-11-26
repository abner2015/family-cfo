"""Automation task implementations (called by the CLI and by cron)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from ..assets.service import AssetService
from ..core.config import Settings
from ..core.constants import TxnStatus
from ..core.dates import parse_period, previous_period, today_local
from ..core.money import format_money
from ..database.backup import backup_database
from ..database.base import Database
from ..database.init import get_household
from ..database.models import Liability, Transaction, TransactionCategory
from ..finance.metrics import balance_sheet
from ..finance.settlement import SettlementService
from ..importers.service import ImportService
from ..investments.service import InvestmentService
from ..planning.service import PlanningService
from ..reports.excel import export_monthly
from ..reports.monthly import save_monthly
from ..transactions.reconcile import Reconciler


def weekly_scan(db: Database, settings: Settings) -> dict:
    reports = ImportService(db, settings).scan_inbox()
    return {
        "files": len(reports),
        "imported": sum(r.imported for r in reports),
        "duplicates": sum(1 for r in reports if r.status == "duplicate"),
        "failed": sum(1 for r in reports if r.status == "failed"),
        "details": [r.as_dict() for r in reports],
    }


def weekly_digest(db: Database, settings: Settings) -> dict:
    end = today_local(settings.timezone)
    start = end - timedelta(days=6)
    with db.session() as session:
        household = get_household(session)
        rows = list(
            session.scalars(
                select(Transaction).where(
                    Transaction.household_id == household.id,
                    Transaction.occurred_at >= datetime.combine(start, datetime.min.time()),
                    Transaction.occurred_at
                    <= datetime.combine(end, datetime.max.time().replace(microsecond=0)),
                    Transaction.is_deleted.is_(False),
                    Transaction.status != TxnStatus.VOID,
                )
            )
        )
        categories = {c.id: c.name for c in session.scalars(select(TransactionCategory))}
        income = sum(
            t.amount_base_minor
            for t in rows
            if t.stats_included and t.txn_type == "income" and t.direction == "inflow"
        )
        refund = sum(
            t.amount_base_minor
            for t in rows
            if t.stats_included and t.txn_type == "refund" and t.direction == "inflow"
        )
        expense = sum(
            t.amount_base_minor
            for t in rows
            if t.stats_included and t.txn_type in {"expense", "fee"} and t.direction == "outflow"
        )
        net_expense = max(expense - refund, 0)
        by_category: dict[str, int] = {}
        for txn in rows:
            if txn.stats_included and txn.txn_type in {"expense", "fee"} and txn.direction == "outflow":
                name = categories.get(txn.category_id, "未分类")
                by_category[name] = by_category.get(name, 0) + txn.amount_base_minor
        pending = [t.id for t in rows if t.status == TxnStatus.PENDING_REVIEW]
    top = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)[:8]
    body = [
        f"# {start.isoformat()} ~ {end.isoformat()} 家庭周报",
        "",
        f"- 收入：{format_money(income, settings.currency)}",
        f"- 支出（已扣退款）：{format_money(net_expense, settings.currency)}",
        f"- 结余：{format_money(income - net_expense, settings.currency)}",
        f"- 待确认交易：{len(pending)} 笔",
        "",
        "## 支出分类 Top",
        "",
    ]
    body += [f"- {name}：{format_money(value, settings.currency)}" for name, value in top] or ["- 无支出记录"]
    markdown = "\n".join(body)
    target = settings.reports_dir / f"{start.isoformat()}_{end.isoformat()}-weekly.md"
    target.write_text(markdown, encoding="utf-8")
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "income_minor": income,
        "expense_minor": net_expense,
        "net_minor": income - net_expense,
        "pending": len(pending),
        "report": str(target),
    }


def monthly_collect(db: Database, settings: Settings, period: str | None = None) -> dict:
    period = parse_period(period or previous_period(_current_period(settings)))
    backup_path = None
    if settings.auto_backup and settings.db_path.exists():
        backup_path = str(
            backup_database(
                settings.db_path, settings.backups_dir, label=f"pre-collect-{period}"
            ).path
        )
    scan = weekly_scan(db, settings)
    with db.session() as session:
        reconciler = Reconciler(session, settings, period=period)
        reconcile = reconciler.run()
        settlement = SettlementService(session, settings).evaluate(period)
    return {
        "period": period,
        "backup": backup_path,
        "scan": {k: scan[k] for k in ("files", "imported", "duplicates", "failed")},
        "reconcile": reconcile.as_dict(),
        "settlement": settlement.as_dict(),
    }


def monthly_report(
    db: Database, settings: Settings, period: str, *, excel: bool = True
) -> dict:
    with db.session() as session:
        report, markdown, md_path = save_monthly(session, settings, period)
        xlsx_path = None
        if excel:
            xlsx_path = settings.reports_dir / f"{period}-monthly-report.xlsx"
            export_monthly(session, settings, report, xlsx_path)
    return {
        "period": period,
        "markdown": str(md_path),
        "excel": str(xlsx_path) if xlsx_path else None,
        "status": report["settlement"]["status"],
        "ready": report["settlement"]["ready"],
    }


def quarterly_review(db: Database, settings: Settings) -> dict:
    with db.session() as session:
        assets = AssetService(session, settings)
        stale = [line for line in assets.list() if line.stale]
        portfolio = InvestmentService(session, settings).portfolio()
        stale_positions = [
            p for p in portfolio.positions if p.valuation_date is None or (date.today() - p.valuation_date).days > 90
        ]
        liabilities = list(
            session.scalars(
                select(Liability).where(
                    Liability.household_id == get_household(session).id,
                    Liability.is_active.is_(True),
                )
            )
        )
        upcoming = [l for l in liabilities if l.next_payment_date and l.next_payment_date <= date.today() + timedelta(days=30)]
        sheet = balance_sheet(session, get_household(session).id, as_of=date.today(), settings=settings)
    return {
        "as_of": date.today().isoformat(),
        "stale_assets": [{"id": a.id, "name": a.name, "valuation_date": a.valuation_date} for a in stale],
        "stale_positions": [{"name": p.name, "valuation_date": str(p.valuation_date)} for p in stale_positions],
        "upcoming_payments": [
            {"name": l.name, "date": str(l.next_payment_date), "amount": format_money(l.monthly_payment_minor)}
            for l in upcoming
        ],
        "net_worth": format_money(sheet.net_worth_minor),
        "message": "请更新上述资产/持仓估值，并核对下月还款计划。",
    }


def annual_planning(db: Database, settings: Settings) -> dict:
    with db.session() as session:
        plan = PlanningService(session, settings).plan()
        sheet = balance_sheet(session, get_household(session).id, as_of=date.today(), settings=settings)
    return {
        "year": date.today().year + 1,
        "net_worth": format_money(sheet.net_worth_minor),
        "goals": plan["goals"],
        "emergency_fund": plan["emergency_fund"],
        "assumptions": plan["assumptions"],
        "note": "规划基于当前台账与明确假设；请在年初与家人一起复核。",
    }


def run_job(name: str, db: Database, settings: Settings, *, period: str | None = None) -> dict:
    mapping = {
        "weekly_scan": lambda: weekly_scan(db, settings),
        "weekly_summary": lambda: weekly_digest(db, settings),
        "monthly_collect": lambda: monthly_collect(db, settings, period),
        "monthly_close_reminder": lambda: _settlement_status(db, settings, period),
        "monthly_report": lambda: monthly_report(db, settings, parse_period(period or previous_period(_current_period(settings)))),
        "quarterly_review": lambda: quarterly_review(db, settings),
        "annual_planning": lambda: annual_planning(db, settings),
    }
    if name not in mapping:
        raise ValueError(f"未知任务: {name}")
    return mapping[name]()


def _settlement_status(db: Database, settings: Settings, period: str | None) -> dict:
    with db.session() as session:
        value = SettlementService(session, settings).evaluate(period or _current_period(settings))
    return value.as_dict()


def _current_period(settings: Settings) -> str:
    return today_local(settings.timezone).strftime("%Y-%m")
