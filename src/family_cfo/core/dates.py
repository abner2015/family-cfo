"""Date/time helpers.

The ledger stores naive local timestamps in the household timezone
(default ``Asia/Shanghai``).  A bill's original time is preserved as a string
in ``raw_transactions``; everything computed uses ``occurred_at``.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .errors import ValidationError

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S%z",
)

_PERIOD_RE = re.compile(r"^(\d{4})[-/年.]?(\d{1,2})?$")


def tzinfo(name: str = "Asia/Shanghai") -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - only on hosts without tzdata
        return ZoneInfo("UTC")


def now_local(tz_name: str = "Asia/Shanghai") -> datetime:
    return datetime.now(tzinfo(tz_name)).replace(tzinfo=None, microsecond=0)


def today_local(tz_name: str = "Asia/Shanghai") -> date:
    return now_local(tz_name).date()


def parse_datetime(value: object, *, tz_name: str = "Asia/Shanghai") -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(tzinfo(tz_name)).replace(tzinfo=None)
        return value.replace(microsecond=0)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if value is None:
        raise ValidationError("日期为空")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel serial date or epoch milliseconds / seconds.
        num = float(value)
        if 1_000_000_000 <= num <= 4_000_000_000:
            return datetime.fromtimestamp(num, tz=tzinfo(tz_name)).replace(tzinfo=None)
        if 1_000_000_000_000 <= num <= 4_000_000_000_000:
            return datetime.fromtimestamp(num / 1000, tz=tzinfo(tz_name)).replace(tzinfo=None)
        if 20000 <= num <= 80000:  # Excel serial (1899-12-30 epoch)
            from datetime import timedelta as _td

            return (datetime(1899, 12, 30) + _td(days=num)).replace(microsecond=0)
        raise ValidationError(f"无法解析日期数值: {value!r}")
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "nat", "none"}:
        raise ValidationError("日期为空")
    text = text.replace("Z", "+00:00") if text.endswith("Z") else text
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(tzinfo(tz_name)).replace(tzinfo=None)
            return parsed.replace(microsecond=0)
        except ValueError:
            continue
    # Last resort: ISO parser (handles 2026-10-01T12:00:00+08:00)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"无法解析日期: {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tzinfo(tz_name)).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def parse_period(value: str | date | datetime | None, *, default: date | None = None) -> str:
    """Normalise a month selector to ``YYYY-MM``.

    ``None`` means "current month" (or ``default`` when supplied).
    """
    if value is None:
        ref = default or today_local()
        return f"{ref.year:04d}-{ref.month:02d}"
    if isinstance(value, (date, datetime)):
        return f"{value.year:04d}-{value.month:02d}"
    text = str(value).strip()
    match = _PERIOD_RE.match(text)
    if not match:
        raise ValidationError(f"无法解析月份: {value!r} (期望 YYYY-MM)")
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    if not 1 <= month <= 12:
        raise ValidationError(f"月份超出范围: {value!r}")
    return f"{year:04d}-{month:02d}"


def month_bounds(period: str) -> tuple[date, date]:
    period = parse_period(period)
    year, month = (int(p) for p in period.split("-"))
    last = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def month_start(period: str) -> date:
    return month_bounds(period)[0]


def month_end(period: str) -> date:
    return month_bounds(period)[1]


def period_of(value: date | datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def add_months(period: str, delta: int) -> str:
    year, month = (int(p) for p in parse_period(period).split("-"))
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def previous_period(period: str, count: int = 1) -> str:
    return add_months(period, -count)


def period_range(start: str, end: str) -> list[str]:
    start = parse_period(start)
    end = parse_period(end)
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def year_bounds(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def within(dt: datetime, start: date, end: date) -> bool:
    return start <= dt.date() <= end


def days_between(a: date, b: date) -> int:
    return abs((a - b).days)


def utc_offset_hours(dt: datetime, tz_name: str = "Asia/Shanghai") -> int:
    offset = dt.replace(tzinfo=tzinfo(tz_name)).utcoffset() or timedelta(0)
    return int(offset.total_seconds() // 3600)
