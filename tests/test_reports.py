"""Monthly / annual report rendering and Excel export."""

from __future__ import annotations


from openpyxl import load_workbook

from family_cfo.assets.service import AssetService
from family_cfo.reports.annual import build_annual, render_annual, save_annual
from family_cfo.reports.excel import export_annual, export_monthly
from family_cfo.reports.monthly import build_monthly, render_monthly, save_monthly
from family_cfo.transactions.reconcile import Reconciler


def test_monthly_report_contains_data_quality_and_formulas(db, settings, import_fixture, session):
    for name in ("wechat_sample.csv", "alipay_sample.csv", "bank_sample.csv"):
        import_fixture(name)
    Reconciler(session, settings, period="2026-10").run()

    report = build_monthly(session, settings, "2026-10")
    markdown = render_monthly(report)
    assert "# 2026-10 家庭财务月报" in markdown
    assert "数据完整性" in markdown
    assert "计算口径" in markdown
    assert "资产负债" in markdown
    assert "口径与声明" in markdown
    assert "¥" in markdown
    assert report["metrics"]["summary"]["income_minor"] == 1231250
    assert report["settlement"]["status"] in {"READY_TO_CLOSE", "REVIEW_REQUIRED"}
    assert any("退款" in line for line in markdown.splitlines())


def test_monthly_report_saves_markdown_and_json(db, settings, import_fixture, session):
    import_fixture("wechat_sample.csv")
    report, markdown, path = save_monthly(session, settings, "2026-10")
    assert path.exists()
    assert path.suffix == ".md"
    assert path.with_suffix(".json").exists()
    assert len(markdown) > 500


def test_monthly_excel_export(db, settings, import_fixture, session):
    import_fixture("wechat_sample.csv")
    report, _, _ = save_monthly(session, settings, "2026-10")
    target = settings.reports_dir / "monthly.xlsx"
    export_monthly(session, settings, report, target)
    assert target.exists() and target.stat().st_size > 5000
    wb = load_workbook(target)
    assert {"摘要", "收支明细", "分类汇总", "资产负债", "预算", "投资", "目标", "趋势", "待确认"} <= set(wb.sheetnames)
    ws = wb["摘要"]
    assert ws["A1"].value.startswith("2026-10")
    assert ws.max_row > 5


def test_annual_report_and_missing_months(db, settings, import_fixture, session):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    AssetService(session, settings).add(
        name="自住房", asset_type="property", value="1000000", is_primary_residence=True
    )
    report = build_annual(session, settings, 2026)
    markdown = render_annual(report)
    assert "# 2026 年度家庭财务报告" in markdown
    assert "缺失" in markdown or report["missing_months"]
    assert "2026-01" in report["missing_months"]
    assert report["populated_months"] == ["2026-10"]
    assert report["totals"]["income_minor"] == 0
    assert report["totals"]["expense_minor"] == 37830
    assert "不能视为零支出" in markdown


def test_annual_save_and_excel(db, settings, import_fixture, session):
    import_fixture("wechat_sample.csv")
    report, markdown, md_path = save_annual(session, settings, 2026)
    assert md_path.exists()
    assert md_path.with_suffix(".json").exists()
    target = settings.reports_dir / "annual.xlsx"
    export_annual(session, settings, report, target)
    assert target.exists()
    wb = load_workbook(target)
    assert {"年度总览", "月度明细", "支出分类"} <= set(wb.sheetnames)
