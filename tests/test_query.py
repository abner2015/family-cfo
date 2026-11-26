"""Deterministic natural-language financial Q&A."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from typer.testing import CliRunner

from family_cfo.assets.accounts import AccountService
from family_cfo.cli import app
from family_cfo.database.models import TransactionCategory
from family_cfo.finance.query import answer_question
from family_cfo.transactions.service import TransactionService


def _txn(session, settings, *, when, amount, direction="outflow", txn_type="expense", category=None):
    account = AccountService(session, settings).get("银行卡")
    return TransactionService(session, settings).create(
        account_id=account.id,
        occurred_at=when,
        direction=direction,
        amount_minor=amount,
        txn_type=txn_type,
        counterparty="问答测试",
        category_id=category,
    ).transaction


def _seed(session, settings):
    salary = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "salary"))
    food = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "food"))
    education = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "education"))
    _txn(session, settings, when="2026-10-01 09:00:00", amount=3000000,
         direction="inflow", txn_type="income", category=salary.id)
    _txn(session, settings, when="2026-10-05 09:00:00", amount=200000, category=food.id)
    _txn(session, settings, when="2026-10-06 09:00:00", amount=150000, category=education.id)
    _txn(session, settings, when="2026-09-05 09:00:00", amount=100000, category=food.id)
    _txn(session, settings, when="2026-11-05 09:00:00", amount=120000, category=food.id)
    AccountService(session, settings).set_balance("银行卡", 10000000, as_of=date(2026, 11, 26))


def test_expense_question(session, settings):
    _seed(session, settings)
    answer = answer_question("2026-10 支出多少？", session, settings)
    assert answer.intent == "expense"
    assert answer.value == "¥3,500.00"
    assert answer.data_status == "actual"
    assert "3,500.00" in answer.answer


def test_category_expense_question(session, settings):
    _seed(session, settings)
    answer = answer_question("2026-10 教育支出多少？", session, settings)
    assert answer.intent == "category_expense"
    assert answer.value == "¥1,500.00"
    assert answer.details[0]["count"] == 1


def test_income_and_savings_rate(session, settings):
    _seed(session, settings)
    income = answer_question("2026-10 收入多少", session, settings)
    assert income.value == "¥30,000.00"
    savings = answer_question("2026-10 储蓄率", session, settings)
    assert savings.intent == "savings_rate"
    assert savings.value == "88.3%"


def test_net_worth_question(session, settings):
    _seed(session, settings)
    answer = answer_question("现在家庭净资产是多少？", session, settings)
    assert answer.intent == "net_worth"
    assert answer.value == "¥100,000.00"
    assert "净资产" in answer.answer


def test_recent_months_trend(session, settings):
    _seed(session, settings)
    answer = answer_question("最近 3 个月消费趋势", session, settings)
    assert answer.intent == "spending_trend"
    assert len(answer.details) == 3
    assert [d["period"] for d in answer.details] == ["2026-09", "2026-10", "2026-11"]


def test_emergency_fund_question(session, settings):
    _seed(session, settings)
    answer = answer_question("应急资金能覆盖几个月？", session, settings)
    assert answer.intent == "emergency_fund"
    assert answer.unit == "月"
    assert answer.data_status == "actual"


def test_pending_review_question(session, settings):
    _seed(session, settings)
    account = AccountService(session, settings).get("银行卡")
    TransactionService(session, settings).create(
        account_id=account.id,
        occurred_at="2026-10-20",
        direction="outflow",
        amount_minor=1000,
        counterparty="待确认",
        status="pending_review",
        review_reason="uncategorized",
    )
    answer = answer_question("2026-10 有多少待确认交易", session, settings)
    assert answer.intent == "pending_review"
    assert answer.value == "1"


def test_unknown_question_does_not_invent(session, settings):
    _seed(session, settings)
    answer = answer_question("帮我预测明年股票收益", session, settings)
    assert answer.intent == "unknown"
    assert answer.data_status == "unavailable"
    assert answer.suggestions


def test_empty_data_is_unavailable(session, settings):
    answer = answer_question("2026-10 支出多少", session, settings)
    assert answer.data_status == "unavailable"
    assert "无法" in answer.answer


def test_ask_cli_json(tmp_path):
    runner = CliRunner()
    base = ["--root", str(tmp_path), "--db", str(tmp_path / "data" / "family_cfo.db")]
    assert runner.invoke(app, [*base, "init"]).exit_code == 0
    result = runner.invoke(app, [*base, "--json", "ask", "2026-10 支出多少"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["intent"] in {"expense", "unknown"}
    assert "answer" in payload


def test_health_question(session, settings):
    _seed(session, settings)
    AccountService(session, settings).set_balance("银行卡", 10000000, as_of=date(2026, 11, 26))
    answer = answer_question("我家财务健康吗", session, settings)
    assert answer.intent == "health"
    assert answer.value is not None
    assert "健康" in answer.answer
    assert answer.details
