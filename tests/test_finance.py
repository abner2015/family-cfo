"""Financial analytics, budgets, assets, liabilities, investments and planning."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from family_cfo.assets.accounts import AccountService
from family_cfo.assets.service import AssetService
from family_cfo.budgets.service import BudgetService
from family_cfo.core.errors import ValidationError
from family_cfo.database.models import (
    FinancialSnapshot,
    TransactionCategory,
)
from family_cfo.finance.metrics import (
    balance_sheet,
    build_metrics,
    capture_snapshot,
    monthly_trend,
    net_flow_by_category,
    period_summary,
)
from family_cfo.investments.service import InvestmentService
from family_cfo.liabilities.service import LiabilityService
from family_cfo.planning.service import PlanningService
from family_cfo.transactions.service import TransactionService


def _txn(session, settings, **kwargs):
    account = AccountService(session, settings).get(kwargs.pop("account", "银行卡"))
    defaults = dict(
        account_id=account.id,
        occurred_at="2026-10-10 12:00:00",
        direction="outflow",
        amount_minor=10000,
        txn_type="expense",
        counterparty="测试",
        source_platform="manual",
    )
    defaults.update(kwargs)
    return TransactionService(session, settings).create(**defaults).transaction


def test_period_summary_income_expense_refund(session, settings):
    _txn(session, settings, direction="inflow", txn_type="income", amount_minor=1000000, counterparty="工资")
    _txn(session, settings, amount_minor=300000, counterparty="房租")
    _txn(session, settings, amount_minor=50000, counterparty="超市")
    _txn(session, settings, direction="inflow", txn_type="refund", amount_minor=20000, counterparty="超市", description="退款")
    summary = period_summary(session, TransactionService(session, settings).household_id, "2026-10")
    assert summary.income_minor == 1000000
    assert summary.expense_minor == 330000  # 300000 + 50000 - 20000
    assert summary.net_minor == 670000
    assert summary.savings_rate == Decimal("0.67")
    assert summary.transaction_count == 4


def test_transfers_and_loans_excluded_from_flow(session, settings):
    _txn(session, settings, direction="inflow", txn_type="income", amount_minor=100000)
    _txn(session, settings, txn_type="transfer", amount_minor=50000)
    _txn(session, settings, txn_type="credit_card_repayment", amount_minor=20000)
    _txn(session, settings, txn_type="investment_buy", amount_minor=30000)
    summary = period_summary(session, TransactionService(session, settings).household_id, "2026-10")
    assert summary.income_minor == 100000
    assert summary.expense_minor == 0
    assert summary.net_minor == 100000


def test_savings_rate_undefined_without_income(session, settings):
    _txn(session, settings, amount_minor=5000)
    summary = period_summary(session, TransactionService(session, settings).household_id, "2026-10")
    assert summary.income_minor == 0
    assert summary.savings_rate is None


def test_category_totals(session, settings):
    food = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "food"))
    _txn(session, settings, amount_minor=3000, category_id=food.id, counterparty="餐厅")
    _txn(session, settings, amount_minor=2000, category_id=food.id, counterparty="咖啡")
    rows = net_flow_by_category(session, TransactionService(session, settings).household_id, "2026-10")
    row = next(r for r in rows if r["slug"] == "food")
    assert row["amount_minor"] == 5000
    assert row["count"] == 2


def test_balance_sheet_and_net_worth(session, settings):
    accounts = AccountService(session, settings)
    accounts.set_balance("银行卡", 5000000, as_of=date(2026, 10, 31))
    accounts.set_balance("微信零钱", 200000, as_of=date(2026, 10, 31))
    accounts.set_balance("信用卡", 300000, as_of=date(2026, 10, 31))
    assets = AssetService(session, settings)
    assets.add(name="自住房", asset_type="property", value="2000000", acquisition_cost="1500000", is_primary_residence=True)
    assets.add(name="货币基金", asset_type="deposit", value="50000", is_liquid=True)
    liability = LiabilityService(session, settings)
    liability.add(name="房贷", liability_type="mortgage", principal="1000000", balance="600000")

    sheet = balance_sheet(session, accounts.household_id, as_of=date(2026, 10, 31), settings=settings)
    assert sheet.account_assets_minor == 5200000
    assert sheet.other_assets_minor == 205000000
    assert sheet.total_assets_minor == 210200000
    assert sheet.liabilities_minor == 300000 + 60000000
    assert sheet.net_worth_minor == 210200000 - 60300000
    # 自住房 is not liquid; the money-market fund is.
    assert sheet.liquid_assets_minor == 5200000 + 5000000
    assert sheet.primary_residence_minor == 200000000


def test_emergency_fund_months(session, settings):
    housing = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "housing"))
    for month in ("2026-07", "2026-08", "2026-09"):
        _txn(
            session, settings, occurred_at=f"{month}-05 12:00:00", amount_minor=300000,
            counterparty="房租", category_id=housing.id,
        )
    AccountService(session, settings).get("银行卡")
    AccountService(session, settings).set_balance("银行卡", 6000000, as_of=date(2026, 10, 31))
    metrics = build_metrics(session, settings, period="2026-10")
    emergency = next(m for m in metrics["metrics"] if m["key"] == "emergency_months")
    assert emergency["raw"] == "20.00"
    assert "近3个月平均必要支出" in emergency["formula"]


def test_budget_versioning_and_execution(session, settings):
    food = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "food"))
    education = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "education"))
    service = BudgetService(session, settings)
    first = service.set_budget(
        period="2026-10",
        items=[{"category": "food", "amount": "1000"}, {"category": "education", "amount": "1500"}],
    )
    assert first.version == 1
    _txn(session, settings, amount_minor=120000, category_id=food.id, counterparty="餐厅")
    _txn(session, settings, amount_minor=50000, category_id=education.id, counterparty="书店")
    report = service.execution("2026-10")
    assert report.total_budget_minor == 250000
    assert report.total_actual_minor == 170000
    assert report.execution_rate == Decimal("0.68")
    food_line = next(l for l in report.lines if l.category_id == food.id)
    assert food_line.over_budget is True
    assert food_line.variance_minor == -20000

    second = service.set_budget(
        period="2026-10", items=[{"category": "food", "amount": "2000"}]
    )
    assert second.version == 2
    assert second.status == "active"
    history = service.history("2026-10")
    assert [b.version for b in history] == [1, 2]
    assert history[0].status == "superseded"


def test_budget_validation(session, settings):
    with pytest.raises(ValidationError):
        BudgetService(session, settings).set_budget(period="2026-10", items=[])
    with pytest.raises(ValidationError):
        BudgetService(session, settings).set_budget(
            period="2026-10", items=[{"category": "food", "amount": "0"}]
        )
    with pytest.raises(ValidationError):
        BudgetService(session, settings).set_budget(
            period="2026-10", items=[{"category": "不存在的分类", "amount": "100"}]
        )


def test_liability_amortization_and_payment(session, settings):
    service = LiabilityService(session, settings)
    item = service.add(
        name="房贷", liability_type="mortgage", principal="1000000", balance="800000",
        annual_rate="4.9", remaining_months=240, monthly_payment="6500",
    )
    assert item.annual_rate_bps == 490
    rows = service.amortization("房贷", months=3)
    assert len(rows) == 3
    assert rows[0].interest_minor > 0
    assert rows[0].balance_minor < 80000000
    assert rows[2].balance_minor < rows[0].balance_minor

    payment = service.record_payment("房贷", paid_on=date(2026, 10, 15), principal="2000", interest="3266")
    assert payment.total_minor == 5266_00
    assert item.balance_minor == 80000000 - 200000
    assert item.remaining_months == 239
    assert len(service.payments("房贷")) == 1
    assert service.summary()["interest_paid_minor"] == 326600

    with pytest.raises(ValidationError):
        service.record_payment("房贷", paid_on=date(2026, 10, 15), principal="999999999")


def test_investment_pnl_and_cashflow_separation(session, settings):
    service = InvestmentService(session, settings)
    service.add_product(symbol="000001", name="测试基金", product_type="fund")
    service.record(
        product_ref="000001", txn_type="buy", trade_date=date(2026, 10, 1),
        quantity="100", price="1.5", fee="1.5",
    )
    service.record(
        product_ref="000001", txn_type="sell", trade_date=date(2026, 10, 10),
        quantity="40", price="2", fee="1",
    )
    service.record(
        product_ref="000001", txn_type="dividend", trade_date=date(2026, 10, 15), amount="50",
    )
    position = service.portfolio().positions[0]
    assert position.quantity == Decimal("60")
    assert position.cost_minor == 9090
    assert position.realized_minor == 1840
    assert position.market_value_minor is None
    assert position.estimated is True

    service.revalue("000001", market_value="170")
    portfolio = service.portfolio()
    assert portfolio.market_value_minor == 17000
    assert portfolio.unrealized_minor == 7910
    assert portfolio.dividends_minor == 5000
    assert portfolio.realized_minor == 1840

    with pytest.raises(ValidationError):
        service.record(
            product_ref="000001", txn_type="sell", trade_date=date(2026, 10, 20),
            quantity="1000", price="1",
        )


def test_goal_planning_required_monthly(session, settings):
    service = PlanningService(session, settings)
    goal = service.add_goal(
        name="教育金", target="200000", current="50000", goal_type="education",
        target_date=date(2030, 9, 1), monthly_saving="3000",
    )
    plan = service.plan(as_of=date(2026, 10, 31))
    row = plan["goals"][0]
    assert row["gap"] == "¥150,000.00"
    assert row["months_remaining"] > 40
    assert row["on_track"] is False
    assert any("线性储蓄" in assumption for assumption in plan["assumptions"])

    with_return = service.plan(as_of=date(2026, 10, 31), annual_return="0.03")
    assert with_return["goals"][0]["required_monthly"] is not None
    assert any("年化" in assumption for assumption in with_return["assumptions"])

    service.set_progress(goal.id, current="200000")
    assert service.get(goal.id).status == "achieved"


def test_emergency_plan_flags_missing_data(session, settings):
    plan = PlanningService(session, settings).plan(as_of=date(2026, 10, 31))
    assert plan["emergency_fund"]["status"] == "unavailable"
    assert plan["emergency_fund"]["covered_months"] is None


def test_snapshots_and_annual_net_worth_change(session, settings):
    AccountService(session, settings).set_balance("银行卡", 5000000, as_of=date(2026, 10, 31))
    session.add(
        FinancialSnapshot(
            id="snap_prev", household_id=AccountService(session, settings).household_id,
            as_of=date(2025, 10, 31), total_assets_minor=3000000,
            total_liabilities_minor=0, net_worth_minor=3000000,
            liquid_assets_minor=3000000, monthly_income_minor=0, monthly_expense_minor=0,
        )
    )
    session.flush()
    snapshot = capture_snapshot(
        session, AccountService(session, settings).household_id,
        as_of=date(2026, 10, 31), settings=settings,
    )
    assert snapshot.net_worth_minor == 5000000
    metrics = build_metrics(session, settings, period="2026-10")
    change = next(m for m in metrics["metrics"] if m["key"] == "net_worth_change")
    assert change["value"] == "¥20,000.00"


def test_metrics_have_formulas_and_sources(session, settings):
    metrics = build_metrics(session, settings, period="2026-10")
    assert len(metrics["metrics"]) >= 12
    for metric in metrics["metrics"]:
        assert metric["formula"]
        assert metric["sources"]
        assert metric["status"] in {"actual", "estimate", "assumption", "unavailable"}
    keys = {m["key"] for m in metrics["metrics"]}
    assert {"month_income", "savings_rate", "net_worth", "debt_ratio", "emergency_months"} <= keys


def test_trend_across_months(session, settings):
    _txn(session, settings, occurred_at="2026-08-05 12:00:00", amount_minor=1000)
    _txn(session, settings, occurred_at="2026-09-05 12:00:00", amount_minor=2000)
    _txn(session, settings, occurred_at="2026-10-05 12:00:00", amount_minor=3000)
    trend = monthly_trend(session, TransactionService(session, settings).household_id, end_period="2026-10", months=3)
    assert [t.period for t in trend] == ["2026-08", "2026-09", "2026-10"]
    assert [t.expense_minor for t in trend] == [1000, 2000, 3000]


def test_stale_asset_valuation_is_flagged(session, settings):
    service = AssetService(session, settings)
    asset = service.add(name="老房子", asset_type="property", value="1000000", valued_on=date(2024, 1, 1))
    summary = service.summary()
    assert asset.id in {line.id for line in service.list()}
    assert summary["stale"] == ["老房子"]
    metrics = build_metrics(session, settings, period="2026-10")
    assert "老房子" in metrics["data_quality"]["stale_valuations"]


def test_foreign_currency_totals_use_base_amount(session, settings):
    _txn(
        session, settings, currency="USD", amount_minor=10000, fx_rate="7.2",
        counterparty="海外订阅", txn_type="expense",
    )
    _txn(
        session, settings, currency="USD", amount_minor=5000,
        counterparty="未提供汇率", txn_type="expense", occurred_at="2026-10-11 12:00:00",
    )
    summary = period_summary(
        session, TransactionService(session, settings).household_id, "2026-10"
    )
    # Only the converted transaction is included; the other is pending with
    # stats_included=false and must not be silently treated as CNY.
    assert summary.expense_minor == 72000
    assert summary.currency == "CNY"


def test_asset_ownership_share_reduces_household_value(session, settings):
    service = AssetService(session, settings)
    service.add(
        name="共同房产", asset_type="property", value="1000000",
        ownership_share="0.5", is_primary_residence=True,
    )
    summary = service.summary()
    assert summary["total_minor"] == 50000000
    assert summary["assets"][0]["owned_value_minor"] == 50000000
    assert summary["assets"][0]["ownership_share"] == "0.5"
