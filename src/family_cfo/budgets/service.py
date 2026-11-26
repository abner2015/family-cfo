"""Budget management with immutable version history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import BudgetPeriod, BudgetStatus, TxnStatus, TxnType
from ..core.dates import month_bounds, month_start, parse_period
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.money import format_money, ratio, to_minor
from ..database.audit import record_audit
from ..database.init import get_household
from ..database.models import (
    Budget,
    BudgetItem,
    FamilyMember,
    Transaction,
    TransactionCategory,
)


@dataclass
class BudgetLine:
    category_id: str | None
    category: str
    member_id: str | None
    member: str | None
    item_type: str
    budget_minor: int
    actual_minor: int

    @property
    def variance_minor(self) -> int:
        return self.budget_minor - self.actual_minor

    @property
    def execution_rate(self) -> Decimal | None:
        return ratio(self.actual_minor, self.budget_minor)

    @property
    def over_budget(self) -> bool:
        return self.actual_minor > self.budget_minor

    def as_dict(self, currency: str = "CNY") -> dict:
        return {
            "category": self.category,
            "category_id": self.category_id,
            "member": self.member,
            "item_type": self.item_type,
            "budget": format_money(self.budget_minor, currency),
            "actual": format_money(self.actual_minor, currency),
            "variance": format_money(self.variance_minor, currency),
            "budget_minor": self.budget_minor,
            "actual_minor": self.actual_minor,
            "variance_minor": self.variance_minor,
            "execution_rate": str(self.execution_rate) if self.execution_rate is not None else None,
            "over_budget": self.over_budget,
        }


@dataclass
class BudgetReport:
    budget_id: str
    period: str
    version: int
    name: str
    total_budget_minor: int
    total_actual_minor: int
    lines: list[BudgetLine] = field(default_factory=list)
    unbudgeted_minor: int = 0
    currency: str = "CNY"

    @property
    def execution_rate(self) -> Decimal | None:
        return ratio(self.total_actual_minor, self.total_budget_minor)

    @property
    def variance_minor(self) -> int:
        return self.total_budget_minor - self.total_actual_minor

    def as_dict(self) -> dict:
        return {
            "budget_id": self.budget_id,
            "period": self.period,
            "version": self.version,
            "name": self.name,
            "total_budget": format_money(self.total_budget_minor, self.currency),
            "total_actual": format_money(self.total_actual_minor, self.currency),
            "variance": format_money(self.variance_minor, self.currency),
            "execution_rate": str(self.execution_rate) if self.execution_rate is not None else None,
            "unbudgeted": format_money(self.unbudgeted_minor, self.currency),
            "lines": [line.as_dict(self.currency) for line in self.lines],
            "warnings": [
                f"{line.category} 超支 {format_money(-line.variance_minor, self.currency)}"
                for line in self.lines
                if line.over_budget
            ],
        }


class BudgetService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id

    # ------------------------------------------------------------------ create
    def set_budget(
        self,
        *,
        period: str | None = None,
        year: int | None = None,
        items: list[dict],
        name: str | None = None,
        actor: str = "cli",
    ) -> Budget:
        if year is not None:
            period_type = BudgetPeriod.ANNUAL
            start, end = date(year, 1, 1), date(year, 12, 31)
            label = f"{year}"
        else:
            period = parse_period(period)
            start, end = month_bounds(period)
            period_type = BudgetPeriod.MONTHLY
            label = period
        if not items:
            raise ValidationError("预算至少需要一个预算项")
        parsed_items = [self._validate_item(item) for item in items]
        previous = self._active(period_type, start)
        version = 1
        if previous is not None:
            version = previous.version + 1
            previous.status = BudgetStatus.SUPERSEDED
        total = sum(i["amount_minor"] for i in parsed_items)
        budget = Budget(
            id=new_id("budget"),
            household_id=self.household_id,
            name=name or f"{label} 家庭预算",
            period_type=period_type,
            period_start=start,
            period_end=end,
            version=version,
            status=BudgetStatus.ACTIVE,
            total_amount_minor=total,
            currency=self.settings.currency,
        )
        self.session.add(budget)
        self.session.flush()
        for item in parsed_items:
            self.session.add(
                BudgetItem(
                    id=new_id("budget_item"),
                    budget_id=budget.id,
                    category_id=item["category_id"],
                    member_id=item["member_id"],
                    item_type=item["item_type"],
                    amount_minor=item["amount_minor"],
                    notes=item.get("notes"),
                )
            )
        record_audit(
            self.session,
            household_id=self.household_id,
            action="create",
            entity_type="budget",
            entity_id=budget.id,
            before={"previous_version": previous.version} if previous else None,
            after={
                "period_type": period_type,
                "period_start": str(start),
                "version": version,
                "total_minor": total,
            },
            detail="budget_version",
            actor=actor,
        )
        self.session.flush()
        return budget

    def _validate_item(self, item: dict) -> dict:
        if "amount" in item:
            amount_minor = to_minor(item["amount"], self.settings.currency)
        elif "amount_minor" in item:
            amount_minor = int(item["amount_minor"])
        else:
            raise ValidationError("预算项缺少金额")
        if amount_minor <= 0:
            raise ValidationError("预算金额必须大于 0")
        category_id = None
        if item.get("category"):
            category = self._find_category(item["category"])
            category_id = category.id
        member_id = None
        if item.get("member"):
            member = self._find_member(item["member"])
            member_id = member.id
        item_type = item.get("item_type", "variable")
        if item_type not in {"fixed", "variable"}:
            raise ValidationError("item_type 只能是 fixed 或 variable")
        return {
            "amount_minor": amount_minor,
            "category_id": category_id,
            "member_id": member_id,
            "item_type": item_type,
            "notes": item.get("notes"),
        }

    def _find_category(self, ref: str) -> TransactionCategory:
        category = self.session.get(TransactionCategory, ref)
        if category is None:
            category = self.session.scalar(
                select(TransactionCategory).where(
                    (TransactionCategory.slug == ref) | (TransactionCategory.name == ref),
                    (TransactionCategory.household_id == self.household_id)
                    | (TransactionCategory.household_id.is_(None)),
                )
            )
        if category is None:
            raise ValidationError(f"分类不存在: {ref}")
        return category

    def _find_member(self, ref: str) -> FamilyMember:
        member = self.session.get(FamilyMember, ref)
        if member is None:
            member = self.session.scalar(
                select(FamilyMember).where(
                    FamilyMember.household_id == self.household_id, FamilyMember.name == ref
                )
            )
        if member is None:
            raise ValidationError(f"家庭成员不存在: {ref}")
        return member

    def _active(self, period_type: str, start: date) -> Budget | None:
        return self.session.scalar(
            select(Budget).where(
                Budget.household_id == self.household_id,
                Budget.period_type == period_type,
                Budget.period_start == start,
                Budget.status == BudgetStatus.ACTIVE,
            )
        )

    # -------------------------------------------------------------------- read
    def active(self, period: str) -> Budget | None:
        return self._active(BudgetPeriod.MONTHLY, month_start(period))

    def active_year(self, year: int) -> Budget | None:
        return self._active(BudgetPeriod.ANNUAL, date(year, 1, 1))

    def history(self, period: str) -> list[Budget]:
        start = month_start(period)
        return list(
            self.session.scalars(
                select(Budget)
                .where(
                    Budget.household_id == self.household_id,
                    Budget.period_type == BudgetPeriod.MONTHLY,
                    Budget.period_start == start,
                )
                .order_by(Budget.version.asc())
            )
        )

    def execution(self, period: str) -> BudgetReport | list[dict]:
        budget = self.active(period)
        if budget is None:
            return []
        report = self._execution_for(budget, period)
        return report

    def _execution_for(self, budget: Budget, period: str) -> BudgetReport:
        start, end = month_bounds(period)
        txns = list(
            self.session.scalars(
                select(Transaction).where(
                    Transaction.household_id == self.household_id,
                    Transaction.occurred_at >= datetime.combine(start, datetime.min.time()),
                    Transaction.occurred_at
                    <= datetime.combine(end, datetime.max.time().replace(microsecond=0)),
                    Transaction.is_deleted.is_(False),
                    Transaction.status != TxnStatus.VOID,
                    Transaction.stats_included.is_(True),
                    Transaction.txn_type.in_([TxnType.EXPENSE, TxnType.FEE, TxnType.REVERSAL]),
                    Transaction.direction == "outflow",
                )
            )
        )
        actual_by_category: dict[str, int] = {}
        for txn in txns:
            if txn.category_id:
                actual_by_category[txn.category_id] = (
                    actual_by_category.get(txn.category_id, 0) + txn.amount_base_minor
                )
        items = list(
            self.session.scalars(
                select(BudgetItem).where(BudgetItem.budget_id == budget.id)
            )
        )
        lines: list[BudgetLine] = []
        budgeted_categories: set[str] = set()
        for item in items:
            category = (
                self.session.get(TransactionCategory, item.category_id) if item.category_id else None
            )
            member = self.session.get(FamilyMember, item.member_id) if item.member_id else None
            actual = 0
            if item.category_id:
                budgeted_categories.add(item.category_id)
                scope = [
                    t
                    for t in txns
                    if t.category_id == item.category_id
                    and (item.member_id is None or t.member_id == item.member_id)
                ]
                actual = sum(t.amount_base_minor for t in scope)
            elif item.member_id:
                scope = [t for t in txns if t.member_id == item.member_id]
                actual = sum(t.amount_base_minor for t in scope)
            else:
                actual = sum(t.amount_base_minor for t in txns)
            lines.append(
                BudgetLine(
                    category_id=item.category_id,
                    category=category.name if category else "未指定分类",
                    member_id=item.member_id,
                    member=member.name if member else None,
                    item_type=item.item_type,
                    budget_minor=item.amount_minor,
                    actual_minor=actual,
                )
            )
        total_actual = sum(t.amount_base_minor for t in txns)
        budgeted_actual = sum(actual_by_category.get(c, 0) for c in budgeted_categories)
        report = BudgetReport(
            budget_id=budget.id,
            period=period,
            version=budget.version,
            name=budget.name,
            total_budget_minor=budget.total_amount_minor,
            total_actual_minor=total_actual,
            lines=lines,
            unbudgeted_minor=max(total_actual - budgeted_actual, 0),
            currency=budget.currency,
        )
        return report
