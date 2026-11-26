"""Financial health diagnosis rules and CLI output."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from typer.testing import CliRunner

from family_cfo.assets.accounts import AccountService
from family_cfo.assets.service import AssetService
from family_cfo.cli import app
from family_cfo.database.models import TransactionCategory
from family_cfo.finance.health import diagnose
from family_cfo.liabilities.service import LiabilityService
from family_cfo.transactions.service import TransactionService


def _txn(session, settings, *, when, amount, direction="outflow", txn_type="expense", category=None):
    account = AccountService(session, settings).get("银行卡")
    return TransactionService(session, settings).create(
        account_id=account.id,
        occurred_at=when,
        direction=direction,
        amount_minor=amount,
        txn_type=txn_type,
        counterparty="诊断测试",
        category_id=category,
    ).transaction


def _seed_income_and_essentials(session, settings):
    salary = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "salary"))
    housing = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "housing"))
    food = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "food"))
    for month in ("2026-08", "2026-09", "2026-10"):
        _txn(session, settings, when=f"{month}-01 09:00:00", amount=3000000,
             direction="inflow", txn_type="income", category=salary.id)
        _txn(session, settings, when=f"{month}-05 09:00:00", amount=300000, category=housing.id)
        _txn(session, settings, when=f"{month}-10 09:00:00", amount=200000, category=food.id)


def test_fresh_database_reports_insufficient_data(session, settings):
    report = diagnose(session, settings, period="2026-10")
    assert report.score is None
    assert report.grade == "数据不足"
    assert "应急资金覆盖" in report.unavailable
    assert all(f.status == "unavailable" for f in report.findings if f.key != "pending_review")


def test_healthy_household_scores_well(session, settings):
    _seed_income_and_essentials(session, settings)
    AccountService(session, settings).set_balance("银行卡", 30000000, as_of=date(2026, 10, 31))
    LiabilityService(session, settings).add(
        name="小额消费贷", liability_type="consumer_loan", principal="5000",
        balance="5000", annual_rate="4", monthly_payment="1000", remaining_months=6,
    )
    report = diagnose(session, settings, period="2026-10")
    by_key = {f.key: f for f in report.findings}
    assert by_key["emergency_months"].status == "good"
    assert by_key["savings_rate"].status == "good"
    assert by_key["debt_ratio"].status == "good"
    assert by_key["debt_service_ratio"].status == "good"
    assert by_key["liquid_ratio"].status == "good"
    assert report.score is not None and report.score >= 80
    assert report.grade in {"良好", "稳健"}


def test_high_debt_and_no_liquidity_scores_poorly(session, settings):
    salary = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "salary"))
    housing = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "housing"))
    for month in ("2026-08", "2026-09", "2026-10"):
        _txn(session, settings, when=f"{month}-01 09:00:00", amount=1000000,
             direction="inflow", txn_type="income", category=salary.id)
        _txn(session, settings, when=f"{month}-05 09:00:00", amount=900000, category=housing.id)
    AccountService(session, settings).set_balance("银行卡", 0, as_of=date(2026, 10, 31))
    AssetService(session, settings).add(
        name="自住房", asset_type="property", value="100000", is_primary_residence=True
    )
    LiabilityService(session, settings).add(
        name="大额房贷", liability_type="mortgage", principal="1000000",
        balance="1000000", annual_rate="5", monthly_payment="8000", remaining_months=200,
    )
    report = diagnose(session, settings, period="2026-10")
    by_key = {f.key: f for f in report.findings}
    assert by_key["emergency_months"].status == "risk"
    assert by_key["debt_ratio"].status == "risk"
    assert by_key["debt_service_ratio"].status == "risk"
    assert by_key["liquid_ratio"].status == "risk"
    assert report.score is not None and report.score < 60
    assert any(f.suggestion for f in report.findings)
    assert report.as_dict()["disclaimer"]


def test_pending_review_is_flagged_but_not_scored(session, settings):
    _seed_income_and_essentials(session, settings)
    AccountService(session, settings).set_balance("银行卡", 30000000, as_of=date(2026, 10, 31))
    account = AccountService(session, settings).get("银行卡")
    TransactionService(session, settings).create(
        account_id=account.id,
        occurred_at="2026-10-20 12:00:00",
        direction="outflow",
        amount_minor=12345,
        counterparty="待确认",
        status="pending_review",
        review_reason="uncategorized",
    )
    report = diagnose(session, settings, period="2026-10")
    by_key = {f.key: f for f in report.findings}
    assert by_key["pending_review"].status == "watch"
    assert report.score is not None  # pending does not change the score


def test_health_cli_outputs_json(tmp_path):
    runner = CliRunner()
    base = ["--root", str(tmp_path), "--db", str(tmp_path / "data" / "family_cfo.db")]
    assert runner.invoke(app, [*base, "init"]).exit_code == 0
    result = runner.invoke(app, [*base, "--json", "health", "--month", "2026-10"])
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["grade"] == "数据不足"
    assert payload["score"] is None
    assert payload["findings"]
