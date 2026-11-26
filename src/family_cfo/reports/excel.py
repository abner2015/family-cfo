"""Excel export (presentation only — the ledger stays in SQLite)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)
TITLE_FONT = Font(size=14, bold=True)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MONEY_FORMAT = '#,##0.00;[Red]-#,##0.00'
PERCENT_FORMAT = "0.0%"


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, int):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _style_header(ws, row: int, columns: int) -> None:
    for col in range(1, columns + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER


def _write_table(ws, start_row: int, headers: list[str], rows: list[list], *, money_cols=(), percent_cols=()) -> int:
    for index, header in enumerate(headers, start=1):
        ws.cell(row=start_row, column=index, value=header)
    _style_header(ws, start_row, len(headers))
    for r_offset, row in enumerate(rows, start=1):
        for c_index, value in enumerate(row, start=1):
            cell = ws.cell(row=start_row + r_offset, column=c_index, value=value)
            cell.border = BORDER
            if c_index in money_cols:
                cell.number_format = MONEY_FORMAT
            if c_index in percent_cols:
                cell.number_format = PERCENT_FORMAT
    for col in range(1, len(headers) + 1):
        max_len = len(str(headers[col - 1]))
        for row in range(start_row + 1, start_row + len(rows) + 1):
            value = ws.cell(row=row, column=col).value
            if value is not None:
                max_len = max(max_len, len(str(value)))
        ws.column_dimensions[get_column_letter(col)].width = min(max(max_len + 2, 10), 40)
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    return start_row + len(rows)


def _sheet_title(ws, title: str, subtitle: str) -> int:
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = subtitle
    ws["A2"].font = Font(color="666666")
    return 3


def export_monthly(session, settings, report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "摘要"
    row = _sheet_title(ws, f"{report['period']} 家庭财务月报", f"生成日期 {report['generated_at']}")
    metrics = report["metrics"]["metrics"]
    rows = [
        [
            m["label"],
            _num(Decimal(m["raw"]) / 100)
            if m["raw"] not in (None, "") and m["unit"] == "CNY"
            else m["value"],
            m["status"],
            m["formula"],
        ]
        for m in metrics
    ]
    row = _write_table(ws, row, ["指标", "数值", "数据状态", "计算口径"], rows, money_cols=(2,))
    row += 2
    ws.cell(row=row, column=1, value="结算状态").font = Font(bold=True)
    ws.cell(row=row, column=2, value=report["settlement"]["status"])
    row += 1
    ws.cell(row=row, column=1, value="未满足条件").font = Font(bold=True)
    ws.cell(row=row, column=2, value="；".join(report["settlement"].get("blockers") or []) or "无")

    # Transactions
    ws2 = wb.create_sheet("收支明细")
    from ..core.dates import month_bounds
    from ..database.init import get_household
    from ..database.models import Account, Transaction, TransactionCategory
    from sqlalchemy import select

    household_id = get_household(session).id
    start, end = month_bounds(report["period"])
    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    categories = {c.id: c.name for c in session.scalars(select(TransactionCategory))}
    txns = list(
        session.scalars(
            select(Transaction)
            .where(
                Transaction.household_id == household_id,
                Transaction.occurred_at >= datetime.combine(start, datetime.min.time()),
                Transaction.occurred_at <= datetime.combine(end, datetime.max.time().replace(microsecond=0)),
                Transaction.is_deleted.is_(False),
            )
            .order_by(Transaction.occurred_at.asc())
        )
    )
    _sheet_title(ws2, f"{report['period']} 交易明细", f"共 {len(txns)} 笔（含转账/不计收支）")
    _write_table(
        ws2,
        3,
        ["日期", "方向", "类型", "金额", "币种", "分类", "账户", "对方", "说明", "状态", "来源"],
        [
            [
                t.occurred_at.strftime("%Y-%m-%d %H:%M"),
                "收入" if t.direction == "inflow" else "支出",
                t.txn_type,
                _num(Decimal(t.amount_minor) / 100),
                t.currency,
                categories.get(t.category_id, "未分类"),
                accounts.get(t.account_id, ""),
                t.counterparty or "",
                t.description or "",
                t.status,
                t.source_platform,
            ]
            for t in txns
        ],
        money_cols=(4,),
    )

    # Category summary
    ws3 = wb.create_sheet("分类汇总")
    _sheet_title(ws3, "分类汇总", "仅统计收入/支出/退款/手续费")
    _write_table(
        ws3,
        3,
        ["分类", "方向", "金额", "笔数"],
        [
            [
                c["category"],
                "收入" if c["direction"] == "inflow" else "支出",
                _num(Decimal(c["amount_minor"]) / 100),
                c["count"],
            ]
            for c in report["metrics"]["categories"]
        ],
        money_cols=(3,),
    )

    # Balance sheet
    ws4 = wb.create_sheet("资产负债")
    bs = report["metrics"]["balance_sheet"]
    _sheet_title(ws4, "资产负债", f"截至 {bs['as_of']}")
    _write_table(
        ws4,
        3,
        ["项目", "金额"],
        [
            ["账户资产", _num(Decimal(bs["account_assets"]))],
            ["非账户资产", _num(Decimal(bs["other_assets"]))],
            ["投资资产", _num(Decimal(bs["investment_assets"]))],
            ["总资产", _num(Decimal(bs["total_assets"]))],
            ["总负债", _num(Decimal(bs["liabilities"]))],
            ["净资产", _num(Decimal(bs["net_worth"]))],
            ["流动资产", _num(Decimal(bs["liquid_assets"]))],
            ["自住房", _num(Decimal(bs["primary_residence"]))],
        ],
        money_cols=(2,),
    )

    # Budget
    ws5 = wb.create_sheet("预算")
    budget = report.get("budget")
    _sheet_title(ws5, "预算执行", report["period"])
    if budget:
        _write_table(
            ws5,
            3,
            ["分类", "预算", "实际", "偏差", "执行率", "超支"],
            [
                [
                    l["category"],
                    _num(Decimal(l["budget_minor"]) / 100),
                    _num(Decimal(l["actual_minor"]) / 100),
                    _num(Decimal(l["variance_minor"]) / 100),
                    _num(Decimal(l["execution_rate"])) if l["execution_rate"] else None,
                    l["over_budget"],
                ]
                for l in budget["lines"]
            ],
            money_cols=(2, 3, 4),
            percent_cols=(5,),
        )
    else:
        ws5.cell(row=3, column=1, value="本月未设置预算")

    # Portfolio
    ws6 = wb.create_sheet("投资")
    portfolio = report["portfolio"]
    _sheet_title(ws6, "投资持仓", f"成本 {portfolio['cost']} / 市值 {portfolio['market_value']}")
    _write_table(
        ws6,
        3,
        ["产品", "类型", "数量", "成本", "市值", "未实现收益", "已实现收益"],
        [
            [
                p["name"],
                p["type"],
                _num(Decimal(p["quantity"])) if p["quantity"] else None,
                _num(Decimal(p["cost_minor"]) / 100),
                _num(Decimal(p["market_value_minor"]) / 100)
                if p["market_value_minor"] is not None
                else None,
                _num(Decimal(p["unrealized_minor"]) / 100)
                if p["unrealized_minor"] is not None
                else None,
                _num(Decimal(p["realized_minor"]) / 100),
            ]
            for p in portfolio["positions"]
        ],
        money_cols=(4, 5, 6, 7),
    )

    # Goals
    ws7 = wb.create_sheet("目标")
    _sheet_title(ws7, "财务目标", "规划结果含明确假设")
    _write_table(
        ws7,
        3,
        ["目标", "目标金额", "当前金额", "完成率", "缺口", "每月需储蓄", "风险"],
        [
            [
                g["name"],
                _num(Decimal(g["target_minor"]) / 100),
                _num(Decimal(g["current_minor"]) / 100),
                _num(Decimal(g["progress"].rstrip("%"))) / 100 if g["progress"] else None,
                _num(Decimal(g["gap_minor"]) / 100),
                g["required_monthly"] or "",
                g["risk_tolerance"],
            ]
            for g in report["planning"]["goals"]
        ],
        percent_cols=(4,),
    )

    # Trend
    ws8 = wb.create_sheet("趋势")
    _sheet_title(ws8, "近六个月趋势", "")
    _write_table(
        ws8,
        3,
        ["月份", "收入", "支出", "结余", "储蓄率"],
        [
            [
                t["period"],
                _num(Decimal(t["income"])),
                _num(Decimal(t["expense"])),
                _num(Decimal(t["net_minor"]) / 100),
                _num(Decimal(t["savings_rate"])) if t["savings_rate"] else None,
            ]
            for t in report["trend"]
        ],
        money_cols=(2, 3, 4),
        percent_cols=(5,),
    )

    # Review
    ws9 = wb.create_sheet("待确认")
    _sheet_title(ws9, "待确认交易", f"{report['review']['pending_count']} 笔")
    _write_table(
        ws9,
        3,
        ["日期", "金额", "对方", "说明", "原因"],
        [
            [i["date"], i["amount"], i["counterparty"], i["description"], i["reason"]]
            for i in report["review"]["items"]
        ],
    )
    wb.save(path)
    return path


def export_annual(session, settings, report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "年度总览"
    _sheet_title(ws, f"{report['year']} 年度家庭财务报告", report["generated_at"])
    totals = report["totals"]
    _write_table(
        ws,
        3,
        ["指标", "金额"],
        [
            ["全年收入", _num(Decimal(totals["income_minor"]) / 100)],
            ["全年支出", _num(Decimal(totals["expense_minor"]) / 100)],
            ["全年结余", _num(Decimal(totals["net_minor"]) / 100)],
            ["净资产变化", _num(Decimal(report["net_worth_change_minor"]) / 100) if report["net_worth_change_minor"] is not None else ""],
        ],
        money_cols=(2,),
    )
    ws2 = wb.create_sheet("月度明细")
    _write_table(
        ws2,
        1,
        ["月份", "收入", "支出", "结余", "储蓄率", "笔数"],
        [
            [
                m["period"],
                _num(Decimal(m["income_minor"]) / 100),
                _num(Decimal(m["expense_minor"]) / 100),
                _num(Decimal(m["net_minor"]) / 100),
                _num(Decimal(m["savings_rate"])) if m["savings_rate"] else None,
                m["transaction_count"],
            ]
            for m in report["months"]
        ],
        money_cols=(2, 3, 4),
        percent_cols=(5,),
    )
    ws3 = wb.create_sheet("支出分类")
    _write_table(
        ws3,
        1,
        ["分类", "金额"],
        [[c["category"], _num(Decimal(c["amount_minor"]) / 100)] for c in report["category_totals"]],
        money_cols=(2,),
    )
    wb.save(path)
    return path
