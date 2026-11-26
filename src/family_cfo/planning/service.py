"""Family financial goals and long-term planning.

Planning output always separates:

* ``actual`` — measured from the ledger
* ``assumption`` — user-supplied rate / inflation / target, clearly labelled
* ``estimate`` — derived from actual + assumption
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dateutil.relativedelta import relativedelta
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import GoalStatus, GoalType
from ..core.dates import today_local
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.money import format_money, pct, to_minor
from ..database.audit import record_audit
from ..database.init import get_household
from ..database.models import FinancialGoal
from ..finance.metrics import average_monthly_expense, balance_sheet


@dataclass
class GoalPlan:
    goal_id: str
    name: str
    goal_type: str
    target_minor: int
    current_minor: int
    target_date: date | None
    months_remaining: int | None
    gap_minor: int
    required_monthly_minor: int | None
    planned_monthly_minor: int | None
    on_track: bool | None
    status: str
    risk_tolerance: str
    assumptions: list[str] = field(default_factory=list)

    def as_dict(self, currency: str) -> dict:
        return {
            "goal_id": self.goal_id,
            "name": self.name,
            "type": self.goal_type,
            "target": format_money(self.target_minor, currency),
            "current": format_money(self.current_minor, currency),
            "target_minor": self.target_minor,
            "current_minor": self.current_minor,
            "gap_minor": self.gap_minor,
            "progress": pct(self.current_minor, self.target_minor),
            "gap": format_money(self.gap_minor, currency),
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "months_remaining": self.months_remaining,
            "required_monthly": format_money(self.required_monthly_minor, currency)
            if self.required_monthly_minor is not None
            else None,
            "planned_monthly": format_money(self.planned_monthly_minor, currency)
            if self.planned_monthly_minor is not None
            else None,
            "on_track": self.on_track,
            "risk_tolerance": self.risk_tolerance,
            "assumptions": self.assumptions,
            "data_status": "actual" if self.target_date else "incomplete",
        }


class PlanningService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id

    def add_goal(
        self,
        *,
        name: str,
        target: str | int,
        goal_type: str = GoalType.WEALTH,
        current: str | int = 0,
        target_date: date | None = None,
        monthly_saving: str | int | None = None,
        priority: int = 100,
        risk_tolerance: str = "moderate",
        assumptions: str | None = None,
        actor: str = "cli",
    ) -> FinancialGoal:
        if goal_type not in {t.value for t in GoalType}:
            raise ValidationError(f"未知目标类型: {goal_type}")
        target_minor = to_minor(target, self.settings.currency)
        if target_minor <= 0:
            raise ValidationError("目标金额必须大于 0")
        current_minor = to_minor(current, self.settings.currency)
        if current_minor < 0:
            raise ValidationError("当前金额不能为负")
        goal = FinancialGoal(
            id=new_id("goal"),
            household_id=self.household_id,
            name=name.strip(),
            goal_type=goal_type,
            target_minor=target_minor,
            current_minor=current_minor,
            target_date=target_date,
            monthly_saving_minor=to_minor(monthly_saving, self.settings.currency)
            if monthly_saving is not None
            else None,
            priority=priority,
            risk_tolerance=risk_tolerance,
            assumptions=assumptions,
        )
        self.session.add(goal)
        self.session.flush()
        record_audit(
            self.session,
            household_id=self.household_id,
            action="create",
            entity_type="financial_goal",
            entity_id=goal.id,
            after={"name": goal.name, "target_minor": target_minor},
            actor=actor,
        )
        return goal

    def get(self, ref: str) -> FinancialGoal:
        goal = self.session.get(FinancialGoal, ref)
        if goal is None:
            goal = self.session.scalar(
                select(FinancialGoal).where(
                    FinancialGoal.household_id == self.household_id,
                    FinancialGoal.name == ref,
                )
            )
        if goal is None:
            raise ValidationError(f"财务目标不存在: {ref}")
        return goal

    def set_progress(self, ref: str, *, current: str | int, actor: str = "cli") -> FinancialGoal:
        goal = self.get(ref)
        before = {"current_minor": goal.current_minor}
        goal.current_minor = to_minor(current, self.settings.currency)
        if goal.current_minor >= goal.target_minor:
            goal.status = GoalStatus.ACHIEVED
        record_audit(
            self.session,
            household_id=self.household_id,
            action="update",
            entity_type="financial_goal",
            entity_id=goal.id,
            before=before,
            after={"current_minor": goal.current_minor},
            detail="progress",
            actor=actor,
        )
        return goal

    def list(self, *, status: str | None = "active") -> list[FinancialGoal]:
        stmt = select(FinancialGoal).where(FinancialGoal.household_id == self.household_id)
        if status:
            stmt = stmt.where(FinancialGoal.status == status)
        return list(self.session.scalars(stmt.order_by(FinancialGoal.priority.asc())))

    def plan(self, *, as_of: date | None = None, annual_return: Decimal | str | None = None) -> dict:
        as_of = as_of or today_local(self.settings.timezone)
        goals = [g for g in self.list(status=None) if g.status in {GoalStatus.ACTIVE, GoalStatus.ACHIEVED}]
        plans = [self._plan_goal(g, as_of, annual_return) for g in goals]
        emergency = self._emergency_plan(as_of)
        return {
            "as_of": as_of.isoformat(),
            "currency": self.settings.currency,
            "goals": [p.as_dict(self.settings.currency) for p in plans],
            "emergency_fund": emergency,
            "assumptions": [
                item
                for plan in plans
                for item in plan.assumptions
            ],
            "disclaimer": "规划结果基于当前台账数据与明确标注的假设，不构成投资建议。",
        }

    def _plan_goal(
        self, goal: FinancialGoal, as_of: date, annual_return: Decimal | str | None
    ) -> GoalPlan:
        gap = max(goal.target_minor - goal.current_minor, 0)
        months: int | None = None
        assumptions: list[str] = []
        if goal.target_date:
            if goal.target_date <= as_of:
                months = 0
            else:
                delta = relativedelta(goal.target_date, as_of)
                months = delta.years * 12 + delta.months + (1 if delta.days > 0 else 0)
        required: int | None = None
        if months and months > 0 and gap > 0:
            if annual_return is not None:
                rate = Decimal(str(annual_return))
                if rate > 1:
                    rate = rate / 100
                monthly_rate = rate / 12
                assumptions.append(f"按年化 {rate * 100:.2f}% 测算（用户假设，非承诺收益）")
                if monthly_rate == 0:
                    required = int(Decimal(gap) / months)
                else:
                    factor = (1 + monthly_rate) ** months - 1
                    required = int(
                        (Decimal(gap) * monthly_rate / factor).to_integral_value(
                            rounding=ROUND_HALF_UP
                        )
                    )
            else:
                required = int(Decimal(gap) / months)
                assumptions.append("未设置预期收益率，按不计收益的线性储蓄测算")
        on_track = None
        if required is not None and goal.monthly_saving_minor is not None:
            on_track = goal.monthly_saving_minor >= required
        if goal.assumptions:
            assumptions.append(f"用户备注: {goal.assumptions}")
        return GoalPlan(
            goal_id=goal.id,
            name=goal.name,
            goal_type=goal.goal_type,
            target_minor=goal.target_minor,
            current_minor=goal.current_minor,
            target_date=goal.target_date,
            months_remaining=months,
            gap_minor=gap,
            required_monthly_minor=required,
            planned_monthly_minor=goal.monthly_saving_minor,
            on_track=on_track,
            status=goal.status,
            risk_tolerance=goal.risk_tolerance,
            assumptions=assumptions,
        )

    def _emergency_plan(self, as_of: date) -> dict:
        period = f"{as_of.year:04d}-{as_of.month:02d}"
        avg, months_used = average_monthly_expense(
            self.session, self.household_id, period=period, months=3, essential=True
        )
        sheet = balance_sheet(self.session, self.household_id, as_of=as_of, settings=self.settings)
        covered = (
            Decimal(sheet.liquid_assets_minor) / Decimal(avg)
            if avg > 0 and sheet.has_data
            else None
        )
        targets = {}
        for months in (3, 6):
            target = avg * months
            targets[f"{months}_months"] = {
                "target": format_money(target, self.settings.currency),
                "gap": format_money(max(target - sheet.liquid_assets_minor, 0), self.settings.currency),
                "covered": covered is not None and covered >= months,
            }
        return {
            "liquid_assets": format_money(sheet.liquid_assets_minor, self.settings.currency),
            "essential_monthly_expense": format_money(avg, self.settings.currency),
            "average_over_months": months_used,
            "covered_months": f"{covered:.1f}" if covered is not None else None,
            "targets": targets,
            "status": "actual" if (avg > 0 and sheet.has_data) else "unavailable",
            "note": (
                None
                if (avg > 0 and sheet.has_data)
                else ("尚未录入流动资产" if avg > 0 else "缺少必要支出数据，无法评估应急资金")
            ),
        }

    def required_monthly(
        self,
        *,
        target: str | int,
        current: str | int = 0,
        months: int,
        annual_return: Decimal | str = "0",
    ) -> dict:
        if months <= 0:
            raise ValidationError("months 必须大于 0")
        gap = to_minor(target, self.settings.currency) - to_minor(current, self.settings.currency)
        gap = max(gap, 0)
        rate = Decimal(str(annual_return))
        monthly_rate = rate / 12
        if monthly_rate == 0:
            required = int(Decimal(gap) / months)
        else:
            factor = (1 + monthly_rate) ** months - 1
            required = int(
                (Decimal(gap) * monthly_rate / factor).to_integral_value(rounding=ROUND_HALF_UP)
            )
        return {
            "gap": format_money(gap, self.settings.currency),
            "months": months,
            "required_monthly": format_money(required, self.settings.currency),
            "annual_return_assumption": str(rate),
            "note": "预期收益率是用户假设，不是保证收益。",
        }
