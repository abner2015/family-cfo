"""Natural-language entry (a supplement, never the primary import path).

Parses a short Chinese sentence into a *proposed* transaction.  The CLI shows
the proposal and requires confirmation unless ``--yes`` is passed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import CategoryKind, Direction, TxnType
from ..core.dates import now_local, parse_datetime
from ..core.money import format_money, to_minor
from ..database.models import TransactionCategory

AMOUNT_RE = re.compile(
    r"(?:(?:¥|￥|RMB)\s*)?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(?:元|块|rmb)?",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
INCOME_HINTS = ("收入", "工资", "奖金", "报销", "收到", "进账", "分红", "利息", "退款")
EXPENSE_HINTS = ("支出", "花了", "消费", "买了", "付款", "付了")
TRANSFER_HINTS = ("转账", "还款", "充值", "提现", "还信用卡")


@dataclass
class ParsedEntry:
    text: str
    amount_minor: int
    direction: str
    txn_type: str
    occurred_at: datetime
    description: str
    category_id: str | None = None
    category_name: str | None = None
    confidence: int = 0  # basis points
    warnings: list[str] = field(default_factory=list)

    def as_dict(self, currency: str = "CNY") -> dict:
        return {
            "text": self.text,
            "amount": format_money(self.amount_minor, currency),
            "amount_minor": self.amount_minor,
            "direction": self.direction,
            "txn_type": self.txn_type,
            "occurred_at": self.occurred_at.isoformat(sep=" "),
            "description": self.description,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "confidence": self.confidence,
            "warnings": self.warnings,
        }


def parse_entry(text: str, session: Session, settings: Settings) -> ParsedEntry:
    text = text.strip()
    if not text:
        raise ValueError("记账内容为空")
    amount_match = None
    for match in AMOUNT_RE.finditer(text):
        # skip pure dates / times
        start = match.start()
        if start > 0 and text[start - 1] in "-/:":
            continue
        amount_match = match
    if amount_match is None:
        raise ValueError("未能识别金额，请使用例如：午餐 35 元")
    amount = to_minor(Decimal(amount_match.group(1).replace(",", "")), settings.currency)
    if amount <= 0:
        raise ValueError("金额必须大于 0")

    direction = Direction.OUTFLOW
    txn_type = TxnType.EXPENSE
    warnings: list[str] = []
    if any(h in text for h in TRANSFER_HINTS):
        txn_type = TxnType.TRANSFER
        direction = Direction.OUTFLOW
        warnings.append("识别为转账/还款类，默认不计入收支")
    elif any(h in text for h in INCOME_HINTS):
        direction = Direction.INFLOW
        txn_type = TxnType.INCOME
    elif any(h in text for h in EXPENSE_HINTS):
        direction = Direction.OUTFLOW
        txn_type = TxnType.EXPENSE

    occurred_at = now_local(settings.timezone)
    date_match = DATE_RE.search(text)
    if date_match:
        occurred_at = parse_datetime(
            f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
        )
    elif "昨天" in text:
        occurred_at = (occurred_at - timedelta(days=1)).replace(hour=12, minute=0)
    elif "前天" in text:
        occurred_at = (occurred_at - timedelta(days=2)).replace(hour=12, minute=0)

    description = AMOUNT_RE.sub("", text)
    for word in ("今天", "昨天", "前天", "支出", "收入", "花了", "消费", "买了", "付款", "付了"):
        description = description.replace(word, "")
    description = re.sub(r"\s+", " ", description).strip(" ，,。;；")
    if not description:
        description = text

    category_id, category_name, confidence = _match_category(
        description, direction, txn_type, session, settings
    )
    confidence = min(confidence + (3000 if amount else 0), 9500)
    if category_id is None:
        warnings.append("未匹配到分类，保存后将进入待确认列表")
    return ParsedEntry(
        text=text,
        amount_minor=amount,
        direction=direction,
        txn_type=txn_type,
        occurred_at=occurred_at,
        description=description,
        category_id=category_id,
        category_name=category_name,
        confidence=confidence,
        warnings=warnings,
    )


def _match_category(
    text: str, direction: str, txn_type: str, session: Session, settings: Settings
) -> tuple[str | None, str | None, int]:
    if txn_type == TxnType.TRANSFER:
        kind = CategoryKind.TRANSFER
    elif direction == Direction.INFLOW:
        kind = CategoryKind.INCOME
    else:
        kind = CategoryKind.EXPENSE
    categories = list(
        session.scalars(
            select(TransactionCategory).where(
                TransactionCategory.kind == kind,
                (TransactionCategory.household_id.is_(None))
                | (TransactionCategory.household_id == _household_id(session)),
            )
        )
    )
    best: tuple[int, TransactionCategory, str] | None = None
    lowered = text.lower()
    for category in categories:
        try:
            keywords = json.loads(category.keywords or "[]")
        except json.JSONDecodeError:
            continue
        for keyword in keywords:
            kw = str(keyword).strip().lower()
            if kw and kw in lowered:
                score = len(kw)
                if best is None or score > best[0]:
                    best = (score, category, kw)
    if best is None:
        return None, None, 2000
    return best[1].id, best[1].name, 5000 + min(best[0] * 200, 2000)


def _household_id(session: Session) -> str:
    from ..database.init import get_household

    return get_household(session).id
