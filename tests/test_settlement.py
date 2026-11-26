"""Monthly settlement state machine, close gates and reopen audit."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from family_cfo.assets.accounts import AccountService
from family_cfo.core.errors import SettlementError, ValidationError
from family_cfo.database.models import (
    FinancialSnapshot,
    SettlementAdjustment,
)
from family_cfo.finance.settlement import SettlementService
from family_cfo.transactions.reconcile import Reconciler
from family_cfo.transactions.service import TransactionService


def _pending_txn(session, settings, amount_minor=12345):
    account = AccountService(session, settings).get("微信零钱")
    return TransactionService(session, settings).create(
        account_id=account.id,
        occurred_at="2026-10-18 12:00:00",
        direction="outflow",
        amount_minor=amount_minor,
        txn_type="expense",
        counterparty="待确认商户",
        status="pending_review",
        review_reason="uncategorized",
        source_platform="wechat",
        external_id=f"PENDING-{amount_minor}",
    ).transaction


def test_new_period_is_collecting(session, settings):
    evaluation = SettlementService(session, settings).evaluate("2026-10")
    assert evaluation.status == "COLLECTING"
    assert evaluation.ready is False
    assert any("缺少渠道" in blocker for blocker in evaluation.blockers)


def test_importing_wechat_satisfies_one_expected_source(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    evaluation = SettlementService(session, settings).evaluate("2026-10")
    sources = {s["source"]: s["status"] for s in evaluation.expected_sources}
    assert sources["wechat"] == "expected"
    assert sources["alipay"] == "expected"
    blocker = next(i for i in evaluation.items if i.key == "sources_present")
    assert blocker.passed is False
    assert "alipay" in blocker.detail and "bank" in blocker.detail


def test_mark_missing_requires_reason_and_unblocks(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    service = SettlementService(session, settings)
    with pytest.raises(ValidationError):
        service.mark_source("2026-10", "alipay", status="missing", reason=" ")
    service.mark_source("2026-10", "alipay", status="missing", reason="本月未使用支付宝")
    service.mark_source("2026-10", "bank", status="missing", reason="本月无银行流水")
    evaluation = service.evaluate("2026-10")
    sources_item = next(i for i in evaluation.items if i.key == "sources_present")
    assert sources_item.passed is True
    assert evaluation.status == "READY_TO_CLOSE"


def test_close_blocked_without_conditions(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    with pytest.raises(SettlementError) as exc:
        SettlementService(session, settings).close("2026-10")
    assert "尚不满足结算条件" in str(exc.value)


def test_close_with_override_records_audit_and_snapshot(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    service = SettlementService(session, settings)
    record = service.close("2026-10", override_reason="已人工确认缺失渠道")
    assert record.status == "CLOSED"
    assert record.closed_at is not None
    assert record.override_reason == "已人工确认缺失渠道"
    snapshot = session.scalar(
        select(FinancialSnapshot).where(FinancialSnapshot.as_of == date(2026, 10, 31))
    )
    assert snapshot is not None
    adjustment = session.scalar(
        select(SettlementAdjustment).where(SettlementAdjustment.settlement_id == record.id)
    )
    assert adjustment is not None and "人工确认" in adjustment.reason


def test_pending_policy_suspends_transactions(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    service = SettlementService(session, settings)
    service.mark_source("2026-10", "alipay", status="missing", reason="未使用")
    service.mark_source("2026-10", "bank", status="missing", reason="未使用")
    pending = _pending_txn(session, settings)
    evaluation = service.evaluate("2026-10")
    assert evaluation.ready is False
    record = service.close("2026-10", pending_policy="suspend")
    assert record.status == "CLOSED"
    assert pending.stats_included is False
    assert "暂挂" in (record.notes or "")


def test_reopen_requires_reason_and_keeps_history(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    service = SettlementService(session, settings)
    service.mark_source("2026-10", "alipay", status="missing", reason="未使用")
    service.mark_source("2026-10", "bank", status="missing", reason="未使用")
    service.close("2026-10")
    with pytest.raises(ValidationError):
        service.reopen("2026-10", reason=" ")
    reopened = service.reopen("2026-10", reason="补充一笔退款")
    assert reopened.status == "RECONCILING"
    assert reopened.reopened_count == 1
    adjustments = list(
        session.scalars(
            select(SettlementAdjustment).where(SettlementAdjustment.settlement_id == reopened.id)
        )
    )
    assert any("reopen" in a.reason for a in adjustments)


def test_cannot_close_twice(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    service = SettlementService(session, settings)
    service.mark_source("2026-10", "alipay", status="missing", reason="未使用")
    service.mark_source("2026-10", "bank", status="missing", reason="未使用")
    service.close("2026-10")
    with pytest.raises(SettlementError):
        service.close("2026-10")


def test_closed_month_with_new_pending_is_flagged(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    Reconciler(session, settings, period="2026-10").run()
    service = SettlementService(session, settings)
    service.mark_source("2026-10", "alipay", status="missing", reason="未使用")
    service.mark_source("2026-10", "bank", status="missing", reason="未使用")
    service.close("2026-10")
    _pending_txn(session, settings, amount_minor=777)
    from family_cfo.database.integrity import run_integrity_checks
    from family_cfo.database.init import get_household

    report = run_integrity_checks(session, get_household(session).id, period="2026-10")
    names = {c.name: c for c in report.checks}
    assert names["closed_period_pending"].severity == "error"
    assert names["closed_period_pending"].count == 1


def test_duplicate_suspect_blocks_close(session, settings, import_fixture):
    import_fixture("wechat_sample.csv")
    account = AccountService(session, settings).get("微信零钱")
    TransactionService(session, settings).create(
        account_id=account.id,
        occurred_at="2026-10-18 12:00:00",
        direction="outflow",
        amount_minor=39900,
        txn_type="expense",
        counterparty="京东",
        external_id="DUP-SUSPECT",
        status="pending_review",
        review_reason="possible_duplicate",
    )
    service = SettlementService(session, settings)
    service.mark_source("2026-10", "alipay", status="missing", reason="未使用")
    service.mark_source("2026-10", "bank", status="missing", reason="未使用")
    evaluation = service.evaluate("2026-10")
    duplicate_item = next(i for i in evaluation.items if i.key == "duplicates")
    assert duplicate_item.passed is False
