"""Financial metrics.

Every metric carries its formula and data source so a report can explain
itself.  Metrics that cannot be computed from real data are returned with
``status="unavailable"`` instead of a guessed number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import (
    TxnStatus,
    TxnType,
)
from ..core.dates import add_months, month_bounds, month_end, previous_period
from ..core.ids import new_id
from ..core.money import DEFAULT_CURRENCY, format_money, from_minor, pct, ratio, safe_div
from ..database.models import (
    Account,
    Asset,
    FinancialGoal,
    FinancialSnapshot,
    InvestmentPosition,
    InvestmentProduct,
    Liability,
    Transaction,
    TransactionCategory,
)

ESSENTIAL_SLUGS_FALLBACK = {
    "food",
    "groceries",
    "housing",
    "utilities",
    "transport",
    "healthcare",
    "education",
    "insurance",
}


@dataclass
class Metric:
    key: str
    label: str
    value: str | None
    raw: str | None
    unit: str
    formula: str
    sources: list[str]
    status: str = "actual"  # actual | estimate | assumption | unavailable
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "raw": self.raw,
            "unit": self.unit,
            "formula": self.formula,
            "sources": self.sources,
            "status": self.status,
            "note": self.note,
        }


@dataclass
class PeriodSummary:
    period: str
    income_minor: int = 0
    expense_minor: int = 0
    refund_minor: int = 0
    net_minor: int = 0
    transaction_count: int = 0
    essential_expense_minor: int = 0
    fixed_expense_minor: int = 0
    currency: str = DEFAULT_CURRENCY

    @property
    def savings_rate(self) -> Decimal | None:
        return ratio(self.net_minor, self.income_minor)

    @property
    def expense_ratio_of_income(self) -> Decimal | None:
        return ratio(self.expense_minor, self.income_minor)

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "currency": self.currency,
            "income": str(from_minor(self.income_minor, self.currency)),
            "expense": str(from_minor(self.expense_minor, self.currency)),
            "refund": str(from_minor(self.refund_minor, self.currency)),
            "net": str(from_minor(self.net_minor, self.currency)),
            "income_minor": self.income_minor,
            "expense_minor": self.expense_minor,
            "net_minor": self.net_minor,
            "savings_rate": str(self.savings_rate) if self.savings_rate is not None else None,
            "transaction_count": self.transaction_count,
            "essential_expense_minor": self.essential_expense_minor,
            "fixed_expense_minor": self.fixed_expense_minor,
        }


@dataclass
class BalanceSheet:
    as_of: date
    currency: str = DEFAULT_CURRENCY
    account_assets_minor: int = 0
    other_assets_minor: int = 0
    investment_assets_minor: int = 0
    total_assets_minor: int = 0
    liabilities_minor: int = 0
    net_worth_minor: int = 0
    liquid_assets_minor: int = 0
    primary_residence_minor: int = 0
    has_data: bool = False
    has_liability_data: bool = False
    excluded_currencies: set[str] = field(default_factory=set)
    stale_valuations: list[str] = field(default_factory=list)
    estimated_values: list[str] = field(default_factory=list)

    @property
    def liquid_ratio(self) -> Decimal | None:
        return ratio(self.liquid_assets_minor, self.total_assets_minor)

    @property
    def debt_ratio(self) -> Decimal | None:
        if self.total_assets_minor == 0:
            return None
        return ratio(self.liabilities_minor, self.total_assets_minor)

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "currency": self.currency,
            "account_assets": str(from_minor(self.account_assets_minor, self.currency)),
            "other_assets": str(from_minor(self.other_assets_minor, self.currency)),
            "investment_assets": str(from_minor(self.investment_assets_minor, self.currency)),
            "total_assets": str(from_minor(self.total_assets_minor, self.currency)),
            "liabilities": str(from_minor(self.liabilities_minor, self.currency)),
            "net_worth": str(from_minor(self.net_worth_minor, self.currency)),
            "liquid_assets": str(from_minor(self.liquid_assets_minor, self.currency)),
            "primary_residence": str(from_minor(self.primary_residence_minor, self.currency)),
            "liquid_ratio": str(self.liquid_ratio) if self.liquid_ratio is not None else None,
            "debt_ratio": str(self.debt_ratio) if self.debt_ratio is not None else None,
            "total_assets_minor": self.total_assets_minor,
            "account_assets_minor": self.account_assets_minor,
            "other_assets_minor": self.other_assets_minor,
            "investment_assets_minor": self.investment_assets_minor,
            "total_liabilities_minor": self.liabilities_minor,
            "net_worth_minor": self.net_worth_minor,
            "liquid_assets_minor": self.liquid_assets_minor,
            "primary_residence_minor": self.primary_residence_minor,
            "has_data": self.has_data,
            "has_liability_data": self.has_liability_data,
            "excluded_currencies": sorted(self.excluded_currencies),
            "stale_valuations": self.stale_valuations,
            "estimated_values": self.estimated_values,
        }


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    start, end = month_bounds(period)
    return (
        datetime.combine(start, datetime.min.time()),
        datetime.combine(end, datetime.max.time().replace(microsecond=0)),
    )


def _include_txn(txn: Transaction) -> bool:
    return (
        not txn.is_deleted
        and txn.status != TxnStatus.VOID
        and txn.stats_included
    )


def period_summary(
    session: Session, household_id: str, period: str, *, currency: str = DEFAULT_CURRENCY
) -> PeriodSummary:
    start, end = _period_bounds(period)
    txns = list(
        session.scalars(
            select(Transaction).where(
                Transaction.household_id == household_id,
                Transaction.occurred_at >= start,
                Transaction.occurred_at <= end,
                Transaction.is_deleted.is_(False),
                Transaction.status != TxnStatus.VOID,
            )
        )
    )
    categories = {
        c.id: c
        for c in session.scalars(select(TransactionCategory))
    }
    summary = PeriodSummary(period=period, currency=currency)
    for txn in txns:
        if not _include_txn(txn):
            continue
        amount = txn.amount_base_minor
        if txn.txn_type == TxnType.INCOME and txn.direction == "inflow":
            summary.income_minor += amount
            summary.transaction_count += 1
            continue
        if txn.txn_type == TxnType.REFUND and txn.direction == "inflow":
            summary.refund_minor += amount
            summary.transaction_count += 1
            category = categories.get(txn.category_id) if txn.category_id else None
            if category is not None and (
                category.is_essential or category.slug in ESSENTIAL_SLUGS_FALLBACK
            ):
                summary.essential_expense_minor -= amount
                summary.fixed_expense_minor -= amount
            continue
        if txn.txn_type in {TxnType.EXPENSE, TxnType.FEE} and txn.direction == "outflow":
            summary.expense_minor += amount
            summary.transaction_count += 1
            category = categories.get(txn.category_id) if txn.category_id else None
            if category is not None:
                if category.is_essential or category.slug in ESSENTIAL_SLUGS_FALLBACK:
                    summary.essential_expense_minor += amount
                    summary.fixed_expense_minor += amount
    summary.expense_minor = summary.expense_minor - summary.refund_minor
    summary.net_minor = summary.income_minor - summary.expense_minor
    return summary


def net_flow_by_category(
    session: Session, household_id: str, period: str
) -> list[dict]:
    start, end = _period_bounds(period)
    rows = session.execute(
        select(
            TransactionCategory.name,
            TransactionCategory.slug,
            Transaction.direction,
            Transaction.txn_type,
            func.sum(Transaction.amount_base_minor),
            func.count(Transaction.id),
        )
        .join(TransactionCategory, Transaction.category_id == TransactionCategory.id, isouter=True)
        .where(
            Transaction.household_id == household_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at <= end,
            Transaction.is_deleted.is_(False),
            Transaction.status != TxnStatus.VOID,
            Transaction.stats_included.is_(True),
            Transaction.txn_type.in_(
                [TxnType.EXPENSE, TxnType.INCOME, TxnType.REFUND, TxnType.FEE]
            ),
        )
        .group_by(
            TransactionCategory.name, TransactionCategory.slug, Transaction.direction, Transaction.txn_type
        )
        .order_by(func.sum(Transaction.amount_base_minor).desc())
    ).all()
    # Refunds are negative expenses of their original category, never income.
    grouped: dict[tuple[str | None, str], dict] = {}
    refunds: dict[str | None, dict] = {}
    for name, slug, direction, txn_type, total, count in rows:
        if txn_type == TxnType.REFUND and direction == "inflow":
            bucket = refunds.setdefault(
                slug,
                {"category": name or "未分类", "amount_minor": 0, "count": 0},
            )
            bucket["amount_minor"] += int(total or 0)
            bucket["count"] += int(count)
            continue
        key = (slug, direction)
        entry = grouped.setdefault(
            key,
            {
                "category": name or "未分类",
                "slug": slug,
                "direction": direction,
                "txn_type": txn_type,
                "amount_minor": 0,
                "count": 0,
            },
        )
        entry["amount_minor"] += int(total or 0)
        entry["count"] += int(count)

    for slug, refund in refunds.items():
        key = (slug, "outflow")
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "category": refund["category"],
                "slug": slug,
                "direction": "outflow",
                "txn_type": TxnType.REFUND,
                "amount_minor": 0,
                "count": 0,
            }
            grouped[key] = entry
        entry["amount_minor"] -= refund["amount_minor"]
        entry["count"] += refund["count"]

    out = []
    for entry in grouped.values():
        entry["amount"] = str(from_minor(entry["amount_minor"]))
        out.append(entry)
    out.sort(key=lambda item: item["amount_minor"], reverse=True)
    return out


def balance_sheet(
    session: Session,
    household_id: str,
    *,
    as_of: date | None = None,
    currency: str = DEFAULT_CURRENCY,
    settings: Settings | None = None,
) -> BalanceSheet:
    as_of = as_of or date.today()
    sheet = BalanceSheet(as_of=as_of, currency=currency)
    for account in session.scalars(
        select(Account).where(
            Account.household_id == household_id,
            Account.is_active.is_(True),
        )
    ):
        if account.currency != currency:
            sheet.excluded_currencies.add(account.currency)
            continue
        if account.is_liability:
            sheet.liabilities_minor += max(account.balance_minor, 0)
            if account.balance_as_of is not None:
                sheet.has_liability_data = True
        elif account.is_asset:
            if account.balance_as_of is not None:
                sheet.has_data = True
            if account.balance_minor >= 0:
                sheet.account_assets_minor += account.balance_minor
                if account.account_type in {"bank", "cash", "payment_platform"}:
                    sheet.liquid_assets_minor += account.balance_minor
            else:
                sheet.liabilities_minor += -account.balance_minor

    for asset in session.scalars(
        select(Asset).where(Asset.household_id == household_id, Asset.is_active.is_(True))
    ):
        if asset.currency != currency:
            sheet.excluded_currencies.add(asset.currency)
            continue
        sheet.has_data = True
        value = int(asset.current_value_minor * asset.ownership_share_bps / 10000)
        sheet.other_assets_minor += value
        if asset.is_primary_residence:
            sheet.primary_residence_minor += value
        elif asset.is_liquid:
            sheet.liquid_assets_minor += value
        if asset.valuation_date is None:
            sheet.estimated_values.append(asset.name)
        elif (as_of - asset.valuation_date).days > 365:
            sheet.stale_valuations.append(asset.name)

    investment_total = 0
    for liability in session.scalars(
        select(Liability).where(
            Liability.household_id == household_id, Liability.is_active.is_(True)
        )
    ):
        if liability.currency != currency:
            sheet.excluded_currencies.add(liability.currency)
            continue
        sheet.has_liability_data = True
        sheet.liabilities_minor += max(liability.balance_minor, 0)

    for position in session.scalars(
        select(InvestmentPosition).where(InvestmentPosition.household_id == household_id)
    ):
        product = session.get(InvestmentProduct, position.product_id)
        if product is not None and product.currency != currency:
            sheet.excluded_currencies.add(product.currency)
            continue
        if position.quantity <= 0:
            continue
        sheet.has_data = True
        if position.market_value_minor is not None:
            value = position.market_value_minor
            if position.valuation_date is None:
                sheet.estimated_values.append(f"持仓 {product.name if product else position.product_id}")
            elif (as_of - position.valuation_date).days > 90:
                sheet.stale_valuations.append(f"持仓 {product.name if product else position.product_id}")
        else:
            value = position.cost_minor
            sheet.estimated_values.append(f"持仓成本 {product.name if product else position.product_id}")
        investment_total += value
        if position.valuation_date is None:
            sheet.liquid_assets_minor += 0
    sheet.investment_assets_minor = investment_total

    sheet.total_assets_minor = (
        sheet.account_assets_minor + sheet.other_assets_minor + sheet.investment_assets_minor
    )
    sheet.net_worth_minor = sheet.total_assets_minor - sheet.liabilities_minor
    return sheet


def average_monthly_expense(
    session: Session, household_id: str, *, period: str, months: int = 3, essential: bool = True
) -> tuple[int, int]:
    """Return (average minor units, number of months with data)."""
    values: list[int] = []
    for offset in range(1, months + 1):
        summary = period_summary(session, household_id, previous_period(period, offset))
        value = summary.essential_expense_minor if essential else summary.expense_minor
        if value > 0:
            values.append(value)
    if not values:
        current = period_summary(session, household_id, period)
        value = current.essential_expense_minor if essential else current.expense_minor
        if value > 0:
            values.append(value)
    if not values:
        return 0, 0
    return sum(values) // len(values), len(values)


def budget_execution(session: Session, household_id: str, period: str, settings: Settings) -> dict | None:
    from ..budgets.service import BudgetService

    report = BudgetService(session, settings).execution(period)
    return report.as_dict() if report else None


def monthly_trend(session: Session, household_id: str, *, end_period: str, months: int = 6) -> list[PeriodSummary]:
    periods = [add_months(end_period, -i) for i in range(months - 1, -1, -1)]
    return [period_summary(session, household_id, p) for p in periods]


def build_metrics(
    session: Session,
    settings: Settings,
    *,
    period: str,
    as_of: date | None = None,
) -> dict:
    household_id = _household_id(session)
    summary = period_summary(session, household_id, period, currency=settings.currency)
    sheet = balance_sheet(session, household_id, as_of=as_of, currency=settings.currency)
    avg_expense, months_used = average_monthly_expense(
        session, household_id, period=period, months=3, essential=True
    )
    emergency_months = (
        Decimal(sheet.liquid_assets_minor) / Decimal(avg_expense)
        if avg_expense > 0
        else None
    )

    liabilities = [
        l
        for l in session.scalars(
            select(Liability).where(
                Liability.household_id == household_id, Liability.is_active.is_(True)
            )
        )
        if l.currency == settings.currency
    ]
    monthly_debt_service = sum(l.monthly_payment_minor for l in liabilities)
    income_values = []
    for offset in range(1, 4):
        s = period_summary(session, household_id, previous_period(period, offset))
        if s.income_minor > 0:
            income_values.append(s.income_minor)
    if not income_values and summary.income_minor > 0:
        income_values.append(summary.income_minor)
    avg_income_value = sum(income_values) // len(income_values) if income_values else 0

    debt_service_ratio = (
        safe_div(Decimal(monthly_debt_service), Decimal(avg_income_value))
        if avg_income_value > 0
        else None
    )
    fixed_ratio = (
        safe_div(Decimal(summary.essential_expense_minor), Decimal(summary.expense_minor))
        if summary.expense_minor > 0
        else None
    )

    investment_total = sheet.investment_assets_minor
    allocation: list[dict] = []
    by_type: dict[str, int] = {}
    for position in session.scalars(
        select(InvestmentPosition).where(InvestmentPosition.household_id == household_id)
    ):
        if position.quantity <= 0:
            continue
        product = session.get(InvestmentProduct, position.product_id)
        if product is not None and product.currency != settings.currency:
            continue
        ptype = product.product_type if product else "other"
        value = position.market_value_minor if position.market_value_minor is not None else position.cost_minor
        by_type[ptype] = by_type.get(ptype, 0) + value
    for ptype, value in sorted(by_type.items(), key=lambda kv: kv[1], reverse=True):
        allocation.append(
            {
                "type": ptype,
                "amount": str(from_minor(value, settings.currency)),
                "amount_minor": value,
                "share": str(ratio(value, investment_total)) if investment_total else None,
            }
        )

    goals = list(
        session.scalars(
            select(FinancialGoal).where(
                FinancialGoal.household_id == household_id, FinancialGoal.status == "active"
            )
        )
    )
    goal_rows = []
    total_target = sum(g.target_minor for g in goals)
    total_current = sum(min(g.current_minor, g.target_minor) for g in goals)
    for goal in goals:
        goal_rows.append(
            {
                "id": goal.id,
                "name": goal.name,
                "progress": str(ratio(goal.current_minor, goal.target_minor)) if goal.target_minor else None,
                "gap": str(from_minor(max(goal.target_minor - goal.current_minor, 0), settings.currency)),
                "target_date": goal.target_date.isoformat() if goal.target_date else None,
                "monthly_saving": str(from_minor(goal.monthly_saving_minor, settings.currency))
                if goal.monthly_saving_minor is not None
                else None,
            }
        )

    snapshot_prev = session.scalar(
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.household_id == household_id,
            FinancialSnapshot.as_of <= month_end(add_months(period, -12)),
        )
        .order_by(FinancialSnapshot.as_of.desc())
        .limit(1)
    )
    net_worth_change = None
    if snapshot_prev is not None:
        net_worth_change = format_money(
            sheet.net_worth_minor - snapshot_prev.net_worth_minor, settings.currency
        )

    asset_notes: list[str] = []
    if not sheet.has_data:
        asset_notes.append("尚未录入账户余额或资产，无法判断总资产")
    elif sheet.estimated_values:
        asset_notes.append("部分资产缺少估值: " + "、".join(sheet.estimated_values[:5]))
    if sheet.excluded_currencies:
        asset_notes.append("未折算币种: " + "、".join(sorted(sheet.excluded_currencies)))

    metrics: list[Metric] = [
        Metric(
            "month_income",
            "月度收入",
            format_money(summary.income_minor, settings.currency),
            str(summary.income_minor),
            "CNY",
            "统计期内 direction=inflow 且 txn_type=income 的金额之和",
            ["transactions"],
        ),
        Metric(
            "month_expense",
            "月度支出（已扣退款）",
            format_money(summary.expense_minor, settings.currency),
            str(summary.expense_minor),
            "CNY",
            "支出与手续费之和 - 退款金额",
            ["transactions", "refunds"],
        ),
        Metric(
            "month_net",
            "月度结余",
            format_money(summary.net_minor, settings.currency),
            str(summary.net_minor),
            "CNY",
            "月度收入 - 月度支出",
            ["transactions"],
        ),
        Metric(
            "savings_rate",
            "储蓄率",
            pct(summary.net_minor, summary.income_minor),
            str(summary.savings_rate) if summary.savings_rate is not None else None,
            "%",
            "月度结余 / 月度收入",
            ["transactions"],
            status="actual" if summary.income_minor else "unavailable",
            note=None if summary.income_minor else "本月无收入记录，储蓄率不计算",
        ),
        Metric(
            "total_assets",
            "家庭总资产",
            format_money(sheet.total_assets_minor, settings.currency),
            str(sheet.total_assets_minor),
            "CNY",
            "账户资产 + 非账户资产 + 投资市值",
            ["accounts", "assets", "investment_positions"],
            status="unavailable" if not sheet.has_data else ("estimate" if sheet.estimated_values else "actual"),
            note="；".join(asset_notes) or None,
        ),        Metric(
            "total_liabilities",
            "家庭总负债",
            format_money(sheet.liabilities_minor, settings.currency),
            str(sheet.liabilities_minor),
            "CNY",
            "负债余额 + 信用卡欠款",
            ["liabilities", "accounts"],
            status="actual" if sheet.has_liability_data else "unavailable",
            note=None if sheet.has_liability_data else "尚未录入负债，无法判断负债总额",
        ),
        Metric(
            "net_worth",
            "家庭净资产",
            format_money(sheet.net_worth_minor, settings.currency),
            str(sheet.net_worth_minor),
            "CNY",
            "总资产 - 总负债",
            ["accounts", "assets", "liabilities", "investment_positions"],
            status="actual" if sheet.has_data or sheet.has_liability_data else "unavailable",
            note=None if (sheet.has_data or sheet.has_liability_data) else "尚未录入资产负债，无法判断净资产",
        ),
        Metric(
            "debt_ratio",
            "资产负债率",
            pct(sheet.liabilities_minor, sheet.total_assets_minor),
            str(sheet.debt_ratio) if sheet.debt_ratio is not None else None,
            "%",
            "总负债 / 总资产",
            ["liabilities", "assets"],
            status="actual" if sheet.total_assets_minor else "unavailable",
        ),
        Metric(
            "liquid_ratio",
            "流动资产比例",
            pct(sheet.liquid_assets_minor, sheet.total_assets_minor),
            str(sheet.liquid_ratio) if sheet.liquid_ratio is not None else None,
            "%",
            "流动资产 / 总资产（自住房不计入流动资产）",
            ["accounts", "assets"],
            status="actual" if (sheet.has_data and sheet.total_assets_minor) else "unavailable",
        ),
        Metric(
            "emergency_months",
            "应急资金覆盖月数",
            f"{emergency_months:.1f} 个月" if emergency_months is not None else None,
            f"{emergency_months:.2f}" if emergency_months is not None else None,
            "月",
            "流动资产 / 近3个月平均必要支出",
            ["accounts", "assets", "transactions"],
            status="actual" if (emergency_months is not None and sheet.has_data) else "unavailable",
            note=(
                "尚未录入流动资产"
                if not sheet.has_data
                else (f"必要支出基于 {months_used} 个月数据" if months_used else "缺少支出数据")
            ),
        ),
        Metric(
            "fixed_expense_ratio",
            "固定/必要支出比例",
            pct(summary.essential_expense_minor, summary.expense_minor),
            str(fixed_ratio) if fixed_ratio is not None else None,
            "%",
            "必要支出 / 总支出",
            ["transactions", "transaction_categories"],
            status="actual" if summary.expense_minor else "unavailable",
        ),
        Metric(
            "debt_service_ratio",
            "家庭债务负担率",
            pct(monthly_debt_service, avg_income_value) if avg_income_value else None,
            str(debt_service_ratio) if debt_service_ratio is not None else None,
            "%",
            "月还款额 / 近3个月平均月收入",
            ["liabilities", "transactions"],
            status="actual" if avg_income_value else "unavailable",
            note=None if avg_income_value else "缺少收入数据，债务负担率不计算",
        ),
        Metric(
            "investment_allocation",
            "投资资产配置",
            "；".join(f"{a['type']} {a['share']}" for a in allocation) if allocation else "无持仓",
            None,
            "%",
            "各产品类型市值 / 投资总市值",
            ["investment_positions", "investment_products"],
            status="actual" if allocation else "unavailable",
        ),
        Metric(
            "net_worth_change",
            "年度净资产变化",
            net_worth_change,
            None,
            "CNY",
            "当前净资产 - 12个月前快照净资产",
            ["financial_snapshots"],
            status="actual" if net_worth_change is not None else "unavailable",
            note=None if net_worth_change is not None else "缺少 12 个月前的净资产快照",
        ),
        Metric(
            "goal_completion",
            "家庭目标完成率",
            pct(total_current, total_target) if total_target else None,
            str(ratio(total_current, total_target)) if total_target else None,
            "%",
            "各目标当前金额之和 / 目标金额之和（单目标封顶 100%）",
            ["financial_goals"],
            status="actual" if goals else "unavailable",
        ),
    ]

    return {
        "period": period,
        "currency": settings.currency,
        "summary": summary.as_dict(),
        "balance_sheet": sheet.as_dict(),
        "metrics": [m.as_dict() for m in metrics],
        "categories": net_flow_by_category(session, household_id, period),
        "investment_allocation": allocation,
        "goals": goal_rows,
        "data_quality": {
            "estimated_assets": sheet.estimated_values,
            "stale_valuations": sheet.stale_valuations,
            "avg_expense_months": months_used,
        },
    }


def capture_snapshot(
    session: Session, household_id: str, *, as_of: date, settings: Settings
) -> FinancialSnapshot:
    period = f"{as_of.year:04d}-{as_of.month:02d}"
    summary = period_summary(session, household_id, period, currency=settings.currency)
    sheet = balance_sheet(session, household_id, as_of=as_of, currency=settings.currency)
    existing = session.scalar(
        select(FinancialSnapshot).where(
            FinancialSnapshot.household_id == household_id, FinancialSnapshot.as_of == as_of
        )
    )
    payload = {
        "total_assets_minor": sheet.total_assets_minor,
        "total_liabilities_minor": sheet.liabilities_minor,
        "net_worth_minor": sheet.net_worth_minor,
        "liquid_assets_minor": sheet.liquid_assets_minor,
    }
    import json

    snapshot = existing or FinancialSnapshot(
        id=new_id("snapshot"),
        household_id=household_id,
        as_of=as_of,
    )
    snapshot.total_assets_minor = sheet.total_assets_minor
    snapshot.total_liabilities_minor = sheet.liabilities_minor
    snapshot.net_worth_minor = sheet.net_worth_minor
    snapshot.liquid_assets_minor = sheet.liquid_assets_minor
    snapshot.monthly_income_minor = summary.income_minor
    snapshot.monthly_expense_minor = summary.expense_minor
    snapshot.payload_json = json.dumps(payload, ensure_ascii=False)
    if existing is None:
        session.add(snapshot)
    session.flush()
    return snapshot


def _household_id(session: Session) -> str:
    from ..database.init import get_household

    return get_household(session).id
