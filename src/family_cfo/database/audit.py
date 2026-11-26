"""Audit trail helpers: every mutation of financial state is recorded."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..core.ids import new_id
from .models import AuditLog


def _jsonable(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def record_audit(
    session: Session,
    *,
    household_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    actor: str = "cli",
    before: dict | None = None,
    after: dict | None = None,
    detail: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        id=new_id("audit"),
        household_id=household_id,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=_jsonable(before),
        after_json=_jsonable(after),
        detail=detail,
    )
    session.add(entry)
    return entry
