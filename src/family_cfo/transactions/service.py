"""Transactional CRUD for ledger entries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import (
    Direction,
    REVIEW_DUPLICATE,
    REVIEW_UNCATEGORIZED,
    TxnStatus,
    TxnType,
)
from ..core.dates import parse_datetime
from ..core.errors import ValidationError
from ..core.ids import new_id, stable_key
from ..core.money import from_minor
from ..database.audit import record_audit
from ..database.models import Transaction, TransactionCategory
from ..database.init import get_household
from .classify import RuleClassifier

_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = _WS_RE.sub(" ", value.strip().lower())
    return _NON_WORD_RE.sub("", text)


def compute_dedupe_key(
    *,
    household_id: str,
    direction: str,
    amount_minor: int,
    currency: str,
    occurred_at: datetime,
    counterparty: str | None,
    description: str | None,
) -> str:
    anchor = normalize_text(counterparty) or normalize_text(description)[:24]
    return stable_key(
        household_id,
        direction,
        amount_minor,
        currency,
        occurred_at.date().isoformat(),
        anchor,
    )


@dataclass
class CreateResult:
    transaction: Transaction
    duplicate: bool = False
    duplicate_kind: str | None = None


class TransactionService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id

    # ------------------------------------------------------------------ create
    def _default_member_id(self) -> str | None:
        from ..database.models import FamilyMember

        member = self.session.scalar(
            select(FamilyMember)
            .where(
                FamilyMember.household_id == self.household_id,
                FamilyMember.is_active.is_(True),
            )
            .order_by(FamilyMember.created_at.asc())
            .limit(1)
        )
        return member.id if member else None

    def create(
        self,
        *,
        account_id: str,
        occurred_at: datetime | str,
        direction: str,
        amount_minor: int,
        txn_type: str = TxnType.EXPENSE,
        currency: str | None = None,
        external_id: str | None = None,
        counterparty: str | None = None,
        description: str | None = None,
        source_platform: str = "manual",
        source_batch_id: str | None = None,
        status: str = TxnStatus.CONFIRMED,
        review_reason: str | None = None,
        category_id: str | None = None,
        member_id: str | None = None,
        payer_member_id: str | None = None,
        owner_member_id: str | None = None,
        beneficiary: str | None = None,
        is_shared: bool = True,
        notes: str | None = None,
        fx_rate: Decimal | str | None = None,
        audit: bool = False,
    ) -> CreateResult:
        if amount_minor < 0:
            raise ValidationError("金额必须是非负整数（分）")
        if direction not in {Direction.INFLOW, Direction.OUTFLOW}:
            raise ValidationError(f"非法方向: {direction}")
        if txn_type not in {t.value for t in TxnType}:
            raise ValidationError(f"非法交易类型: {txn_type}")
        txn_currency = (currency or self.settings.currency).upper()
        if member_id is None:
            member_id = self._default_member_id()
        occurred = (
            parse_datetime(occurred_at) if not isinstance(occurred_at, datetime) else occurred_at
        )
        if external_id:
            existing = self.session.scalar(
                select(Transaction).where(
                    Transaction.source_platform == source_platform,
                    Transaction.external_id == external_id,
                )
            )
            if existing is not None:
                return CreateResult(existing, duplicate=True, duplicate_kind="external_id")

        amount_base = amount_minor
        fx_decimal: Decimal | None = None
        stats_included = True
        if txn_currency != self.settings.currency:
            if fx_rate is None:
                status = TxnStatus.PENDING_REVIEW
                review_reason = review_reason or "fx_missing"
                amount_base = 0
                stats_included = False
            else:
                fx_decimal = Decimal(str(fx_rate))
                if fx_decimal <= 0:
                    raise ValidationError("汇率必须大于 0")
                amount_base = int(
                    (Decimal(amount_minor) * fx_decimal).to_integral_value()
                )

        key = compute_dedupe_key(
            household_id=self.household_id,
            direction=direction,
            amount_minor=amount_minor,
            currency=txn_currency,
            occurred_at=occurred,
            counterparty=counterparty,
            description=description,
        )
        window = timedelta(days=1)
        candidate = self.session.scalar(
            select(Transaction)
            .where(
                Transaction.household_id == self.household_id,
                Transaction.dedupe_key == key,
                Transaction.source_platform == source_platform,
                Transaction.is_deleted.is_(False),
                Transaction.occurred_at >= occurred - window,
                Transaction.occurred_at <= occurred + window,
            )
            .order_by(Transaction.occurred_at.asc())
            .limit(1)
        )
        duplicate = False
        if candidate is not None and candidate.external_id != external_id:
            duplicate = True
            status = TxnStatus.PENDING_REVIEW
            review_reason = review_reason or REVIEW_DUPLICATE
            stats_included = False

        txn = Transaction(
            id=new_id("txn"),
            household_id=self.household_id,
            account_id=account_id,
            external_id=external_id,
            source_platform=source_platform,
            source_batch_id=source_batch_id,
            occurred_at=occurred,
            direction=direction,
            txn_type=txn_type,
            amount_minor=amount_minor,
            currency=txn_currency,
            amount_base_minor=amount_base,
            fx_rate=fx_decimal,
            counterparty=counterparty,
            description=description,
            category_id=category_id,
            member_id=member_id,
            payer_member_id=payer_member_id,
            owner_member_id=owner_member_id,
            beneficiary=beneficiary,
            is_shared=is_shared,
            status=status,
            stats_included=stats_included,
            review_reason=review_reason,
            dedupe_key=key,
            duplicate_of_id=candidate.id if duplicate else None,
            notes=notes,
        )
        self.session.add(txn)
        self.session.flush()
        if audit:
            record_audit(
                self.session,
                household_id=self.household_id,
                action="create",
                entity_type="transaction",
                entity_id=txn.id,
                before=None,
                after=self.to_dict(txn),
                detail=f"manual entry via {source_platform}",
            )
        return CreateResult(txn, duplicate=duplicate, duplicate_kind="dedupe_key" if duplicate else None)

    # -------------------------------------------------------------------- read
    def get(self, txn_id: str) -> Transaction:
        txn = self.session.get(Transaction, txn_id)
        if txn is None or txn.is_deleted:
            raise ValidationError(f"交易不存在或已删除: {txn_id}")
        return txn

    def list(
        self,
        *,
        period: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
        account_id: str | None = None,
        category_id: str | None = None,
        only_review: bool = False,
    ) -> list[Transaction]:
        stmt = select(Transaction).where(Transaction.household_id == self.household_id)
        if not include_deleted:
            stmt = stmt.where(Transaction.is_deleted.is_(False))
        if period:
            if start or end:
                raise ValidationError("period 与 start/end 不能同时使用")
            from ..core.dates import month_end, month_start

            start = datetime.combine(month_start(period), datetime.min.time())
            end = datetime.combine(month_end(period), datetime.max.time().replace(microsecond=0))
        if start:
            stmt = stmt.where(Transaction.occurred_at >= start)
        if end:
            stmt = stmt.where(Transaction.occurred_at <= end)
        if status:
            stmt = stmt.where(Transaction.status == status)
        if only_review:
            stmt = stmt.where(Transaction.status == TxnStatus.PENDING_REVIEW)
        if account_id:
            stmt = stmt.where(Transaction.account_id == account_id)
        if category_id:
            stmt = stmt.where(Transaction.category_id == category_id)
        stmt = stmt.order_by(Transaction.occurred_at.asc(), Transaction.created_at.asc())
        if limit:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def to_dict(self, txn: Transaction) -> dict:
        category = self.session.get(TransactionCategory, txn.category_id) if txn.category_id else None
        return {
            "id": txn.id,
            "occurred_at": txn.occurred_at.isoformat(sep=" "),
            "direction": txn.direction,
            "txn_type": txn.txn_type,
            "amount": str(from_minor(txn.amount_minor, txn.currency)),
            "amount_minor": txn.amount_minor,
            "amount_base_minor": txn.amount_base_minor,
            "currency": txn.currency,
            "counterparty": txn.counterparty,
            "description": txn.description,
            "category": category.name if category else None,
            "category_id": txn.category_id,
            "account_id": txn.account_id,
            "source_platform": txn.source_platform,
            "status": txn.status,
            "review_reason": txn.review_reason,
            "is_shared": txn.is_shared,
            "notes": txn.notes,
        }

    # ------------------------------------------------------------------ mutate
    def categorize(
        self,
        txn_id: str,
        category_id: str,
        *,
        create_rule: bool = False,
        actor: str = "cli",
    ) -> Transaction:
        txn = self.get(txn_id)
        category = self.session.get(TransactionCategory, category_id)
        if category is None:
            raise ValidationError(f"分类不存在: {category_id}")
        before = self.to_dict(txn)
        txn.category_id = category.id
        if txn.status == TxnStatus.PENDING_REVIEW and txn.review_reason == REVIEW_UNCATEGORIZED:
            txn.status = TxnStatus.CONFIRMED
            txn.review_reason = None
        txn.stats_included = True
        if create_rule:
            self.add_rule(
                pattern=(txn.counterparty or txn.description or "")[:500],
                category_id=category.id,
                priority=50,
                created_by=actor,
                match_type="keyword",
            )
        record_audit(
            self.session,
            household_id=self.household_id,
            action="update",
            entity_type="transaction",
            entity_id=txn.id,
            before=before,
            after=self.to_dict(txn),
            detail=f"categorize:{category.slug}",
        )
        self.session.flush()
        return txn

    def update_note(self, txn_id: str, note: str, *, actor: str = "cli") -> Transaction:
        txn = self.get(txn_id)
        before = self.to_dict(txn)
        txn.notes = note
        record_audit(
            self.session,
            household_id=self.household_id,
            action="update",
            entity_type="transaction",
            entity_id=txn.id,
            before=before,
            after=self.to_dict(txn),
            detail="note",
        )
        return txn

    def soft_delete(self, txn_id: str, *, reason: str, actor: str = "cli") -> Transaction:
        if not reason.strip():
            raise ValidationError("删除交易必须填写原因（审计要求）")
        txn = self.get(txn_id)
        before = self.to_dict(txn)
        txn.is_deleted = True
        txn.stats_included = False
        txn.status = TxnStatus.VOID
        record_audit(
            self.session,
            household_id=self.household_id,
            action="soft_delete",
            entity_type="transaction",
            entity_id=txn.id,
            before=before,
            after=self.to_dict(txn),
            detail=reason,
        )
        return txn

    def restore(self, txn_id: str, *, reason: str = "restore", actor: str = "cli") -> Transaction:
        txn = self.session.get(Transaction, txn_id)
        if txn is None:
            raise ValidationError(f"交易不存在: {txn_id}")
        before = self.to_dict(txn)
        txn.is_deleted = False
        txn.status = TxnStatus.CONFIRMED
        txn.stats_included = True
        record_audit(
            self.session,
            household_id=self.household_id,
            action="restore",
            entity_type="transaction",
            entity_id=txn.id,
            before=before,
            after=self.to_dict(txn),
            detail=reason,
        )
        return txn

    # ------------------------------------------------------------------- rules
    def create_category(
        self,
        *,
        name: str,
        kind: str = "expense",
        parent_id: str | None = None,
        is_essential: bool = False,
        keywords: list[str] | None = None,
        sort_order: int = 500,
    ) -> TransactionCategory:
        from ..core.constants import CategoryKind
        from ..core.ids import stable_key

        if kind not in {k.value for k in CategoryKind}:
            raise ValidationError(f"非法分类类型: {kind}")
        name = name.strip()
        if not name:
            raise ValidationError("分类名称不能为空")
        existing = self.session.scalar(
            select(TransactionCategory).where(
                (TransactionCategory.household_id == self.household_id)
                | (TransactionCategory.household_id.is_(None)),
                TransactionCategory.name == name,
                TransactionCategory.kind == kind,
            )
        )
        if existing is not None:
            raise ValidationError(f"分类已存在: {name}")
        if parent_id:
            parent = self.session.get(TransactionCategory, parent_id)
            if parent is None:
                raise ValidationError(f"父分类不存在: {parent_id}")
        category = TransactionCategory(
            id=new_id("category"),
            slug="custom_" + stable_key(self.household_id, kind, name)[:12],
            household_id=self.household_id,
            name=name,
            kind=kind,
            parent_id=parent_id,
            is_system=False,
            is_essential=is_essential,
            sort_order=sort_order,
            keywords=json.dumps(keywords or [name], ensure_ascii=False),
        )
        self.session.add(category)
        self.session.flush()
        record_audit(
            self.session,
            household_id=self.household_id,
            action="create",
            entity_type="transaction_category",
            entity_id=category.id,
            after={"name": category.name, "kind": kind, "keywords": keywords or [name]},
        )
        return category

    def list_categories(self, *, kind: str | None = None) -> list[TransactionCategory]:
        stmt = select(TransactionCategory).where(
            (TransactionCategory.household_id == self.household_id)
            | (TransactionCategory.household_id.is_(None))
        )
        if kind:
            stmt = stmt.where(TransactionCategory.kind == kind)
        return list(
            self.session.scalars(
                stmt.order_by(TransactionCategory.kind.asc(), TransactionCategory.sort_order.asc())
            )
        )

    def add_rule(
        self,
        *,
        pattern: str,
        category_id: str,
        priority: int = 50,
        match_type: str = "keyword",
        created_by: str = "user",
        direction: str | None = None,
        txn_type: str | None = None,
        source_platform: str | None = None,
        min_amount_minor: int | None = None,
        max_amount_minor: int | None = None,
    ):
        from ..database.models import CategoryRule

        pattern = pattern.strip()
        if not pattern:
            raise ValidationError("分类规则不能为空")
        rule = CategoryRule(
            id=new_id("rule"),
            household_id=self.household_id,
            category_id=category_id,
            priority=priority,
            match_type=match_type,
            pattern=pattern,
            direction=direction,
            txn_type=txn_type,
            source_platform=source_platform,
            min_amount_minor=min_amount_minor,
            max_amount_minor=max_amount_minor,
            created_by=created_by,
        )
        self.session.add(rule)
        self.session.flush()
        return rule

    def reclassify_pending(self) -> int:
        classifier = RuleClassifier(self.session, self.household_id)
        changed = 0
        for txn in self.list(status=TxnStatus.PENDING_REVIEW):
            if txn.review_reason not in {None, REVIEW_UNCATEGORIZED}:
                continue
            result = classifier.apply(txn)
            if result.category_id:
                changed += 1
        return changed
