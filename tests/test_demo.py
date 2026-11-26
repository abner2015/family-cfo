"""Synthetic demo data seeding."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from family_cfo.assets.accounts import AccountService
from family_cfo.cli import app
from family_cfo.core.errors import ValidationError
from family_cfo.database.models import Transaction
from family_cfo.demo import seed_demo
from family_cfo.finance.metrics import build_metrics, period_summary


def test_demo_seed_is_labelled_and_consistent(db, settings, session):
    result = seed_demo(db, settings)
    assert result["demo"] is True
    assert result["counts"]["transactions"] >= 20
    household_id = AccountService(session, settings).household_id
    period = result["periods"][-1]
    summary = period_summary(session, household_id, period)
    assert summary.income_minor > 0
    assert summary.expense_minor > 0
    txns = list(session.scalars(select(Transaction)))
    assert all((t.counterparty or "").startswith("演示") for t in txns)
    assert all(t.source_platform == "demo" for t in txns)
    metrics = build_metrics(session, settings, period=period)
    assert metrics["balance_sheet"]["net_worth_minor"] != 0


def test_demo_seed_reconciles_transfer_and_refund(db, settings, session):
    result = seed_demo(db, settings)
    assert result["periods"]
    txns = list(session.scalars(select(Transaction)))
    assert any(t.transfer_group_id for t in txns)
    assert any(t.refund_of_id for t in txns)


def test_demo_refuses_non_empty_ledger(db, settings):
    seed_demo(db, settings)
    with pytest.raises(ValidationError):
        seed_demo(db, settings)


def test_demo_cli_requires_yes(tmp_path):
    runner = CliRunner()
    base = ["--root", str(tmp_path), "--db", str(tmp_path / "data" / "family_cfo.db")]
    assert runner.invoke(app, [*base, "init"]).exit_code == 0
    refused = runner.invoke(app, [*base, "demo"])
    assert refused.exit_code == 2
    accepted = runner.invoke(app, [*base, "--json", "demo", "--yes"])
    assert accepted.exit_code == 0, accepted.output
    payload = json.loads(accepted.output)
    assert payload["demo"] is True
    assert "合成" in payload["warning"]
