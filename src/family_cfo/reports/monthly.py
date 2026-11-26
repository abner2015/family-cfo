"""Monthly family report."""

from __future__ import annotations

import json
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assets.service import AssetService
from ..budgets.service import BudgetService
from ..core.config import Settings
from ..core.constants import REVIEW_DUPLICATE, TxnStatus
from ..core.money import format_money
from ..database.models import Transaction, TransactionCategory
from ..finance.metrics import build_metrics, monthly_trend
from ..finance.settlement import SettlementService
from ..investments.service import InvestmentService
from ..liabilities.service import LiabilityService
from ..planning.service import PlanningService
from .render import md_table, money, percent, signed_money


def build_monthly(
    session: Session, settings: Settings, period: str, *, as_of: date | None = None
) -> dict:
    metrics = build_metrics(session, settings, period=period, as_of=as_of)
    settlement = SettlementService(session, settings).evaluate(period)
    budget_service = BudgetService(session, settings)
    budget = budget_service.execution(period)
    trend = monthly_trend(session, _hid(session), end_period=period, months=6)
    assets = AssetService(session, settings).summary()
    liabilities = LiabilityService(session, settings).summary()
    portfolio = InvestmentService(session, settings).portfolio()
    planning = PlanningService(session, settings).plan(as_of=as_of)
    review = _review_items(session, settings, period)
    categories = {}
    for cat in metrics["categories"]:
        categories.setdefault(cat["category"], {"amount_minor": 0, "count": 0})
        categories[cat["category"]]["amount_minor"] += cat["amount_minor"]
        categories[cat["category"]]["count"] += cat["count"]
    return {
        "kind": "monthly",
        "period": period,
        "generated_at": date.today().isoformat(),
        "currency": settings.currency,
        "metrics": metrics,
        "settlement": settlement.as_dict(),
        "budget": budget.as_dict() if budget else None,
        "trend": [s.as_dict() for s in trend],
        "assets": assets,
        "liabilities": liabilities,
        "portfolio": portfolio.as_dict(),
        "planning": planning,
        "review": review,
        "category_totals": categories,
        "disclaimer": "本报告由本地账本自动生成；未导入的渠道不会被当作零支出。",
    }


def _hid(session: Session) -> str:
    from ..database.init import get_household

    return get_household(session).id


def _review_items(session: Session, settings: Settings, period: str) -> dict:
    from ..core.dates import month_bounds

    start, end = month_bounds(period)
    txns = list(
        session.scalars(
            select(Transaction).where(
                Transaction.household_id == _hid(session),
                Transaction.occurred_at >= datetime.combine(start, time.min),
                Transaction.occurred_at <= datetime.combine(end, time.max.replace(microsecond=0)),
                Transaction.is_deleted.is_(False),
            )
        )
    )
    categories = {c.id: c.name for c in session.scalars(select(TransactionCategory))}
    pending = [
        {
            "id": t.id,
            "date": t.occurred_at.strftime("%Y-%m-%d"),
            "amount": format_money(t.amount_minor, t.currency),
            "counterparty": t.counterparty,
            "description": t.description,
            "reason": t.review_reason,
            "category": categories.get(t.category_id, "未分类"),
        }
        for t in txns
        if t.status == TxnStatus.PENDING_REVIEW
    ]
    duplicates = [p for p in pending if p["reason"] == REVIEW_DUPLICATE]
    return {
        "pending_count": len(pending),
        "duplicate_suspects": len(duplicates),
        "items": pending[:50],
    }


