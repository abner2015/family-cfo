"""Bill adapters, format detection, dedupe-by-file and mapping fallback."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from family_cfo.core.errors import MappingRequiredError
from family_cfo.database.models import ImportBatch, RawTransaction, Transaction, TransactionCategory
from family_cfo.importers.columns import suggest_mapping
from family_cfo.importers.readers import read_any_table
from family_cfo.importers.service import ImportService

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_detects_wechat_format(importer: ImportService):
    table = read_any_table(FIXTURES / "wechat_sample.csv")
    adapter, score = importer.detect(table)
    assert adapter.name == "wechat"
    assert score >= 0.9


def test_detects_alipay_format(importer: ImportService):
    table = read_any_table(FIXTURES / "alipay_sample.csv")
    adapter, _ = importer.detect(table)
    assert adapter.name == "alipay"


def test_detects_bank_format(importer: ImportService):
    table = read_any_table(FIXTURES / "bank_sample.csv")
    adapter, score = importer.detect(table)
    assert adapter.name == "bank"
    assert score >= 0.5


def test_unknown_format_requires_mapping(importer: ImportService):
    with pytest.raises(MappingRequiredError) as exc:
        importer.import_file(FIXTURES / "generic_unknown.csv")
    suggestion = exc.value.suggestion
    assert suggestion["ready"] is True
    assert suggestion["mapping"]["date"] == "日期"
    assert suggestion["mapping"]["amount"] == "金额"


def test_column_mapping_suggestion_lists_headers():
    suggestion = suggest_mapping(["日期", "说明", "金额", "方向"])
    assert suggestion["mapping"]["date"] == "日期"
    assert suggestion["mapping"]["direction"] == "方向"
    assert suggestion["missing"] == []


def test_import_wechat_bill(importer: ImportService, session):
    report = importer.import_file(FIXTURES / "wechat_sample.csv")
    assert report.status == "imported"
    assert report.imported == 11
    assert report.failed == 0
    assert report.period == "2026-10"
    txns = list(session.scalars(select(Transaction).where(Transaction.source_platform == "wechat")))
    assert len(txns) == 11
    topup = next(t for t in txns if t.external_id == "WX011")
    assert topup.direction == "inflow"
    assert topup.txn_type == "topup"
    refund = next(t for t in txns if t.external_id == "WX007")
    assert refund.txn_type == "refund"
    original = next(t for t in txns if t.external_id == "WX006")
    assert original.txn_type == "expense"
    assert original.amount_minor == 39900
    assert all(isinstance(t.amount_minor, int) for t in txns)
    raw_count = session.execute(select(RawTransaction)).all()
    assert len(raw_count) == 11


def test_import_alipay_bill(importer: ImportService, session):
    report = importer.import_file(FIXTURES / "alipay_sample.csv")
    assert report.imported == 10
    txns = list(session.scalars(select(Transaction).where(Transaction.source_platform == "alipay")))
    assert len(txns) == 10
    balance_transfer = next(t for t in txns if t.external_id == "AL004")
    assert balance_transfer.txn_type in {"topup", "investment_buy"}
    assert balance_transfer.direction == "outflow"
    assert balance_transfer.stats_included is False  # non-flow type


def test_import_bank_statement(importer: ImportService, session):
    report = importer.import_file(FIXTURES / "bank_sample.csv")
    assert report.imported == 5
    txns = list(session.scalars(select(Transaction).where(Transaction.source_platform == "bank")))
    salary = next(t for t in txns if t.external_id == "BK001")
    assert salary.direction == "inflow"
    assert salary.amount_minor == 1200000
    mortgage = next(t for t in txns if t.external_id == "BK004")
    assert mortgage.txn_type == "loan_repay"


def test_import_xlsx(importer: ImportService, tmp_path: Path, session):
    import csv

    rows = list(csv.reader((FIXTURES / "wechat_sample.csv").read_text(encoding="utf-8").splitlines()))
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    xlsx = tmp_path / "wechat_sample.xlsx"
    wb.save(xlsx)

    report = importer.import_file(xlsx)
    assert report.status == "imported"
    assert report.imported == 11
    assert report.meta["sheet"] is not None


def test_xlsx_detection_by_platform_override(importer: ImportService, tmp_path: Path):
    import csv

    rows = list(csv.reader((FIXTURES / "wechat_sample.csv").read_text(encoding="utf-8").splitlines()))
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    xlsx = tmp_path / "bill.xlsx"
    wb.save(xlsx)
    report = importer.import_file(xlsx, platform="wechat")
    assert report.imported == 11


def test_duplicate_file_detected_by_hash(importer: ImportService):
    first = importer.import_file(FIXTURES / "wechat_sample.csv")
    second = importer.import_file(FIXTURES / "wechat_sample.csv")
    assert first.status == "imported"
    assert second.status == "duplicate"
    assert second.batch_id == first.batch_id


def test_same_transactions_in_new_file_are_deduped(importer: ImportService, tmp_path: Path, session):
    importer.import_file(FIXTURES / "wechat_sample.csv")
    copy = tmp_path / "wechat_copy.csv"
    content = (FIXTURES / "wechat_sample.csv").read_text(encoding="utf-8")
    copy.write_text(content + "\n# reshuffled export\n", encoding="utf-8")
    report = importer.import_file(copy)
    assert report.status == "imported"
    assert report.imported == 0
    assert report.duplicates == 11
    assert len(list(session.scalars(select(Transaction)))) == 11


def test_gbk_alipay_export(importer: ImportService, tmp_path: Path):
    text = (FIXTURES / "alipay_sample.csv").read_text(encoding="utf-8")
    gbk_file = tmp_path / "alipay_gbk.csv"
    gbk_file.write_bytes(text.encode("gb18030"))
    report = importer.import_file(gbk_file)
    assert report.imported == 10


def test_mapping_import_for_unknown_platform(importer: ImportService, session):
    mapping = {
        "date": "日期",
        "amount": "金额",
        "direction": "方向",
        "counterparty": "对方",
        "description": "说明",
        "external_id": "订单号",
    }
    report = importer.import_file(
        FIXTURES / "generic_unknown.csv", mapping=mapping, platform="other"
    )
    assert report.imported == 3
    assert report.platform == "other"
    txns = list(session.scalars(select(Transaction).where(Transaction.source_platform == "other")))
    assert len(txns) == 3
    assert txns[0].external_id == "OTH001"


def test_row_errors_are_isolated_and_reported(importer: ImportService, settings):
    bad = settings.data_dir / "bad_wechat.csv"
    content = "\n".join(
        [
            "微信支付账单明细",
            "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注",
            "2026-10-01 10:00:00,商户消费,美团,外卖,支出,¥35.00,零钱,支付成功,BAD001,,",
            "2026-10-02 10:00:00,商户消费,美团,外卖,支出,金额错误,零钱,支付成功,BAD002,,",
            "2026-10-03 10:00:00,商户消费,盒马,日用品,支出,¥20.00,零钱,支付成功,BAD003,,",
        ]
    )
    bad.write_text(content, encoding="utf-8")
    report = importer.import_file(bad)
    assert report.imported == 2
    assert report.failed == 1
    assert report.errors[0].row_number == 4
    assert report.meta["error_report"]
    assert Path(report.meta["error_report"]).exists()


def test_archive_is_read_only_and_source_moved_from_inbox(importer: ImportService, settings):
    settings.ensure_dirs()
    inbox_file = settings.inbox_dir / "wechat_inbox.csv"
    shutil.copy2(FIXTURES / "wechat_sample.csv", inbox_file)
    report = importer.import_file(inbox_file)
    assert report.status == "imported"
    archive = Path(report.archive_path)
    assert archive.exists()
    assert archive.stat().st_mode & 0o777 == 0o444
    assert not inbox_file.exists()
    assert (settings.processed_dir / inbox_file.name).exists()


def test_duplicate_batch_keeps_single_batch_record(importer: ImportService, session):
    importer.import_file(FIXTURES / "wechat_sample.csv")
    importer.import_file(FIXTURES / "wechat_sample.csv")
    assert len(list(session.scalars(select(ImportBatch)))) == 1


def test_imported_rows_get_categories_or_review(importer: ImportService, session):
    importer.import_file(FIXTURES / "wechat_sample.csv")
    txns = list(session.scalars(select(Transaction)))
    categorized = [t for t in txns if t.category_id]
    review = [t for t in txns if t.status == "pending_review"]
    assert categorized
    assert categorized[0].category_id in {c.id for c in session.scalars(select(TransactionCategory))}
    # No transaction is silently given a category that does not exist.
    assert all(t.category_id is None or t.status in {"confirmed", "pending_review"} for t in txns)
    assert isinstance(review, list)


def test_scan_custom_directory(importer: ImportService, settings, tmp_path):
    scan_dir = tmp_path / "bills"
    scan_dir.mkdir()
    shutil.copy2(FIXTURES / "wechat_sample.csv", scan_dir / "wechat_sample.csv")
    reports = importer.scan_inbox(directory=scan_dir)
    assert len(reports) == 1
    assert reports[0].imported == 11


def test_reversal_transaction_is_flagged_and_excluded(importer: ImportService, settings, session):
    from family_cfo.finance.metrics import period_summary

    content = "\n".join(
        [
            "微信支付账单明细",
            "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注",
            "2026-10-05 10:00:00,商户消费,撤销商户,测试,支出,¥100.00,零钱,已撤销,WXREV1,,",
            "2026-10-06 10:00:00,商户消费,正常商户,测试,支出,¥20.00,零钱,支付成功,WXOK1,,",
        ]
    )
    path = settings.data_dir / "reversal.csv"
    path.write_text(content, encoding="utf-8")
    report = importer.import_file(path)
    assert report.imported == 2
    txns = {t.external_id: t for t in session.scalars(select(Transaction))}
    reversal = txns["WXREV1"]
    assert reversal.txn_type == "reversal"
    assert reversal.status == "pending_review"
    assert reversal.stats_included is False
    assert reversal.review_reason == "reversal_unlinked"
    summary = period_summary(
        session, session.scalar(select(Transaction)).household_id, "2026-10"
    )
    assert summary.expense_minor == 2000


def test_concurrent_duplicate_insert_is_reported_as_duplicate(importer: ImportService):
    first = importer.import_file(FIXTURES / "wechat_sample.csv")
    assert first.status == "imported"
    # Simulate the race where the hash check happens before another process
    # commits the same file: the unique constraint must win without a crash.
    original = importer._find_batch
    calls = {"count": 0}

    def flaky(file_hash):
        calls["count"] += 1
        return None if calls["count"] == 1 else original(file_hash)

    importer._find_batch = flaky
    second = importer.import_file(FIXTURES / "wechat_sample.csv")
    assert second.status == "duplicate"
    assert second.batch_id == first.batch_id


def test_alipay_success_refund_column_is_resolved(importer: ImportService, settings, session):
    content = "\n".join(
        [
            "支付宝交易记录明细查询",
            "交易时间,交易分类,交易对方,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,成功退款（元）",
            "2026-10-01 10:00:00,购物,淘宝,商品,支出,100.00,余额,交易成功,ALR1,100.00",
            "2026-10-03 10:00:00,退款,淘宝,退款,收入,100.00,余额,退款成功,ALR2,0.00",
        ]
    )
    path = settings.data_dir / "alipay_refund.csv"
    path.write_text(content, encoding="utf-8")
    report = importer.import_file(path)
    assert report.imported == 2
    original = session.scalar(select(Transaction).where(Transaction.external_id == "ALR1"))
    assert original.status == "pending_review"
    from family_cfo.transactions.reconcile import Reconciler

    Reconciler(session, settings, period="2026-10").run()
    assert original.status == "confirmed"
    assert original.review_reason is None
    refund = session.scalar(select(Transaction).where(Transaction.external_id == "ALR2"))
    assert refund.refund_of_id == original.id


def test_alipay_yuebao_transfer_direction(importer: ImportService, settings, session):
    content = "\n".join(
        [
            "支付宝交易记录明细查询",
            "交易时间,交易分类,交易对方,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号",
            "2026-10-01 10:00:00,充值,余额宝,余额宝-转出,不计收支,500.00,余额,交易成功,ALY1",
        ]
    )
    path = settings.data_dir / "alipay_yuebao.csv"
    path.write_text(content, encoding="utf-8")
    importer.import_file(path)
    txn = session.scalar(select(Transaction).where(Transaction.external_id == "ALY1"))
    assert txn.txn_type == "investment_sell"
    assert txn.direction == "inflow"
    assert txn.stats_included is False


def test_duplicate_file_in_inbox_moves_to_processed(importer: ImportService, settings, session):
    settings.ensure_dirs()
    first = settings.inbox_dir / "first.csv"
    shutil.copy2(FIXTURES / "wechat_sample.csv", first)
    importer.import_file(first)
    again = settings.inbox_dir / "again.csv"
    shutil.copy2(FIXTURES / "wechat_sample.csv", again)
    report = importer.import_file(again)
    assert report.status == "duplicate"
    assert not again.exists()
    assert (settings.processed_dir / "again.csv").exists()


def test_import_legacy_xls(importer: ImportService, tmp_path: Path):
    import csv

    import xlwt

    rows = list(csv.reader((FIXTURES / "wechat_sample.csv").read_text(encoding="utf-8").splitlines()))
    book = xlwt.Workbook()
    sheet = book.add_sheet("Sheet1")
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    path = tmp_path / "wechat_sample.xls"
    book.save(str(path))
    report = importer.import_file(path)
    assert report.status == "imported"
    assert report.imported == 11


def test_overlapping_exports_are_incremental(importer: ImportService, settings, session):
    header = "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注"
    first = settings.data_dir / "overlap1.csv"
    first.write_text(
        "\n".join(
            [
                "微信支付账单明细",
                header,
                "2026-10-01 10:00:00,商户消费,美团,外卖,支出,¥10.00,零钱,支付成功,OV1,,",
                "2026-10-02 10:00:00,商户消费,美团,外卖,支出,¥20.00,零钱,支付成功,OV2,,",
            ]
        ),
        encoding="utf-8",
    )
    second = settings.data_dir / "overlap2.csv"
    second.write_text(
        "\n".join(
            [
                "微信支付账单明细",
                header,
                "2026-10-02 10:00:00,商户消费,美团,外卖,支出,¥20.00,零钱,支付成功,OV2,,",
                "2026-10-03 10:00:00,商户消费,美团,外卖,支出,¥30.00,零钱,支付成功,OV3,,",
            ]
        ),
        encoding="utf-8",
    )
    one = importer.import_file(first)
    two = importer.import_file(second)
    assert one.imported == 2
    assert two.imported == 1
    assert two.duplicates == 1
    assert len(list(session.scalars(select(Transaction)))) == 3
