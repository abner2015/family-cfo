"""Rule-first transaction classification.

The classifier never invents a category.  No confident match means the
transaction is flagged ``pending_review`` with reason ``uncategorized`` and the
user (or the Skill, after asking) can correct it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.constants import (
    EXPENSE_TYPES,
    NON_FLOW_TYPES,
    REVIEW_UNCATEGORIZED,
    CategoryKind,
    RuleMatchType,
    TxnStatus,
    TxnType,
)
from ..database.models import CategoryRule, Transaction, TransactionCategory

INTERNAL_TYPE_CATEGORY = {
    TxnType.TRANSFER: "internal_transfer",
    TxnType.CREDIT_CARD_REPAYMENT: "credit_card_repayment",
    TxnType.TOPUP: "internal_transfer",
    TxnType.WITHDRAWAL: "internal_transfer",
    TxnType.INVESTMENT_BUY: "internal_transfer",
    TxnType.INVESTMENT_SELL: "internal_transfer",
}


@dataclass
class Classification:
    category_id: str | None
    confidence: int  # basis points 0..10000
    reason: str
    category_slug: str | None = None


def _text_of(txn: Transaction) -> str:
    return " ".join(
        part for part in (txn.counterparty, txn.description, txn.notes) if part
    ).lower()


class RuleClassifier:
    def __init__(self, session: Session, household_id: str) -> None:
        self.session = session
        self.household_id = household_id
        self.rules = list(
            session.scalars(
                select(CategoryRule)
                .where(
                    CategoryRule.household_id == household_id,
                    CategoryRule.is_active.is_(True),
                )
                .order_by(CategoryRule.priority.asc(), CategoryRule.created_at.asc())
            )
        )
        self.categories = {
            c.id: c
            for c in session.scalars(
                select(TransactionCategory).where(
                    (TransactionCategory.household_id == household_id)
                    | (TransactionCategory.household_id.is_(None))
                )
            )
        }
        self.by_slug = {c.slug: c for c in self.categories.values()}

    # ------------------------------------------------------------------ public
    def classify(self, txn: Transaction) -> Classification:
        if txn.category_id:
            return Classification(txn.category_id, 10000, "already_categorized")

        if txn.txn_type in INTERNAL_TYPE_CATEGORY:
            slug = INTERNAL_TYPE_CATEGORY[txn.txn_type]
            cat = self.by_slug.get(slug)
            if cat is not None:
                return Classification(cat.id, 10000, "internal_flow", cat.slug)

        for rule in self.rules:
            if not self._rule_applies(rule, txn):
                continue
            category = self.categories.get(rule.category_id)
            if category is None:
                continue
            rule.hit_count += 1
            return Classification(category.id, 8000, f"rule:{rule.id}", category.slug)

        keyword_match = self._keyword_match(txn)
        if keyword_match is not None:
            category, keyword = keyword_match
            return Classification(category.id, 6000, f"keyword:{keyword}", category.slug)

        if txn.txn_type in NON_FLOW_TYPES:
            return Classification(None, 0, "internal_flow_uncategorized")
        return Classification(None, 0, "no_match")

    def apply(self, txn: Transaction) -> Classification:
        result = self.classify(txn)
        if result.category_id:
            txn.category_id = result.category_id
        elif txn.txn_type in EXPENSE_TYPES or txn.txn_type in {TxnType.INCOME, TxnType.REFUND}:
            txn.status = TxnStatus.PENDING_REVIEW
            txn.review_reason = txn.review_reason or REVIEW_UNCATEGORIZED
        return result

    # ----------------------------------------------------------------- helpers
    def _rule_applies(self, rule: CategoryRule, txn: Transaction) -> bool:
        if rule.direction and rule.direction != txn.direction:
            return False
        if rule.txn_type and rule.txn_type != txn.txn_type:
            return False
        if rule.source_platform and rule.source_platform != txn.source_platform:
            return False
        if rule.min_amount_minor is not None and txn.amount_minor < rule.min_amount_minor:
            return False
        if rule.max_amount_minor is not None and txn.amount_minor > rule.max_amount_minor:
            return False
        text = _text_of(txn)
        if rule.match_type == RuleMatchType.KEYWORD:
            return rule.pattern.strip().lower() in text
        if rule.match_type == RuleMatchType.COUNTERPARTY:
            return rule.pattern.strip().lower() == (txn.counterparty or "").strip().lower()
        if rule.match_type == RuleMatchType.REGEX:
            try:
                return re.search(rule.pattern, text) is not None
            except re.error:
                return False
        if rule.match_type == RuleMatchType.AMOUNT_RANGE:
            # amount_range is fully described by min/max columns
            return True
        return False

    def _keyword_match(self, txn: Transaction) -> tuple[TransactionCategory, str] | None:
        text = _text_of(txn)
        if not text:
            return None
        if txn.txn_type in EXPENSE_TYPES or txn.txn_type in {TxnType.INCOME, TxnType.REFUND}:
            wanted_kind = (
                CategoryKind.INCOME if txn.direction == "inflow" else CategoryKind.EXPENSE
            )
        else:
            wanted_kind = CategoryKind.TRANSFER
        candidates = [
            c
            for c in self.categories.values()
            if c.kind == wanted_kind and c.keywords
        ]
        candidates.sort(key=lambda c: c.sort_order)
        best: tuple[TransactionCategory, str, int] | None = None
        for category in candidates:
            try:
                keywords = json.loads(category.keywords or "[]")
            except json.JSONDecodeError:
                continue
            for keyword in keywords:
                kw = str(keyword).strip().lower()
                if kw and kw in text:
                    score = len(kw) + (10000 - category.sort_order) / 10000
                    if best is None or score > best[2]:
                        best = (category, kw, score)
        if best is None:
            return None
        return best[0], best[1]

    def category_options(self, kind: str | None = None) -> list[dict]:
        cats = sorted(
            [c for c in self.categories.values() if kind is None or c.kind == kind],
            key=lambda c: (c.kind, c.sort_order, c.name),
        )
        return [{"id": c.id, "slug": c.slug, "name": c.name, "kind": c.kind} for c in cats]
