"""Automation tasks: backup-before-collect and no implicit close."""

from __future__ import annotations

from sqlalchemy import select

from family_cfo.automation.tasks import monthly_collect
from family_cfo.database.models import Settlement


def test_monthly_collect_backs_up_and_does_not_close(db, settings, import_fixture, session):
    import_fixture("wechat_sample.csv")
    result = monthly_collect(db, settings, "2026-10")
    assert result["backup"] is not None
    assert settings.backups_dir.exists()
    assert list(settings.backups_dir.glob("family_cfo-*.db"))
    record = session.scalar(select(Settlement).where(Settlement.period == "2026-10"))
    assert record is not None
    assert record.status != "CLOSED"
