"""Deterministic natural-language financial Q&A.

The agent-facing Skill normally answers questions by reading CLI JSON.  This
module provides the same capability inside Python for the common questions, so
the answer never depends on an LLM doing arithmetic.

If an intent is not recognised, it returns ``unknown`` with suggestions rather
than inventing an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import TxnStatus
from ..core.dates import month_end, month_start, parse_period, period_range, previous_period, today_local
from ..core.money import format_money, pct
from ..database.models import Transaction, TransactionCategory
from ..database.init import get_household
from .metrics import (
    average_monthly_expense,
    balance_sheet,
    monthly_trend,
    net_flow_by_category,
    period_summary,
)

CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

MONTH_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*月?")
YEAR_RE = re.compile(r"(20\d{2})\s*年")
RECENT_RE = re.compile(r"(?:最近|过去)\s*([0-9一二三四五六七八九十两]+)\s*个?月")


@dataclass
class QueryAnswer:
    question: str
    intent: str
    period: str | None
    answer: str
    value: str | None = None
    unit: str | None = None
    metric: str | None = None
    source: list[str] = field(default_factory=list)
    data_status: str = "actual"
    details: list[dict] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "intent": self.intent,
            "period": self.period,
            "answer": self.answer,
            "value": self.value,
            "unit": self.unit,
            "metric": self.metric,
            "source": self.source,
            "data_status": self.data_status,
            "details": self.details,
            "suggestions": self.suggestions,
        }


def _number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text in CN_NUMBERS:
        return CN_NUMBERS[text]
    if text.startswith("十") and len(text) == 2:
        return 10 + CN_NUMBERS.get(text[1], 0)
    if len(text) == 2 and text[1] == "十":
        return CN_NUMBERS.get(text[0], 0) * 10
    return None


def _resolve_periods(question: str, settings: Settings) -> tuple[list[str], str, str | None]:
    today = today_local(settings.timezone)
    match = MONTH_RE.search(question)
    if match:
        period = parse_period(f"{match.group(1)}-{int(match.group(2)):02d}")
        return [period], period, "month"
    year_match = YEAR_RE.search(question)
    if year_match:
        year = int(year_match.group(1))
        end = f"{year}-12"
        if year == today.year:
            end = f"{year}-{today.month:02d}"
        return period_range(f"{year}-01", end), f"{year}-01 至 {end}", "year"
    if "今年" in question or "本年" in question:
        end = f"{today.year}-{today.month:02d}"
        return period_range(f"{today.year}-01", end), f"{today.year}-01 至 {end}", "year"
    if "去年" in question:
        year = today.year - 1
        return period_range(f"{year}-01", f"{year}-12"), f"{year}-01 至 {year}-12", "year"
    if "上个月" in question or "上月" in question:
        period = previous_period(f"{today.year}-{today.month:02d}")
        return [period], period, "month"
    if "这个月" in question or "本月" in question or "当月" in question:
        period = f"{today.year}-{today.month:02d}"
        return [period], period, "month"
    recent = RECENT_RE.search(question)
    if recent:
        count = _number(recent.group(1)) or 6
        periods = [previous_period(f"{today.year}-{today.month:02d}", i) for i in range(count - 1, -1, -1)]
        return periods, f"最近 {count} 个月", "trend"
    period = f"{today.year}-{today.month:02d}"
    return [period], period, "month"


def _category_in_question(session: Session, household_id: str, question: str) -> TransactionCategory | None:
    categories = list(
        session.scalars(
            select(TransactionCategory).where(
                (TransactionCategory.household_id == household_id)
                | (TransactionCategory.household_id.is_(None))
            )
        )
    )
    # Prefer a full name match; fall back to long slug fragments.
    for category in categories:
        if category.name and category.name in question:
            return category
    return None


def _aggregate(periods: list[str], session: Session, household_id: str, settings: Settings) -> dict:
    summaries = [period_summary(session, household_id, p, currency=settings.currency) for p in periods]
    income = sum(s.income_minor for s in summaries)
    expense = sum(s.expense_minor for s in summaries)
    refund = sum(s.refund_minor for s in summaries)
    count = sum(s.transaction_count for s in summaries)
    return {
        "income": income,
        "expense": expense,
        "refund": refund,
        "net": income - expense,
        "count": count,
        "savings_rate": (income - expense) / income if income else None,
    }


def answer_question(question: str, session: Session, settings: Settings) -> QueryAnswer:
    text = question.strip()
    if not text:
        return QueryAnswer(text, "unknown", None, "问题为空。", data_status="unavailable")
    household_id = get_household(session).id
    periods, label, scope = _resolve_periods(text, settings)
    currency = settings.currency
    base = QueryAnswer(
        question=text,
        intent="unknown",
        period=label,
        answer="",
        source=[],
        data_status="actual",
    )
    has_data = any(
        period_summary(session, household_id, period).transaction_count > 0 for period in periods
    ) or any(
        session.scalar(
            select(Transaction.id).where(
                Transaction.household_id == household_id,
                Transaction.occurred_at >= datetime.combine(month_start(period), time.min),
                Transaction.occurred_at
                <= datetime.combine(month_end(period), time.max.replace(microsecond=0)),
            ).limit(1)
        )
        is not None
        for period in periods
    )

    def unavailable(message: str) -> QueryAnswer:
        base.answer = message
        base.data_status = "unavailable"
        base.suggestions = _SUGGESTIONS
        return base

    # Net worth / assets / liabilities
    if any(word in text for word in ("净资产", "总资产", "总负债")):
        sheet = balance_sheet(session, household_id, as_of=today_local(settings.timezone), settings=settings)
        if not sheet.has_data and not sheet.has_liability_data:
            return unavailable("尚未录入账户余额、资产或负债，无法回答该问题。")
        if "净资产" in text:
            base.intent = "net_worth"
            base.metric = "net_worth"
            base.value = format_money(sheet.net_worth_minor, currency)
            base.answer = f"截至 {sheet.as_of.isoformat()}，家庭净资产为 {base.value}。"
            base.source = ["accounts", "assets", "liabilities", "investment_positions"]
        elif "总资产" in text:
            base.intent = "total_assets"
            base.metric = "total_assets"
            base.value = format_money(sheet.total_assets_minor, currency)
            base.answer = f"截至 {sheet.as_of.isoformat()}，家庭总资产为 {base.value}。"
            base.source = ["accounts", "assets", "investment_positions"]
        else:
            base.intent = "total_liabilities"
            base.metric = "total_liabilities"
            base.value = format_money(sheet.liabilities_minor, currency)
            base.answer = f"截至 {sheet.as_of.isoformat()}，家庭总负债为 {base.value}。"
            base.source = ["liabilities", "accounts"]
        if sheet.estimated_values:
            base.details.append({"estimated": sheet.estimated_values})
        if sheet.excluded_currencies:
            base.details.append({"excluded_currencies": sorted(sheet.excluded_currencies)})
        return base

    # Emergency fund
    if "应急" in text:
        avg, months = average_monthly_expense(session, household_id, period=periods[-1], months=3)
        sheet = balance_sheet(session, household_id, as_of=today_local(settings.timezone), settings=settings)
        if avg <= 0 or not sheet.has_data:
            return unavailable("缺少流动资产或必要支出数据，无法计算应急资金覆盖月数。")
        covered = sheet.liquid_assets_minor / avg
        base.intent = "emergency_fund"
        base.metric = "emergency_months"
        base.value = f"{covered:.1f} 个月"
        base.unit = "月"
        base.answer = (
            f"流动资产 {format_money(sheet.liquid_assets_minor, currency)}，"
            f"近 {months} 个月平均必要支出 {format_money(avg, currency)}，"
            f"可覆盖约 {covered:.1f} 个月。"
        )
        base.source = ["accounts", "assets", "transactions"]
        return base

    # Trend
    if scope == "trend" or "趋势" in text:
        count = len(periods)
        trend = monthly_trend(session, household_id, end_period=periods[-1], months=count)
        base.intent = "spending_trend"
        base.metric = "monthly_trend"
        base.answer = "最近 " + str(count) + " 个月：" + "；".join(
            f"{t.period} 收入 {format_money(t.income_minor, currency)}、"
            f"支出 {format_money(t.expense_minor, currency)}、结余 {format_money(t.net_minor, currency)}"
            for t in trend
        )
        base.details = [t.as_dict() for t in trend]
        base.source = ["transactions"]
        if not any(t.transaction_count for t in trend):
            base.data_status = "unavailable"
            base.answer = "最近 " + str(count) + " 个月没有交易记录，无法判断趋势。"
        return base

    # Budget
    if "预算" in text:
        from ..budgets.service import BudgetService

        service = BudgetService(session, settings)
        report = service.execution(periods[-1])
        if report is None:
            return unavailable(f"{periods[-1]} 未设置预算。")
        base.intent = "budget"
        base.metric = "budget_execution"
        base.value = pct(report.total_actual_minor, report.total_budget_minor)
        base.answer = (
            f"{report.period} 预算 {format_money(report.total_budget_minor, currency)}，"
            f"实际 {format_money(report.total_actual_minor, currency)}，"
            f"执行率 {base.value}。"
        )
        base.details = report.as_dict()["lines"]
        base.source = ["budgets", "transactions"]
        return base

    # Pending review
    if "待确认" in text or "异常" in text:
        start = month_start(periods[0])
        pending = list(
            session.scalars(
                select(Transaction).where(
                    Transaction.household_id == household_id,
                    Transaction.occurred_at >= datetime.combine(start, time.min),
                    Transaction.status == TxnStatus.PENDING_REVIEW,
                    Transaction.is_deleted.is_(False),
                )
            )
        )
        base.intent = "pending_review"
        base.metric = "pending_review"
        base.value = str(len(pending))
        base.unit = "笔"
        base.answer = f"{label} 有 {len(pending)} 笔待确认交易。"
        base.details = [{"id": t.id, "amount_minor": t.amount_minor, "reason": t.review_reason} for t in pending[:20]]
        base.source = ["transactions"]
        return base

    # Income / expense / savings / net
    aggregate = _aggregate(periods, session, household_id, settings)
    category = _category_in_question(session, household_id, text)
    wants_expense = any(word in text for word in ("支出", "花费", "消费", "花了"))
    wants_income = "收入" in text
    wants_savings = "储蓄率" in text
    wants_net = any(word in text for word in ("结余", "存下", "攒下")) or wants_savings

    if category is not None and (wants_expense or wants_income):
        total = 0
        count = 0
        for period in periods:
            for row in net_flow_by_category(session, household_id, period):
                if row["slug"] == category.slug and (
                    row["direction"] == "outflow" if wants_expense else row["direction"] == "inflow"
                ):
                    total += row["amount_minor"]
                    count += row["count"]
        base.intent = "category_expense" if wants_expense else "category_income"
        base.metric = f"category:{category.slug}"
        base.value = format_money(total, currency)
        base.answer = (
            f"{label} {category.name}{'支出' if wants_expense else '收入'} "
            f"{base.value}，共 {count} 笔。"
        )
        base.details = [{"category": category.name, "amount_minor": total, "count": count}]
        base.source = ["transactions", "transaction_categories"]
        if count == 0 and not has_data:
            return unavailable(f"{label} 没有 {category.name} 交易记录，且该期间无账单数据。")
        return base

    if wants_savings:
        if aggregate["income"] <= 0:
            return unavailable(f"{label} 没有收入记录，无法计算储蓄率。")
        base.intent = "savings_rate"
        base.metric = "savings_rate"
        base.value = pct(aggregate["net"], aggregate["income"])
        base.answer = (
            f"{label} 收入 {format_money(aggregate['income'], currency)}，"
            f"支出 {format_money(aggregate['expense'], currency)}，"
            f"结余 {format_money(aggregate['net'], currency)}，储蓄率 {base.value}。"
        )
        base.source = ["transactions"]
        return base

    if wants_expense or wants_income or wants_net:
        if aggregate["count"] == 0 and not has_data:
            return unavailable(f"{label} 没有交易记录，无法判断收支。")
        if wants_income:
            base.intent = "income"
            base.metric = "month_income"
            base.value = format_money(aggregate["income"], currency)
            base.answer = f"{label} 家庭收入 {base.value}。"
        elif wants_net:
            base.intent = "net"
            base.metric = "month_net"
            base.value = format_money(aggregate["net"], currency)
            base.answer = (
                f"{label} 收入 {format_money(aggregate['income'], currency)}，"
                f"支出 {format_money(aggregate['expense'], currency)}，"
                f"结余 {format_money(aggregate['net'], currency)}。"
            )
        else:
            base.intent = "expense"
            base.metric = "month_expense"
            base.value = format_money(aggregate["expense"], currency)
            base.answer = (
                f"{label} 家庭支出 {base.value}（已扣退款），共 {aggregate['count']} 笔。"
            )
        base.source = ["transactions"]
        return base

    base.answer = "暂不支持这个问题。我目前可以回答：收支、分类支出、净资产、储蓄率、应急资金、预算、趋势、待确认交易。"
    base.data_status = "unavailable"
    base.suggestions = _SUGGESTIONS
    return base


_SUGGESTIONS = [
    "2026-10 支出多少？",
    "2026-10 教育支出多少？",
    "现在家庭净资产多少？",
    "今年储蓄率多少？",
    "最近 6 个月消费趋势",
    "应急资金能覆盖几个月？",
    "本月预算执行率",
    "有多少待确认交易？",
]
