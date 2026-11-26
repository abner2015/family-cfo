"""Feishu Bitable client + sync against the in-memory mock server."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from family_cfo.budgets.service import BudgetService
from family_cfo.core.errors import (
    FeishuError,
    FeishuNotConfigured,
    ValidationError,
)
from family_cfo.core.logging import redact
from family_cfo.database.models import Budget, SyncRecord, Transaction
from family_cfo.integrations.feishu.client import FeishuClient
from family_cfo.integrations.feishu.schema import load_schema
from family_cfo.planning.service import PlanningService


def test_schema_yaml_matches_default_tables(settings):
    specs = load_schema(settings.project_root)
    assert set(specs) == {
        "transactions", "assets", "liabilities", "budgets", "goals", "monthly_reports"
    }
    assert specs["transactions"].primary_field == "业务ID"
    assert "金额" in specs["transactions"].field_names()


def test_not_configured_does_not_pretend(settings):
    client = FeishuClient(settings.with_overrides(
        feishu_app_id="", feishu_app_secret="", feishu_app_token=""
    ))
    status = client.status()
    assert status["configured"] is False
    assert status["connected"] is False
    with pytest.raises(FeishuNotConfigured):
        client.healthcheck()
    client.close()


def test_ensure_tables_creates_and_is_idempotent(sync_service, mock_feishu):
    assert len(mock_feishu.tables) == 6
    mapping = sync_service.ensure_tables()
    assert len(mapping) == 6
    sync_service.ensure_tables()
    assert len(mock_feishu.tables) == 6


def test_push_transactions_and_skip_unchanged(sync_service, import_fixture, mock_feishu):
    import_fixture("wechat_sample.csv")
    first = sync_service.push("transactions")
    assert first.created == 11
    assert not first.failed
    table = mock_feishu.table_by_name("FamilyCFO_交易明细")
    assert len(table.records) == 11
    second = sync_service.push("transactions")
    assert second.created == 0
    assert second.skipped == 11


def test_push_updates_changed_record(sync_service, import_fixture, session, mock_feishu):
    import_fixture("wechat_sample.csv")
    sync_service.push("transactions")
    txn = session.scalar(select(Transaction).limit(1))
    txn.counterparty = "更新后的商户"
    session.commit()
    report = sync_service.push("transactions")
    assert report.updated == 1
    assert report.created == 0


def test_dry_run_writes_nothing(sync_service, import_fixture, mock_feishu):
    import_fixture("wechat_sample.csv")
    report = sync_service.push("transactions", dry_run=True)
    assert report.dry_run is True
    assert report.created == 11
    assert mock_feishu.table_by_name("FamilyCFO_交易明细").records == {}


def test_conflict_is_not_silently_overwritten(sync_service, session, mock_feishu):
    goal = PlanningService(session, sync_service.settings).add_goal(
        name="教育金", target="200000", current="50000", target_date=date(2030, 9, 1)
    )
    session.commit()
    sync_service.push("goals")
    record = session.scalar(
        select(SyncRecord).where(SyncRecord.entity_id == goal.id, SyncRecord.direction == "push")
    )
    mock_feishu.edit_remote("FamilyCFO_目标", record.remote_record_id, {"当前金额": 60000.0})
    goal.current_minor = 70000
    session.commit()

    report = sync_service.push("goals")
    assert report.conflicts
    assert goal.current_minor == 70000  # local ledger untouched
    conflicts = sync_service.conflicts()
    assert conflicts and conflicts[0]["entity_id"] == goal.id


def test_resolve_conflict_local_wins(sync_service, session, mock_feishu):
    goal = PlanningService(session, sync_service.settings).add_goal(
        name="教育金", target="200000", current="50000"
    )
    session.commit()
    sync_service.push("goals")
    record = session.scalar(
        select(SyncRecord).where(SyncRecord.entity_id == goal.id, SyncRecord.direction == "push")
    )
    mock_feishu.edit_remote("FamilyCFO_目标", record.remote_record_id, {"当前金额": 60000.0})
    goal.current_minor = 70000
    session.commit()
    sync_service.push("goals")
    report = sync_service.resolve("goals", goal.id, strategy="local")
    assert report.updated == 1
    remote = mock_feishu.find_record("FamilyCFO_目标", "业务ID", goal.id)
    assert remote["fields"]["当前金额"] == 700.0


def test_pull_goals_updates_local(sync_service, session, mock_feishu):
    goal = PlanningService(session, sync_service.settings).add_goal(
        name="教育金", target="200000", current="50000"
    )
    session.commit()
    sync_service.push("goals")
    record = mock_feishu.find_record("FamilyCFO_目标", "业务ID", goal.id)
    mock_feishu.edit_remote("FamilyCFO_目标", record["record_id"], {"当前金额": 60000.0})
    report = sync_service.pull("goals")
    session.expire_all()
    assert report.updated == 1
    assert goal.current_minor == 6000000


def test_pull_rejects_invalid_remote_values(sync_service, session, mock_feishu):
    goal = PlanningService(session, sync_service.settings).add_goal(
        name="教育金", target="200000", current="50000"
    )
    session.commit()
    sync_service.push("goals")
    record = mock_feishu.find_record("FamilyCFO_目标", "业务ID", goal.id)
    mock_feishu.edit_remote("FamilyCFO_目标", record["record_id"], {"目标金额": 0.0})
    report = sync_service.pull("goals")
    session.expire_all()
    assert report.failed
    assert goal.target_minor == 20000000


def test_pull_conflicts_when_local_also_changed(sync_service, session, mock_feishu):
    goal = PlanningService(session, sync_service.settings).add_goal(
        name="教育金", target="200000", current="50000"
    )
    session.commit()
    sync_service.push("goals")
    record = mock_feishu.find_record("FamilyCFO_目标", "业务ID", goal.id)
    mock_feishu.edit_remote("FamilyCFO_目标", record["record_id"], {"当前金额": 60000.0})
    goal.current_minor = 5500000
    session.commit()
    report = sync_service.pull("goals")
    session.expire_all()
    assert report.conflicts
    assert goal.current_minor == 5500000


def test_pull_budgets_creates_new_version(sync_service, session, mock_feishu, settings):
    service = BudgetService(session, settings)
    budget = service.set_budget(
        period="2026-10", items=[{"category": "餐饮", "amount": "1000"}]
    )
    session.commit()
    sync_service.push("budgets")
    table = mock_feishu.table_by_name("FamilyCFO_预算")
    for record in table.records.values():
        record["fields"]["版本"] = 2
        record["fields"]["预算金额"] = 1500.0
        record["last_modified_time"] += 1000
    report = sync_service.pull("budgets")
    session.expire_all()
    assert report.updated == 1
    active = service.active("2026-10")
    assert active.version == 2
    assert active.total_amount_minor == 150000
    assert session.get(Budget, budget.id).status == "superseded"


def test_read_only_entities_reject_pull(sync_service):
    with pytest.raises(ValidationError):
        sync_service.pull("transactions")


def test_retry_on_rate_limit(sync_service, session, mock_feishu):
    PlanningService(session, sync_service.settings).add_goal(name="应急金", target="60000")
    session.commit()
    mock_feishu.fail_next(1254290, times=1)
    report = sync_service.push("goals")
    assert report.created == 1
    assert not report.failed


def test_push_failure_does_not_touch_ledger(sync_service, session, mock_feishu, monkeypatch):
    goal = PlanningService(session, sync_service.settings).add_goal(
        name="教育金", target="200000", current="50000"
    )
    session.commit()
    before = goal.current_minor

    def boom(*args, **kwargs):
        raise FeishuError("simulated network failure")

    monkeypatch.setattr(sync_service.client, "create_record", boom)
    report = sync_service.push("goals")
    assert report.failed
    assert goal.current_minor == before


def test_client_masks_credentials_and_redacts_logs(settings):
    client = FeishuClient(settings.with_overrides(
        feishu_app_id="cli_secret_app", feishu_app_secret="very-secret", feishu_app_token="apptoken123456"
    ))
    assert client.status()["app_token_set"] is True  # boolean only, never the token
    assert "***" in client._mask("apptoken123456")
    assert "very-secret" not in redact("app_secret=very-secret")
    assert "t-abc123456789" not in redact("Authorization: Bearer t-abc123456789")
    client.close()


def test_sync_records_are_stable_business_ids(sync_service, import_fixture, session):
    import_fixture("wechat_sample.csv")
    sync_service.push("transactions")
    rows = list(session.scalars(select(SyncRecord).where(SyncRecord.direction == "push")))
    assert len(rows) == 11
    assert all(r.remote_record_id for r in rows)
    assert len({r.entity_id for r in rows}) == 11


def test_pull_budgets_is_idempotent(sync_service, session, mock_feishu, settings):
    service = BudgetService(session, settings)
    service.set_budget(period="2026-10", items=[{"category": "餐饮", "amount": "1000"}])
    session.commit()
    sync_service.push("budgets")
    report = sync_service.pull("budgets")
    assert report.skipped == 1
    assert service.active("2026-10").version == 1


def test_pull_budgets_amount_edit_without_version_bump(sync_service, session, mock_feishu, settings):
    service = BudgetService(session, settings)
    service.set_budget(period="2026-10", items=[{"category": "餐饮", "amount": "1000"}])
    session.commit()
    sync_service.push("budgets")
    table = mock_feishu.table_by_name("FamilyCFO_预算")
    for record in table.records.values():
        record["fields"]["预算金额"] = 1800.0  # user edits amount, version stays 1
        record["last_modified_time"] += 1000
    report = sync_service.pull("budgets")
    session.expire_all()
    assert report.updated == 1
    assert service.active("2026-10").version == 2
    assert service.active("2026-10").total_amount_minor == 180000


def test_failed_push_is_recorded_without_touching_ledger(sync_service, session, monkeypatch):
    from family_cfo.core.errors import FeishuError
    from family_cfo.database.models import SyncRecord

    goal = PlanningService(session, sync_service.settings).add_goal(name="应急金", target="60000")
    session.commit()
    before = goal.target_minor

    def boom(*args, **kwargs):
        raise FeishuError("simulated failure")

    monkeypatch.setattr(sync_service.client, "create_record", boom)
    report = sync_service.push("goals")
    assert report.failed
    assert goal.target_minor == before
    record = session.scalar(
        select(SyncRecord).where(
            SyncRecord.entity_id == goal.id, SyncRecord.direction == "push"
        )
    )
    assert record.status == "failed"
    assert "simulated failure" in (record.last_error or "")


def test_resolve_conflict_remote_wins(sync_service, session, mock_feishu):
    goal = PlanningService(session, sync_service.settings).add_goal(
        name="教育金", target="200000", current="50000"
    )
    session.commit()
    sync_service.push("goals")
    record = mock_feishu.find_record("FamilyCFO_目标", "业务ID", goal.id)
    mock_feishu.edit_remote("FamilyCFO_目标", record["record_id"], {"当前金额": 60000.0})
    goal.current_minor = 70000
    session.commit()
    conflict = sync_service.push("goals")
    assert conflict.conflicts
    resolved = sync_service.resolve("goals", goal.id, strategy="remote")
    session.expire_all()
    assert resolved.updated == 1
    assert goal.current_minor == 6000000


def test_pull_budgets_ignores_old_versions(sync_service, session, mock_feishu, settings):
    service = BudgetService(session, settings)
    service.set_budget(period="2026-10", items=[{"category": "餐饮", "amount": "1000"}])
    session.commit()
    sync_service.push("budgets")
    service.set_budget(period="2026-10", items=[{"category": "餐饮", "amount": "1500"}])
    session.commit()
    sync_service.push("budgets")
    # Both v1 and v2 rows now exist remotely; pull must compare against v2 only.
    report = sync_service.pull("budgets")
    assert not report.failed
    assert report.conflicts == []
    assert service.active("2026-10").version == 2


def test_client_token_is_deterministic_uuid4():
    import uuid

    from family_cfo.integrations.feishu.sync import _client_token

    token = _client_token("transactions", "txn_1")
    assert token == _client_token("transactions", "txn_1")
    assert token != _client_token("transactions", "txn_2")
    parsed = uuid.UUID(token)
    assert parsed.version == 4