def render_monthly(report: dict) -> str:
    metrics = report["metrics"]
    currency = report["currency"]
    lines: list[str] = []
    lines.append(f"# {report['period']} 家庭财务月报")
    lines.append("")
    lines.append(f"生成日期：{report['generated_at']}　币种：{currency}")
    lines.append("")

    settlement = report["settlement"]
    lines.append("## 一、数据完整性")
    lines.append("")
    lines.append(f"- 结算状态：**{settlement['status']}**" + ("（可结算）" if settlement["ready"] else ""))
    expected = settlement["expected_sources"]
    if expected:
        lines.append(
            "- 应纳入渠道："
            + "、".join(
                f"{s['source']}" + ("（已标记缺失）" if s["status"] == "missing" else "")
                for s in expected
            )
        )
    blockers = settlement.get("blockers") or []
    if blockers:
        lines.append("- 未满足条件：")
        for blocker in blockers:
            lines.append(f"  - {blocker}")
    lines.append("")

    lines.append("## 二、核心指标")
    lines.append("")
    rows = []
    for metric in metrics["metrics"]:
        if metric["key"] in {"month_income", "month_expense", "month_net", "savings_rate"}:
            rows.append([metric["label"], metric["value"] or "—", metric["status"], metric["formula"]])
    lines.append(md_table(["指标", "数值", "数据状态", "计算口径"], rows))
    lines.append("")

    lines.append("## 三、收支结构")
    lines.append("")
    expense_rows = [
        [c["category"], format_money(c["amount_minor"], currency), c["count"]]
        for c in metrics["categories"]
        if c["direction"] == "outflow"
    ]
    income_rows = [
        [c["category"], format_money(c["amount_minor"], currency), c["count"]]
        for c in metrics["categories"]
        if c["direction"] == "inflow"
    ]
    lines.append("**支出（按分类）**")
    lines.append("")
    lines.append(md_table(["分类", "金额", "笔数"], expense_rows) if expense_rows else "本月无支出记录。")
    lines.append("")
    lines.append("**收入（按分类）**")
    lines.append("")
    lines.append(md_table(["分类", "金额", "笔数"], income_rows) if income_rows else "本月无收入记录。")
    lines.append("")

    lines.append("## 四、预算执行")
    lines.append("")
    budget = report.get("budget")
    if not budget:
        lines.append("本月未设置预算；预算执行率无法计算。")
    else:
        lines.append(
            f"预算版本 v{budget['version']}，总预算 {budget['total_budget']}，"
            f"实际 {budget['total_actual']}，执行率 {percent(budget['execution_rate'])}，"
            f"未纳入分类的支出 {budget['unbudgeted']}。"
        )
        lines.append("")
        lines.append(
            md_table(
                ["分类", "预算", "实际", "偏差", "执行率", "超支"],
                [
                    [l["category"], l["budget"], l["actual"], l["variance"], percent(l["execution_rate"]), l["over_budget"]]
                    for l in budget["lines"]
                ],
            )
        )
    lines.append("")

    lines.append("## 五、资产负债")
    lines.append("")
    bs = metrics["balance_sheet"]
    lines.append(
        md_table(
            ["项目", "金额"],
            [
                ["总资产", money(bs["total_assets_minor"], currency)],
                ["其中：账户资产", money(bs["account_assets_minor"], currency)],
                ["其中：非账户资产", money(bs["other_assets_minor"], currency)],
                ["其中：投资资产", money(bs["investment_assets_minor"], currency)],
                ["其中：自住房", money(bs["primary_residence_minor"], currency)],
                ["总负债", money(bs["total_liabilities_minor"], currency)],
                ["净资产", money(bs["net_worth_minor"], currency)],
                ["流动资产", money(bs["liquid_assets_minor"], currency)],
                ["资产负债率", percent(bs["debt_ratio"])],
                ["流动资产比例", percent(bs["liquid_ratio"])],
                ["应急资金覆盖", next((m["value"] for m in metrics["metrics"] if m["key"] == "emergency_months"), "—")],
            ],
        )
    )
    if bs["estimated_values"]:
        lines.append("")
        lines.append(
            "> 估值说明：以下资产/持仓缺少明确估值，按成本或最近估值列示："
            + "、".join(bs["estimated_values"][:10])
        )
    if bs["stale_valuations"]:
        lines.append("")
        lines.append("> 估值过期：以下项目超过一年（资产）/90 天（持仓）未更新：" + "、".join(bs["stale_valuations"][:10]))
    lines.append("")

    lines.append("## 六、投资持仓")
    lines.append("")
    portfolio = report["portfolio"]
    if not portfolio["positions"]:
        lines.append("无投资持仓记录。")
    else:
        lines.append(
            f"成本 {portfolio['cost']}，市值 {portfolio['market_value']}，已实现收益 {portfolio['realized_pnl']}，"
            f"未实现收益 {portfolio['unrealized_pnl'] or '—'}，分红 {portfolio['dividends']}。"
        )
        lines.append("")
        lines.append(
            md_table(
                ["产品", "类型", "数量", "成本", "市值", "未实现收益"],
                [
                    [p["name"], p["type"], p["quantity"], p["cost"], p["market_value"] or "—", p["unrealized"] or "—"]
                    for p in portfolio["positions"]
                ],
            )
        )
    lines.append("")

    lines.append("## 七、待确认事项")
    lines.append("")
    review = report["review"]
    if review["pending_count"] == 0:
        lines.append("本月无待确认交易。")
    else:
        lines.append(f"共 {review['pending_count']} 笔待确认，其中疑似重复 {review['duplicate_suspects']} 笔。")
        lines.append("")
        lines.append(
            md_table(
                ["日期", "金额", "对方", "说明", "原因"],
                [
                    [i["date"], i["amount"], i["counterparty"] or "—", i["description"] or "—", i["reason"] or "—"]
                    for i in review["items"][:20]
                ],
            )
        )
    lines.append("")

    lines.append("## 八、近六个月趋势")
    lines.append("")
    lines.append(
        md_table(
            ["月份", "收入", "支出", "结余", "储蓄率"],
            [
                [
                    t["period"],
                    money(t["income_minor"], currency),
                    money(t["expense_minor"], currency),
                    signed_money(t["net_minor"], currency),
                    percent(t["savings_rate"]),
                ]
                for t in report["trend"]
            ],
        )
    )
    lines.append("")
    lines.append("## 九、口径与声明")
    lines.append("")
    lines.append("1. 收入/支出仅统计已确认且纳入统计的交易；转账、信用卡还款、充值、提现、投资买卖不计入收支。")
    lines.append("2. 退款按发生月份冲减支出；跨月退款会在退款月份体现。")
    lines.append("3. 未导入的渠道不会被当作零支出，报告中会列入未满足条件。")
    lines.append("4. 资产按最近一次用户提供的估值计算，超过一年未更新的估值会标注。")
    lines.append("5. 本报告由 Python 计算生成，不包含大模型心算结果。")
    lines.append("")
    lines.append(report["disclaimer"])
    return "\n".join(lines)


def save_monthly(
    session: Session, settings: Settings, period: str, *, out_dir=None, as_of: date | None = None
) -> tuple[dict, str, object]:
    from pathlib import Path

    report = build_monthly(session, settings, period, as_of=as_of)
    markdown = render_monthly(report)
    target_dir = Path(out_dir) if out_dir else settings.reports_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    md_path = target_dir / f"{period}-monthly-report.md"
    md_path.write_text(markdown, encoding="utf-8")
    json_path = target_dir / f"{period}-monthly-report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report, markdown, md_path
