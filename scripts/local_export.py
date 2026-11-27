#!/usr/bin/env python3
"""本地导出：把 SQLite 权威账本导出为可读档案（CSV + XLSX），并做行数体检。

飞书多维表格免费版单表上限 2000 行，交易明细已接近上限，
因此本地导出是「永不丢数据」的兜底：即使飞书写不下，档案仍在。

用法：
    python3 local_export.py            # 导出 + 体检
    python3 local_export.py --check    # 只体检，不导出
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path("/opt/data/home/family-cfo-data/family_cfo.db")
OUT_DIR = Path("/opt/data/home/family-cfo-data/exports")
FEISHU_LIMIT = 2000


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def query(con: sqlite3.Connection, sql: str, args=()) -> list[sqlite3.Row]:
    return con.execute(sql, args).fetchall()


def fetch_transactions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return query(
        con,
        """
        SELECT t.id            AS txn_id,
               t.occurred_at   AS occurred_at,
               t.direction     AS direction,
               t.txn_type      AS txn_type,
               t.amount_minor  AS amount_minor,
               t.currency      AS currency,
               COALESCE(c.name, '未分类')  AS category,
               COALESCE(a.name, '')        AS account,
               COALESCE(t.counterparty, '') AS counterparty,
               COALESCE(t.description, '')  AS description,
               t.status        AS status,
               COALESCE(t.source_platform, '') AS source,
               t.stats_included AS stats_included,
               t.is_deleted    AS is_deleted
        FROM transactions t
        LEFT JOIN transaction_categories c ON c.id = t.category_id
        LEFT JOIN accounts a              ON a.id = t.account_id
        ORDER BY t.occurred_at, t.id
        """,
    )


HEADERS = [
    "日期", "方向", "类型", "金额", "币种", "分类", "账户",
    "对方", "说明", "状态", "来源", "纳入统计", "业务ID",
]


def row_view(r: sqlite3.Row) -> list:
    amt = r["amount_minor"] / 100
    return [
        r["occurred_at"][:10] if r["occurred_at"] else "",
        "收入" if r["direction"] == "inflow" else "支出",
        r["txn_type"],
        f"{amt:.2f}",
        r["currency"],
        r["category"],
        r["account"],
        r["counterparty"],
        r["description"],
        r["status"],
        r["source"],
        "是" if r["stats_included"] else "否",
        r["txn_id"],
    ]


def export_csv(rows: list[sqlite3.Row]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    path = OUT_DIR / f"交易明细_{stamp}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        for r in rows:
            w.writerow(row_view(r))
    return path


def export_xlsx(rows: list[sqlite3.Row]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    path = OUT_DIR / f"交易明细_{stamp}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "交易明细"
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)

    ws.append(HEADERS)
    for c in range(1, len(HEADERS) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for r in rows:
        ws.append(row_view(r))

    widths = [12, 8, 22, 14, 8, 14, 20, 20, 34, 12, 12, 10, 26]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        row[0].number_format = '#,##0.00;[Red]-#,##0.00'
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # 年度汇总页
    ws2 = wb.create_sheet("年度汇总")
    ws2.append(["年份", "笔数", "收入(元)", "支出(元)", "结余(元)"])
    for c in range(1, 6):
        cell = ws2.cell(row=1, column=c)
        cell.fill = header_fill
        cell.font = header_font
    years: dict[str, dict] = {}
    for r in rows:
        if r["is_deleted"] or r["txn_type"] not in ("expense", "income"):
            continue
        y = (r["occurred_at"] or "")[:4]
        d = years.setdefault(y, {"n": 0, "in": 0, "out": 0})
        d["n"] += 1
        amt = r["amount_minor"] / 100
        if r["direction"] == "inflow":
            d["in"] += amt
        else:
            d["out"] += amt
    for y in sorted(years):
        d = years[y]
        ws2.append([y, d["n"], round(d["in"], 2), round(d["out"], 2), round(d["in"] - d["out"], 2)])
    for row in ws2.iter_rows(min_row=2, min_col=3, max_col=5):
        for cell in row:
            cell.number_format = '#,##0.00;[Red]-#,##0.00'
    for i, w in enumerate([10, 10, 16, 16, 16], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    wb.save(path)
    return path


def health(con: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict:
    live = [r for r in rows if not r["is_deleted"]]
    counted = [r for r in live if r["stats_included"] and r["txn_type"] in ("expense", "income")]
    years: dict[str, int] = {}
    for r in live:
        y = (r["occurred_at"] or "")[:4]
        years[y] = years.get(y, 0) + 1
    uncategorized = sum(1 for r in live if r["category"] == "未分类")
    pending = sum(1 for r in live if r["status"] == "pending_review")
    return {
        "total_rows": len(rows),
        "live_rows": len(live),
        "counted_rows": len(counted),
        "rows_by_year": dict(sorted(years.items())),
        "uncategorized": uncategorized,
        "pending_review": pending,
        "feishu_limit": FEISHU_LIMIT,
        "feishu_usage_pct": round(len(live) / FEISHU_LIMIT * 100, 1),
    }


def main() -> int:
    check_only = "--check" in sys.argv
    con = connect()
    rows = fetch_transactions(con)
    info = health(con, rows)

    print("=== 本地账本体检 ===")
    print(f"  有效交易   {info['live_rows']} 笔")
    print(f"  计入统计   {info['counted_rows']} 笔")
    print(f"  未分类     {info['uncategorized']} 笔")
    print(f"  待确认     {info['pending_review']} 笔")
    print(f"  飞书上限   {info['feishu_limit']} 行 → 已用 {info['feishu_usage_pct']}%")
    print("  年度分布  " + "  ".join(f"{y}:{n}" for y, n in info["rows_by_year"].items()))

    if not check_only:
        csv_path = export_csv(rows)
        xlsx_path = export_xlsx(rows)
        print(f"\n  CSV  → {csv_path}")
        print(f"  XLSX → {xlsx_path}")

    # 体检报告落盘（供定时任务读取）
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hp = OUT_DIR / "health.json"
    hp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  体检 → {hp}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
