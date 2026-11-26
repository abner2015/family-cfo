"""Database initialisation, integrity, audit and backup/restore."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from family_cfo.core.errors import BackupError, ValidationError
from family_cfo.database.backup import backup_database, prune_backups, restore_database, verify_backup
from family_cfo.database.base import Database
from family_cfo.database.init import initialize
from family_cfo.database.integrity import run_integrity_checks
from family_cfo.database.models import (
    Account,
    AuditLog,
    FamilyMember,
    Household,
    SystemConfig,
    TransactionCategory,
)
from family_cfo.assets.accounts import AccountService
from family_cfo.transactions.service import TransactionService


REQUIRED_TABLES = {
    "households",
    "family_members",
    "accounts",
    "import_batches",
    "raw_transactions",
    "transactions",
    "transaction_categories",
    "category_rules",
    "transfer_matches",
    "refunds",
    "assets",
    "asset_valuations",
    "liabilities",
    "liability_payments",
    "budgets",
    "budget_items",
    "investment_products",
    "investment_transactions",
    "investment_positions",
    "financial_goals",
    "financial_snapshots",
    "sync_records",
    "audit_logs",
    "system_configs",
    "settlements",
}


def test_initialize_creates_schema_and_seed_data(db: Database, settings):
    tables = set(inspect(db.engine).get_table_names())
    assert REQUIRED_TABLES <= tables
    with db.session() as session:
        assert session.scalar(select(Household)) is not None
        assert session.scalar(select(FamilyMember)) is not None
        categories = list(session.scalars(select(TransactionCategory)))
        assert len(categories) >= 20
        assert {c.slug for c in categories} >= {"food", "salary", "internal_transfer"}
        assert len(list(session.scalars(select(Account)))) == 5
        assert session.get(SystemConfig, "schema_version") is not None


def test_schema_version_recorded(db: Database):
    with db.session() as session:
        row = session.get(SystemConfig, "schema_version")
        assert row.value == "1"


def test_foreign_keys_enforced(db: Database):
    with pytest.raises(Exception):
        with db.session() as session:
            session.execute(
                text(
                    "INSERT INTO family_members (id, household_id, name, role, is_active, created_at, updated_at) "
                    "VALUES ('mem_x', 'hh_missing', 'x', 'member', 1, '2026-01-01', '2026-01-01')"
                )
            )


def test_money_is_integer_minor_units(db: Database, settings):
    with db.session() as session:
        accounts = AccountService(session, settings)
        account = accounts.list()[0]
        txn = TransactionService(session, settings).create(
            account_id=account.id,
            occurred_at="2026-10-01 10:00:00",
            direction="outflow",
            amount_minor=3550,
            txn_type="expense",
            counterparty="测试商户",
        ).transaction
        assert isinstance(txn.amount_minor, int)
        assert txn.amount_minor == 3550
        assert txn.amount_base_minor == 3550
        raw = session.execute(
            text("SELECT typeof(amount_minor) FROM transactions WHERE id=:id"), {"id": txn.id}
        ).scalar()
        assert raw == "integer"


def test_init_is_idempotent(db: Database, settings):
    first = initialize(db, settings)
    second = initialize(db, settings)
    assert first.household_id == second.household_id
    assert second.created is False
    with db.session() as session:
        assert len(list(session.scalars(select(Household)))) == 1


def test_integrity_report_clean_on_fresh_db(db: Database):
    with db.session() as session:
        household = session.scalar(select(Household))
        report = run_integrity_checks(session, household.id)
        assert report.ok
        assert all(c.severity != "error" for c in report.checks)


def test_integrity_detects_uncategorized_and_pending(db: Database, settings):
    with db.session() as session:
        account = AccountService(session, settings).list()[0]
        TransactionService(session, settings).create(
            account_id=account.id,
            occurred_at="2026-10-02 10:00:00",
            direction="outflow",
            amount_minor=1234,
            txn_type="expense",
            counterparty="未知商户",
            status="pending_review",
            review_reason="uncategorized",
        )
        household = session.scalar(select(Household))
        report = run_integrity_checks(session, household.id)
        names = {c.name: c for c in report.checks}
        assert names["pending_review"].count == 1
        assert names["uncategorized"].count == 1
        assert report.ok  # warnings do not make the ledger invalid


def test_audit_log_written_for_manual_entry(db: Database, settings):
    with db.session() as session:
        account = AccountService(session, settings).list()[0]
        txn = TransactionService(session, settings).create(
            account_id=account.id,
            occurred_at="2026-10-03",
            direction="outflow",
            amount_minor=100,
            counterparty="手工",
            audit=True,
        ).transaction
        logs = list(session.scalars(select(AuditLog).where(AuditLog.entity_id == txn.id)))
        assert logs and logs[0].action == "create"


def test_backup_and_restore_roundtrip(db: Database, settings):
    with db.session() as session:
        account = AccountService(session, settings).list()[0]
        TransactionService(session, settings).create(
            account_id=account.id,
            occurred_at="2026-10-04",
            direction="outflow",
            amount_minor=4321,
            counterparty="备份测试",
        )
    info = backup_database(settings.db_path, settings.backups_dir)
    assert info.integrity == "ok"
    assert info.table_counts["transactions"] == 1
    assert info.sha256

    with db.session() as session:
        account = AccountService(session, settings).list()[0]
        TransactionService(session, settings).create(
            account_id=account.id,
            occurred_at="2026-10-05",
            direction="outflow",
            amount_minor=100,
            counterparty="备份后",
        )

    result = restore_database(
        info.path, settings.db_path, backup_dir=settings.backups_dir, engine=db.engine
    )
    assert result["safety_backup"]
    with db.session() as session:
        count = session.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()
        assert count == 1


def test_backup_verification_rejects_corrupt_file(tmp_path: Path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite database")
    with pytest.raises(BackupError):
        verify_backup(bad)


def test_prune_backups_keeps_latest(db: Database, settings):
    for _ in range(3):
        backup_database(settings.db_path, settings.backups_dir)
    removed = prune_backups(settings.backups_dir, keep=1)
    assert len(removed) == 2
    remaining = list(settings.backups_dir.glob("family_cfo-*.db"))
    assert len(remaining) == 1


def test_duplicate_account_name_rejected(db: Database, settings):
    with db.session() as session:
        service = AccountService(session, settings)
        with pytest.raises(ValidationError):
            service.create(name=service.list()[0].name)


def test_alembic_revision_is_stamped(db: Database):
    with db.engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert row == [("0001_initial",)]
