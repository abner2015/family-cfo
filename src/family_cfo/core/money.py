"""Money handling.

Rules enforced across the codebase:

* Monetary amounts are stored as **integer minor units** (fen for CNY) in the
  database.  No float ever touches a financial amount.
* ``Decimal`` is used for every calculation and converted only at the edges.
* The currency code travels with the amount; the original amount and the
  base-currency amount are separate columns.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import MoneyError

DEFAULT_CURRENCY = "CNY"
# Currencies with no minor unit.  Everything else here uses 2 decimals, which
# covers CNY/USD/EUR/JPY-style household use.  Extend deliberately, never guess.
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK"}
THREE_DECIMAL = {"BHD", "KWD", "OMR", "TND"}
CURRENCY_SYMBOLS = {
    "¥": "CNY",
    "￥": "CNY",
    "RMB": "CNY",
    "CNY": "CNY",
    "$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "HK$": "HKD",
    "HKD": "HKD",
    "JPY": "JPY",
}

_CLEAN_RE = re.compile(r"[,\s\u00a0\u3000]")


def currency_exponent(currency: str) -> int:
    cur = (currency or DEFAULT_CURRENCY).upper()
    if cur in ZERO_DECIMAL:
        return 0
    if cur in THREE_DECIMAL:
        return 3
    return 2


def quantize_money(value: Decimal | int | str, currency: str = DEFAULT_CURRENCY) -> Decimal:
    exp = currency_exponent(currency)
    quantum = Decimal(1).scaleb(-exp)
    return Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)


def parse_money(value: object, *, currency: str = DEFAULT_CURRENCY, allow_negative: bool = True) -> Decimal:
    """Parse a human/export amount into a Decimal.

    Handles ``¥1,234.56``, ``1 234.56``, ``(123.45)`` (accounting negative),
    ``123.45-``, ``+12``, ``1,234``, empty values (error, never zero).
    """
    if value is None:
        raise MoneyError("金额为空")
    if isinstance(value, bool):
        raise MoneyError(f"无法将布尔值解析为金额: {value!r}")
    if isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, Decimal):
        result = value
    elif isinstance(value, float):
        # Floats from pandas are permitted at the boundary only, converted via str
        # to avoid binary representation artefacts.
        result = Decimal(repr(value))
    else:
        text = str(value).strip()
        if text == "" or text.lower() in {"nan", "none", "null", "--"}:
            raise MoneyError(f"金额为空或无效: {value!r}")
        negative = False
        if text.startswith("(") and text.endswith(")"):
            negative = True
            text = text[1:-1]
        if text.endswith("-"):
            negative = True
            text = text[:-1]
        for sym, cur in CURRENCY_SYMBOLS.items():
            if cur == currency.upper() and text.upper().startswith(sym.upper()):
                text = text[len(sym) :]
        # Disambiguate thousands separators: 1,234.56 keeps comma as separator,
        # 1.234,56 (European) is normalised too.
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", text):
            text = text.replace(".", "").replace(",", ".")
        text = _CLEAN_RE.sub("", text)
        text = text.replace("−", "-")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"无法解析金额: {value!r}") from exc
        if negative:
            result = -abs(result)
    if not allow_negative and result < 0:
        raise MoneyError(f"金额不能为负数: {result}")
    return quantize_money(result, currency)


def to_minor(amount: Decimal | int | str, currency: str = DEFAULT_CURRENCY) -> int:
    """Convert an amount to integer minor units without float arithmetic."""
    dec = parse_money(amount, currency=currency)
    exp = currency_exponent(currency)
    return int((dec * (Decimal(10) ** exp)).to_integral_value(rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str = DEFAULT_CURRENCY) -> Decimal:
    exp = currency_exponent(currency)
    return (Decimal(minor) / (Decimal(10) ** exp)).quantize(Decimal(1).scaleb(-exp))


def format_money(minor: int, currency: str = DEFAULT_CURRENCY, *, symbol: bool = True) -> str:
    dec = from_minor(minor, currency)
    sign = "-" if dec < 0 else ""
    body = f"{abs(dec):,.{currency_exponent(currency)}f}"
    if not symbol:
        return f"{sign}{body} {currency}"
    prefix = {"CNY": "¥", "USD": "$", "EUR": "€", "GBP": "£", "HKD": "HK$"}.get(
        currency.upper(), f"{currency} "
    )
    return f"{sign}{prefix}{body}"


def decimal_str(value: Decimal | int | str | None, places: int = 8) -> str | None:
    """Canonical string form for non-monetary decimals (quantities, rates)."""
    if value is None:
        return None
    dec = Decimal(str(value))
    norm = dec.normalize()
    text = format(norm, "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        frac = frac[:places].rstrip("0")
        return f"{whole}.{frac}" if frac else whole
    return text


def safe_decimal(value: Decimal | int | str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def safe_div(numerator: Decimal | int | float, denominator: Decimal | int | float) -> Decimal | None:
    """Decimal division that returns None on a zero denominator (never inf/nan)."""
    den = Decimal(str(denominator))
    if den == 0:
        return None
    return Decimal(str(numerator)) / den


def ratio(numerator: Decimal | int, denominator: Decimal | int, *, places: int = 4) -> Decimal | None:
    value = safe_div(numerator, denominator)
    if value is None:
        return None
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def pct(numerator: Decimal | int, denominator: Decimal | int, *, places: int = 1) -> str | None:
    value = ratio(numerator, denominator, places=places + 2)
    if value is None:
        return None
    return f"{(value * 100).quantize(Decimal(1).scaleb(-places))}%"
