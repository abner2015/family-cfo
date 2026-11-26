"""Markdown rendering helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..core.money import format_money


def md_table(headers: Iterable[str], rows: Iterable[Iterable[object]]) -> str:
    header_list = [str(h) for h in headers]
    lines = ["| " + " | ".join(header_list) + " |"]
    lines.append("| " + " | ".join("---" for _ in header_list) + " |")
    for row in rows:
        cells = []
        for value in row:
            if value is None:
                cells.append("—")
            elif isinstance(value, bool):
                cells.append("是" if value else "否")
            else:
                text = str(value).replace("|", "\\|").replace("\n", " ")
                cells.append(text)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def money(minor: int, currency: str = "CNY") -> str:
    return format_money(minor, currency)


def percent(value: Decimal | str | None) -> str:
    if value is None:
        return "—"
    try:
        dec = Decimal(str(value))
    except Exception:
        return str(value)
    return f"{(dec * 100).quantize(Decimal('0.1'))}%"


def signed_money(minor: int, currency: str = "CNY") -> str:
    text = format_money(abs(minor), currency)
    return f"+{text}" if minor > 0 else (f"-{text}" if minor < 0 else text)
