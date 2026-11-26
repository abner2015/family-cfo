"""Transaction CRUD, classification, dedupe, transfers, refunds and NLP entry."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select

from family_cfo.assets.accounts import AccountService
from family_cfo.core.errors import ValidationError
from family_cfo.database.models import (
    Account,
    AuditLog,
    CategoryRule,
    Refund,
    Transaction,
    TransferMatch,
)
from family_cfo.transactions.classify import RuleClassifier
from family_cfo.transactions.nlp import parse_entry
from family_cfo.transactions.reconcile import Reconciler
from family_cfo.transactions.service import TransactionService, compute_dedupe_key


def _account(session, settings, name="微信零钱") -> Account:
    return AccountService(session, settings).get(name)


def _create(session, settings, **overrides):
    account = overrides.pop("account", _account(session, settings))
    defaults = dict(
        account_id=account.id,
        occurred_at="2026-10-10 12:00:00",
        direction="outflow",
        amount_minor=10000,
        txn_type="expense",
        counterparty="测试商户",
        description="测试",
        source_platform="wechat",
        external_id=None,
    )
    defaults.update(overrides)
    return TransactionService(session, settings).create(**defaults)


def test_create_read_update_and_soft_delete(session, settings):
    svc = TransactionService(session, settings)
    result = _create(session, settings, external_id="T1")
    txn = result.transaction
    assert txn.status == "confirmed"
    assert txn.amount_minor == 10000

    found = svc.list(period="2026-10")
    assert [t.id for t in found] == [txn.id]

    from family_cfo.database.models import TransactionCategory

    food = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "food"))
    updated = svc.categorize(txn.id, food.id, create_rule=True)
    assert updated.category_id == food.id
    assert session.scalar(select(CategoryRule).where(CategoryRule.pattern == "测试商户")) is not None

    deleted = svc.soft_delete(txn.id, reason="录入错误")
    assert deleted.is_deleted is True
    assert svc.list(period="2026-10") == []
    logs = list(session.scalars(select(AuditLog).where(AuditLog.entity_id == txn.id)))
    assert any(log.action == "soft_delete" for log in logs)

    restored = svc.restore(txn.id)
    assert restored.is_deleted is False
    assert svc.list(period="2026-10")


def test_soft_delete_requires_reason(session, settings):
    txn = _create(session, settings).transaction
    with pytest.raises(ValidationError):
        TransactionService(session, settings).soft_delete(txn.id, reason=" ")


def test_invalid_amount_and_type_rejected(session, settings):
    with pytest.raises(ValidationError):
        _create(session, settings, amount_minor=-1)
    with pytest.raises(ValidationError):
        _create(session, settings, txn_type="not_a_type")
    with pytest.raises(ValidationError):
        _create(session, settings, direction="sideways")


def test_same_platform_dedupe_flags_second_transaction(session, settings):
    first = _create(session, settings, external_id="A1")
    second = _create(session, settings, external_id="A2")
    assert second.duplicate is True
    assert second.transaction.duplicate_of_id == first.transaction.id
    assert second.transaction.status == "pending_review"
    assert second.transaction.stats_included is False
    # Nothing is deleted: the user decides.
    assert len(list(session.scalars(select(Transaction)))) == 2


def test_external_id_uniqueness_returns_existing(session, settings):
    first = _create(session, settings, external_id="SAME")
    second = _create(session, settings, external_id="SAME", amount_minor=999)
    assert second.duplicate is True
    assert second.transaction.id == first.transaction.id
    assert len(list(session.scalars(select(Transaction)))) == 1


def test_classifier_keyword_and_unknown(session, settings):
    svc = TransactionService(session, settings)
    txn = _create(session, settings, counterparty="美团外卖", description="午餐").transaction
    classifier = RuleClassifier(session, svc.household_id)
    result = classifier.apply(txn)
    assert result.category_slug == "food"
    assert txn.status == "confirmed"

    unknown = _create(
        session, settings, counterparty="某不知名小店", description="misc", external_id="UNK"
    ).transaction
    result = classifier.apply(unknown)
    assert result.category_id is None
    assert unknown.status == "pending_review"
    assert unknown.review_reason == "uncategorized"


def test_user_rule_has_priority_and_hits_recorded(session, settings):
    svc = TransactionService(session, settings)
    from family_cfo.database.models import TransactionCategory

    pets = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "other_expense"))
    rule = svc.add_rule(pattern="宠物医院", category_id=pets.id, priority=1)
    txn = _create(session, settings, counterparty="宠物医院", description="体检").transaction
    classifier = RuleClassifier(session, svc.household_id)
    result = classifier.apply(txn)
    assert result.category_slug == "other_expense"
    assert rule.hit_count == 1


def test_rule_by_priority_order(session, settings):
    svc = TransactionService(session, settings)
    from family_cfo.database.models import TransactionCategory

    food = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "food"))
    shopping = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "shopping"))
    svc.add_rule(pattern="盒马", category_id=food.id, priority=1)
    svc.add_rule(pattern="盒马", category_id=shopping.id, priority=90)
    txn = _create(session, settings, counterparty="盒马鲜生", description="购物").transaction
    RuleClassifier(session, svc.household_id).apply(txn)
    assert txn.category_id == food.id


def test_cross_platform_duplicate_is_conservative(session, settings):
    bank = _account(session, settings, "银行卡")
    first = _create(
        session, settings, account=_account(session, settings), source_platform="wechat",
        counterparty="美团", amount_minor=5000, external_id="WX1",
    ).transaction
    second = _create(
        session, settings, account=bank, source_platform="bank",
        counterparty="美团", amount_minor=5000, external_id="BK1",
    ).transaction
    reconciler = Reconciler(session, settings, period="2026-10")
    flagged = reconciler.run().duplicates_flagged
    assert flagged == 1
    dup = first if first.duplicate_of_id else second
    assert dup.duplicate_of_id is not None
    assert dup.stats_included is False
    # The user can confirm it is a distinct transaction.
    reconciler.confirm_duplicate(dup.id, distinct=True)
    assert dup.stats_included is True
    assert dup.duplicate_of_id is None


def test_cross_platform_different_merchant_not_flagged(session, settings):
    bank = _account(session, settings, "银行卡")
    _create(session, settings, counterparty="美团", amount_minor=5000, external_id="WX1")
    _create(session, settings, account=bank, source_platform="bank", counterparty="星巴克", amount_minor=5000, external_id="BK1")
    result = Reconciler(session, settings, period="2026-10").run()
    assert result.duplicates_flagged == 0


def test_internal_transfer_matching(session, settings):
    bank = _account(session, settings, "银行卡")
    out_txn = _create(
        session, settings, account=bank, source_platform="bank", direction="outflow",
        txn_type="transfer", amount_minor=200000, counterparty="本人微信", description="转账",
        external_id="OUT1",
    ).transaction
    in_txn = _create(
        session, settings, account=_account(session, settings), source_platform="wechat",
        direction="inflow", txn_type="topup", amount_minor=200000,
        counterparty="零钱充值", description="充值", external_id="IN1",
    ).transaction
    result = Reconciler(session, settings, period="2026-10").run()
    assert result.transfers_matched == 1
    assert out_txn.transfer_group_id and out_txn.transfer_group_id == in_txn.transfer_group_id
    assert out_txn.stats_included is False
    assert in_txn.stats_included is False
    match = session.scalar(select(TransferMatch))
    assert match is not None


def test_credit_card_repayment_matching(session, settings):
    card = _account(session, settings, "信用卡")
    out_txn = _create(
        session, settings, account=_account(session, settings), direction="outflow",
        txn_type="credit_card_repayment", amount_minor=300000, counterparty="招商银行信用卡",
        description="信用卡还款", external_id="PAY1",
    ).transaction
    _create(
        session, settings, account=card, source_platform="credit_card", direction="inflow",
        txn_type="transfer", amount_minor=300000, counterparty="还款入账", description="还款",
        external_id="PAY2",
    ).transaction
    result = Reconciler(session, settings, period="2026-10").run()
    assert result.repayments_matched == 1
    assert out_txn.txn_type == "credit_card_repayment"
    assert out_txn.stats_included is False


def test_refund_linking_and_category_copy(session, settings):
    from family_cfo.database.models import TransactionCategory

    food = session.scalar(select(TransactionCategory).where(TransactionCategory.slug == "food"))
    original = _create(
        session, settings, amount_minor=39900, counterparty="美团", description="外卖",
        external_id="ORIG", category_id=food.id,
    ).transaction
    refund = _create(
        session, settings, direction="inflow", txn_type="refund", amount_minor=39900,
        counterparty="美团", description="退款", external_id="REF", occurred_at="2026-10-15 10:00:00",
    ).transaction
    result = Reconciler(session, settings, period="2026-10").run()
    assert result.refunds_linked == 1
    assert refund.refund_of_id == original.id
    assert refund.category_id == food.id
    assert session.scalar(select(Refund).where(Refund.refund_txn_id == refund.id)) is not None


def test_unlinked_refund_goes_to_review(session, settings):
    refund = _create(
        session, settings, direction="inflow", txn_type="refund", amount_minor=5000,
        counterparty="陌生商户", description="退款", external_id="REF2",
    ).transaction
    Reconciler(session, settings, period="2026-10").run()
    assert refund.status == "pending_review"
    assert refund.review_reason == "refund_unlinked"


def test_multi_currency_requires_fx_rate(session, settings):
    txn = _create(session, settings, currency="USD", amount_minor=1000).transaction
    assert txn.currency == "USD"
    assert txn.status == "pending_review"
    assert txn.review_reason == "fx_missing"
    assert txn.amount_base_minor == 0
    assert txn.stats_included is False

    converted = _create(
        session, settings, currency="USD", amount_minor=1000, fx_rate="7.2", external_id="USD2"
    ).transaction
    assert converted.currency == "USD"
    assert converted.amount_minor == 1000
    assert converted.amount_base_minor == 7200


def test_dedupe_key_is_stable_and_merchant_sensitive():
    base = dict(
        household_id="hh", direction="outflow", amount_minor=1000, currency="CNY",
        occurred_at=datetime(2026, 10, 1, 12, 0), counterparty="美团", description="外卖",
    )
    assert compute_dedupe_key(**base) == compute_dedupe_key(**base)
    other = dict(base, counterparty="星巴克")
    assert compute_dedupe_key(**base) != compute_dedupe_key(**other)


def test_natural_language_expense(session, settings):
    parsed = parse_entry("午餐 35 元", session, settings)
    assert parsed.amount_minor == 3500
    assert parsed.direction == "outflow"
    assert parsed.category_name == "餐饮"
    assert parsed.confidence > 0


def test_natural_language_income_and_date(session, settings):
    parsed = parse_entry("2026-10-05 工资 12000", session, settings)
    assert parsed.amount_minor == 1200000
    assert parsed.direction == "inflow"
    assert parsed.occurred_at.date() == date(2026, 10, 5)
    assert parsed.category_name == "工资"


def test_natural_language_rejects_missing_amount(session, settings):
    with pytest.raises(ValueError):
        parse_entry("今天买了点菜", session, settings)


def test_custom_category_and_keywords_are_used(session, settings):
    svc = TransactionService(session, settings)
    category = svc.create_category(
        name="宠物", kind="expense", keywords=["猫粮", "宠物医院"], is_essential=False
    )
    txn = _create(session, settings, counterparty="猫粮旗舰店", description="猫粮").transaction
    result = RuleClassifier(session, svc.household_id).apply(txn)
    assert result.category_id == category.id
    assert txn.category_id == category.id
    with pytest.raises(ValidationError):
        svc.create_category(name="宠物", kind="expense")
