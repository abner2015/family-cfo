"""Stable identifiers and hashing."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

ID_PREFIXES = {
    "household": "hh",
    "member": "mem",
    "account": "acc",
    "batch": "imp",
    "raw": "raw",
    "txn": "txn",
    "category": "cat",
    "rule": "rule",
    "transfer": "tfm",
    "refund": "rfd",
    "asset": "ast",
    "valuation": "val",
    "liability": "lia",
    "payment": "pay",
    "budget": "bud",
    "budget_item": "bit",
    "product": "prd",
    "inv_txn": "itx",
    "position": "pos",
    "goal": "goal",
    "snapshot": "snap",
    "sync": "sync",
    "audit": "aud",
    "settlement": "stl",
    "adjustment": "adj",
    "report": "rpt",
}


def new_id(kind: str) -> str:
    prefix = ID_PREFIXES.get(kind, kind[:4] or "id")
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def stable_key(*parts: Any) -> str:
    """Deterministic hash used for dedupe keys and business keys.

    ``None`` is encoded distinctly from the string ``""``.
    """
    payload = json.dumps(
        ["__none__" if p is None else str(p) for p in parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short_key(*parts: Any, length: int = 24) -> str:
    return stable_key(*parts)[:length]


def file_sha256(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def payload_hash(payload: dict[str, Any]) -> str:
    return stable_key(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
