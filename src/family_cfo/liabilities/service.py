"""Liability register, payments and amortisation schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import AuditAction, LiabilityType
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.money import format_money, to_minor
from ..database.audit import record_audit
from ..database.init import get_household
from ..database.models import FamilyMember, Liability, LiabilityPayment


@dataclass
class ScheduleRow:
    period_index: int
    payment_minor: int
    principal_minor: int
    interest_minor: int
    balance_minor: int

    def as_dict(self, currency: str) -> dict:
        return {
            "period": self.period_index,
            "payment": format_money(self.payment_minor, currency),
            "principal": format_money(self.principal_minor, currency),
            "interest": format_money(self.interest_minor, currency),
            "balance": format_money(self.balance_minor, currency),
        }


class LiabilityService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id

    def add(
        self,
        *,
        name: str,
        liability_type: str,
        principal: str | int,
        balance: str | int | None = None,
        annual_rate: Decimal | str = "0",
        repayment_method: str = "equal_installment",
        term_months: int | None = None,
        remaining_months: int | None = None,
        monthly_payment: str | int | None = None,
        next_payment_date: date | None = None,
        member_id: str | None = None,
        currency: str | None = None,
        notes: str | None = None,
        actor: str = "cli",
    ) -> Liability:
        if liability_type not in {t.value for t in LiabilityType}:
            raise ValidationError(f"未知负债类型: {liability_type}")
        currency = (currency or self.settings.currency).upper()
        principal_minor = to_minor(principal, currency)
        balance_minor = to_minor(balance, currency) if balance is not None else principal_minor
        if principal_minor < 0 or balance_minor < 0:
            raise ValidationError("负债金额不能为负")
        rate = Decimal(str(annual_rate))
        if rate < 0:
            raise ValidationError("利率不能为负")
        if rate > 1:
            rate = rate / 100
        if member_id:
            member = self.session.get(FamilyMember, member_id)
            if member is None or member.household_id != self.household_id:
                raise ValidationError(f"家庭成员不存在: {member_id}")
        payment_minor = (
            to_minor(monthly_payment, currency)
            if monthly_payment is not None
            else self._equal_installment(balance_minor, rate, remaining_months or term_months or 0)
        )
        liability = Liability(
            id=new_id("liability"),
            household_id=self.household_id,
            member_id=member_id,
            name=name.strip(),
            liability_type=liability_type,
            currency=currency,
            principal_minor=principal_minor,
            balance_minor=balance_minor,
            annual_rate_bps=int((rate * 10000).to_integral_value()),
            repayment_method=repayment_method,
            repayment_cycle="monthly",
            next_payment_date=next_payment_date,
            term_months=term_months,
            remaining_months=remaining_months,
            monthly_payment_minor=payment_minor,
            notes=notes,
        )
        self.session.add(liability)
        self.session.flush()
        record_audit(
            self.session,
            household_id=self.household_id,
            action=AuditAction.CREATE,
            entity_type="liability",
            entity_id=liability.id,
            after={"name": liability.name, "balance_minor": balance_minor},
            actor=actor,
        )
        return liability

    def get(self, ref: str) -> Liability:
        liability = self.session.get(Liability, ref)
        if liability is None:
            liability = self.session.scalar(
                select(Liability).where(
                    Liability.household_id == self.household_id, Liability.name == ref
                )
            )
        if liability is None:
            raise ValidationError(f"负债不存在: {ref}")
        return liability

    def update_balance(
        self, ref: str, *, balance: str | int, reason: str, actor: str = "cli"
    ) -> Liability:
        liability = self.get(ref)
        if not reason.strip():
            raise ValidationError("调整负债余额必须填写原因（审计要求）")
        before = {"balance_minor": liability.balance_minor}
        new_balance = to_minor(balance, liability.currency)
        if new_balance < 0:
            raise ValidationError("负债余额不能为负")
        liability.balance_minor = new_balance
        if liability.remaining_months:
            liability.remaining_months = max(liability.remaining_months - 1, 0)
        record_audit(
            self.session,
            household_id=self.household_id,
            action=AuditAction.UPDATE,
            entity_type="liability",
            entity_id=liability.id,
            before=before,
            after={"balance_minor": new_balance, "reason": reason},
            detail="balance_update",
            actor=actor,
        )
        return liability

    def record_payment(
        self,
        ref: str,
        *,
        paid_on: date,
        principal: str | int,
        interest: str | int = 0,
        total: str | int | None = None,
        note: str | None = None,
        actor: str = "cli",
    ) -> LiabilityPayment:
        liability = self.get(ref)
        principal_minor = to_minor(principal, liability.currency)
        interest_minor = to_minor(interest, liability.currency)
        total_minor = (
            to_minor(total, liability.currency)
            if total is not None
            else principal_minor + interest_minor
        )
        if principal_minor < 0 or interest_minor < 0:
            raise ValidationError("还款金额不能为负")
        if principal_minor > liability.balance_minor:
            raise ValidationError("本金还款额超过当前余额")
        before = {"balance_minor": liability.balance_minor}
        payment = LiabilityPayment(
            id=new_id("payment"),
            liability_id=liability.id,
            paid_on=paid_on,
            principal_minor=principal_minor,
            interest_minor=interest_minor,
            total_minor=total_minor,
            note=note,
        )
        self.session.add(payment)
        liability.balance_minor -= principal_minor
        if liability.remaining_months is not None:
            liability.remaining_months = max(liability.remaining_months - 1, 0)
        if liability.next_payment_date is not None:
            next_date = liability.next_payment_date
            month = next_date.month % 12 + 1
            year = next_date.year + (1 if next_date.month == 12 else 0)
            try:
                liability.next_payment_date = next_date.replace(year=year, month=month)
            except ValueError:
                liability.next_payment_date = date(year, month, 28)
        record_audit(
            self.session,
            household_id=self.household_id,
            action=AuditAction.UPDATE,
            entity_type="liability",
            entity_id=liability.id,
            before=before,
            after={"balance_minor": liability.balance_minor, "payment_minor": total_minor},
            detail="repayment",
            actor=actor,
        )
        self.session.flush()
        return payment

    def amortization(
        self, ref: str, *, months: int | None = None, extra_principal: str | int = 0
    ) -> list[ScheduleRow]:
        liability = self.get(ref)
        balance = liability.balance_minor - to_minor(extra_principal, liability.currency)
        if balance < 0:
            raise ValidationError("提前还款金额超过余额")
        rate = Decimal(liability.annual_rate_bps) / 10000 / 12
        total_periods = liability.remaining_months or liability.term_months
        if not total_periods:
            raise ValidationError("缺少剩余期限，请提供 --months")
        rows_to_show = min(months or total_periods, total_periods)
        rows: list[ScheduleRow] = []
        for index in range(1, rows_to_show + 1):
            interest = int((Decimal(balance) * rate).to_integral_value(rounding=ROUND_HALF_UP))
            if liability.repayment_method == "equal_principal":
                periods_left = total_periods - index + 1
                principal = int(Decimal(balance) / Decimal(periods_left))
            else:
                payment = self._equal_installment(balance, rate, total_periods - index + 1)
                principal = max(payment - interest, 0)
            principal = min(principal, balance)
            payment_total = principal + interest
            balance -= principal
            rows.append(
                ScheduleRow(
                    period_index=index,
                    payment_minor=payment_total,
                    principal_minor=principal,
                    interest_minor=interest,
                    balance_minor=balance,
                )
            )
            if balance <= 0:
                break
        return rows

    def list(self, *, include_inactive: bool = False) -> list[Liability]:
        stmt = select(Liability).where(Liability.household_id == self.household_id)
        if not include_inactive:
            stmt = stmt.where(Liability.is_active.is_(True))
        return list(self.session.scalars(stmt.order_by(Liability.created_at.asc())))

    def payments(self, ref: str) -> list[LiabilityPayment]:
        liability = self.get(ref)
        return list(
            self.session.scalars(
                select(LiabilityPayment)
                .where(LiabilityPayment.liability_id == liability.id)
                .order_by(LiabilityPayment.paid_on.asc())
            )
        )

    def summary(self) -> dict:
        all_liabilities = self.list()
        liabilities = [l for l in all_liabilities if l.currency == self.settings.currency]
        excluded = sorted(
            {l.currency for l in all_liabilities} - {self.settings.currency}
        )
        ids = {l.id for l in liabilities}
        total = sum(l.balance_minor for l in liabilities)
        monthly = sum(l.monthly_payment_minor for l in liabilities)
        interest_paid = sum(
            p.interest_minor
            for p in self.session.scalars(select(LiabilityPayment))
            if p.liability_id in ids
        )
        weighted_rate = (
            sum(Decimal(l.balance_minor) * Decimal(l.annual_rate_bps) for l in liabilities)
            / Decimal(total)
            / 10000
            if total
            else None
        )
        by_type: dict[str, int] = {}
        for liability in liabilities:
            by_type[liability.liability_type] = (
                by_type.get(liability.liability_type, 0) + liability.balance_minor
            )
        return {
            "total_minor": total,
            "monthly_payment_minor": monthly,
            "interest_paid_minor": interest_paid,
            "interest_paid": format_money(interest_paid, self.settings.currency),
            "excluded_currencies": excluded,
            "weighted_rate": str(weighted_rate) if weighted_rate is not None else None,
            "by_type": {
                k: {"amount": format_money(v, self.settings.currency), "amount_minor": v}
                for k, v in by_type.items()
            },
            "items": [
                {
                    "id": l.id,
                    "name": l.name,
                    "type": l.liability_type,
                    "balance": format_money(l.balance_minor, l.currency),
                    "balance_minor": l.balance_minor,
                    "rate": str(Decimal(l.annual_rate_bps) / 10000),
                    "monthly_payment": format_money(l.monthly_payment_minor, l.currency),
                    "next_payment_date": l.next_payment_date.isoformat() if l.next_payment_date else None,
                    "remaining_months": l.remaining_months,
                }
                for l in liabilities
            ],
        }

    @staticmethod
    def _equal_installment(balance_minor: int, monthly_rate: Decimal, months: int) -> int:
        if months <= 0:
            return 0
        if monthly_rate == 0:
            return int(Decimal(balance_minor) / Decimal(months))
        factor = (1 + monthly_rate) ** months
        payment = Decimal(balance_minor) * monthly_rate * factor / (factor - 1)
        return int(payment.to_integral_value(rounding=ROUND_HALF_UP))
