"""Annual family report."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assets.service import AssetService
from ..core.config import Settings
from ..core.constants import SettlementStatus
from ..core.dates import period_range
from ..core.money import format_money
from ..database.models import FinancialSnapshot
from ..finance.metrics import build_metrics, net_flow_by_category, period_summary
from ..finance.settlement import SettlementService
from ..investments.service import InvestmentService
from ..liabilities.service import LiabilityService
from ..planning.service import PlanningService
from .render import md_table, money, percent, signed_money


def build_annual(session: Session, settings: Settings, year: int) -> dict:
    from ..database.init import get_household

    household_id = get_household(session).id
    months = period_range(f"{year}-01", f"{year}-12")
    summaries = [period_summary(session, household_id, p, currency=settings.currency) for p in months]
    populated = [
        s for s in summaries if s.transaction_count > 0 or s.income_minor or s.expense_minor
    ]
    total_income = sum(s.income_minor for s in summaries)
    total_expense = sum(s.expense_minor for s in summaries)
    total_net = total_income - total_expense
    category_totals: dict[str, int] = {}
    for p in months:
        for row in net_flow_by_category(session, household_id, p):
            if row["direction"] == "outflow":
                category_totals[row["category"]] = category_totals.get(row["category"], 0) + row["amount_minor"]

    settlement = SettlementService(session, settings)
    closed = []
    missing = []
    for period in months:
        record = settlement.get(period)
        if record is not None and record.status == SettlementStatus.CLOSED:
            closed.append(period)
        elif period not in {s.period for s in populated}:
            missing.append(period)

    year_start = date(year, 1, 1)
    first_snapshot = session.scalar(
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.household_id == household_id,
            FinancialSnapshot.as_of >= year_start,
        )
        .order_by(FinancialSnapshot.as_of.asc())
        .limit(1)
    )
    latest_snapshot = session.scalar(
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.household_id == household_id,
            FinancialSnapshot.as_of <= date(year, 12, 31),
        )
        .order_by(FinancialSnapshot.as_of.desc())
        .limit(1)
    )
    net_worth_change = None
    if first_snapshot and latest_snapshot and first_snapshot.id != latest_snapshot.id:
        net_worth_change = latest_snapshot.net_worth_minor - first_snapshot.net_worth_minor

    metrics = build_metrics(session, settings, period=f"{year}-12")
    return {
        "kind": "annual",
        "year": year,
        "generated_at": date.today().isoformat(),
        "currency": settings.currency,
        "months": [s.as_dict() for s in summaries],
        "populated_months": [s.period for s in populated],
        "missing_months": missing,
        "closed_months": closed,
        "totals": {
            "income_minor": total_income,
            "expense_minor": total_expense,
            "net_minor": total_net,
            "income": format_money(total_income, settings.currency),
            "expense": format_money(total_expense, settings.currency),
            "net": format_money(total_net, settings.currency),
            "savings_rate": str(total_net / total_income) if total_income else None,
        },
        "category_totals": [
            {"category": k, "amount_minor": v, "amount": format_money(v, settings.currency)}
            for k, v in sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "net_worth_change_minor": net_worth_change,
        "metrics": metrics,
        "assets": AssetService(session, settings).summary(),
        "liabilities": LiabilityService(session, settings).summary(),
        "portfolio": InvestmentService(session, settings).portfolio().as_dict(),
        "planning": PlanningService(session, settings).plan(),
        "disclaimer": "年度报告基于本地账本；缺失月份与未导入渠道不会被当作零支出。",
    }


def render_annual(report: dict) -> str:
    currency = report["currency"]
    year = report["year"]
    lines = [f"# {year} 年度家庭财务报告", ""]
    lines.append(
        f"生成日期：{report['generated_at']}　有数据月份：{len(report['populated_months'])}/12　"
        f"已结算月份：{len(report['closed_months'])}/12"
    )
    lines.append("")
    lines.append("## 一、年度总览")
    lines.append("")
    totals = report["totals"]
    lines.append(
        md_table(
            ["指标", "金额"],
            [
                ["全年收入", totals["income"]],
                ["全年支出", totals["expense"]],
                ["全年结余", signed_money(totals["net_minor"], currency)],
                ["年度储蓄率", percent(totals["savings_rate"])],
                [
                    "净资产变化",
                    signed_money(report["net_worth_change_minor"], currency)
                    if report["net_worth_change_minor"] is not None
                    else "—（缺少年初/年末快照）",
                ],
            ],
        )
    )
    lines.append("")
    if report["missing_months"]:
        lines.append(
            "> 注意：以下月份没有导入任何交易，不能视为零支出："
            + "、".join(report["missing_months"])
        )
        lines.append("")

    lines.append("## 二、月度明细")
    lines.append("")
    lines.append(
        md_table(
            ["月份", "收入", "支出", "结余", "储蓄率", "笔数"],
            [
                [
                    m["period"],
                    money(m["income_minor"], currency),
                    money(m["expense_minor"], currency),
                    signed_money(m["net_minor"], currency),
                    percent(m["savings_rate"]),
                    m["transaction_count"],
                ]
                for m in report["months"]
            ],
        )
    )
    lines.append("")

    lines.append("## 三、全年支出分类")
    lines.append("")
    if report["category_totals"]:
        lines.append(
            md_table(
                ["分类", "金额"],
                [[c["category"], c["amount"]] for c in report["category_totals"]],
            )
        )
    else:
        lines.append("无支出数据。")
    lines.append("")

    lines.append("## 四、年末资产负债")
    lines.append("")
    bs = report["metrics"]["balance_sheet"]
    lines.append(
        md_table(
            ["项目", "金额"],
            [
                ["总资产", money(bs["total_assets_minor"], currency)],
                ["总负债", money(bs["total_liabilities_minor"], currency)],
                ["净资产", money(bs["net_worth_minor"], currency)],
                ["流动资产", money(bs["liquid_assets_minor"], currency)],
                ["资产负债率", percent(bs["debt_ratio"])],
                ["应急资金覆盖", next((m["value"] for m in report["metrics"]["metrics"] if m["key"] == "emergency_months"), "—")],
            ],
        )
    )
    lines.append("")

    lines.append("## 五、投资与目标")
    lines.append("")
    portfolio = report["portfolio"]
    lines.append(
        f"投资成本 {portfolio['cost']}，市值 {portfolio['market_value']}，"
        f"已实现 {portfolio['realized_pnl']}，未实现 {portfolio['unrealized_pnl'] or '—'}。"
    )
    lines.append("")
    plan = report["planning"]
    goal_rows = [
        [g["name"], g["target"], g["current"], g["progress"], g["required_monthly"] or "—", g["gap"]]
        for g in plan["goals"]
    ]
    lines.append(
        md_table(["目标", "目标金额", "当前金额", "完成率", "每月需储蓄", "缺口"], goal_rows)
        if goal_rows
        else "未设置财务目标。"
    )
    lines.append("")
    emergency = plan["emergency_fund"]
    lines.append(
        f"应急资金：流动资产 {emergency['liquid_assets']}，必要月支出 {emergency['essential_monthly_expense']}，"
        f"可覆盖 {emergency['covered_months'] or '—'} 个月。"
    )
    lines.append("")
    lines.append("## 六、口径与声明")
    lines.append("")
    lines.append("1. 年度合计为 12 个月已入账交易之和；无数据月份单独列出，不按 0 计。")
    lines.append("2. 转账、还款、充值、提现、投资买卖不计入收支；退款冲减支出。")
    lines.append("3. 投资未实现收益仅在用户提供估值时计算。")
    lines.append("4. 所有数字由 Python/Decimal 计算，可追溯到 transactions 与 assets 表。")
    lines.append("")
    lines.append(report["disclaimer"])
    return "\n".join(lines)


def save_annual(
    session: Session, settings: Settings, year: int, *, out_dir=None
) -> tuple[dict, str, object]:
    from pathlib import Path

    report = build_annual(session, settings, year)
    markdown = render_annual(report)
    target_dir = Path(out_dir) if out_dir else settings.reports_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    md_path = target_dir / f"{year}-annual-report.md"
    md_path.write_text(markdown, encoding="utf-8")
    json_path = target_dir / f"{year}-annual-report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report, markdown, md_path
