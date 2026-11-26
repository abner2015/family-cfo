"""Household financial health diagnosis.

Deterministic rules over real ledger data.  Missing inputs yield
``unavailable`` findings and are excluded from the score — the diagnosis never
substitutes zero for unknown data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.dates import month_end, previous_period, today_local
from ..core.money import pct, ratio
from ..database.init import get_household
from ..database.models import Liability, Transaction, TxnStatus
from ..investments.service import InvestmentService
from .metrics import average_monthly_expense, balance_sheet, period_summary

GOOD = "good"
WATCH = "watch"
RISK = "risk"
UNAVAILABLE = "unavailable"

STATUS_LABEL = {
    GOOD: "良好",
    WATCH: "关注",
    RISK: "风险",
    UNAVAILABLE: "数据不足",
}


@dataclass
class Finding:
    key: str
    label: str
    value: str | None
    raw: str | None
    status: str
    threshold: str
    formula: str
    sources: list[str]
    suggestion: str | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "raw": self.raw,
            "status": self.status,
            "status_label": STATUS_LABEL[self.status],
            "threshold": self.threshold,
            "formula": self.formula,
            "sources": self.sources,
            "suggestion": self.suggestion,
        }


@dataclass
class HealthReport:
    period: str
    as_of: date
    score: int | None
    grade: str
    findings: list[Finding] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "as_of": self.as_of.isoformat(),
            "score": self.score,
            "grade": self.grade,
            "findings": [f.as_dict() for f in self.findings],
            "unavailable": self.unavailable,
            "disclaimer": "健康诊断基于当前台账与明确阈值，不构成投资或信贷建议。",
        }


def _score(findings: list[Finding]) -> tuple[int | None, str]:
    weights = {
        "emergency_months": 25,
        "savings_rate": 20,
        "debt_ratio": 20,
        "debt_service_ratio": 15,
        "liquid_ratio": 10,
        "investment_concentration": 10,
    }
    points = {GOOD: 100, WATCH: 60, RISK: 20}
    total_weight = 0
    total = 0
    for finding in findings:
        if finding.status == UNAVAILABLE:
            continue
        weight = weights.get(finding.key, 5)
        total_weight += weight
        total += weight * points[finding.status]
    if total_weight == 0:
        return None, STATUS_LABEL[UNAVAILABLE]
    score = round(total / total_weight)
    if score >= 80:
        grade = "良好"
    elif score >= 60:
        grade = "稳健"
    elif score >= 40:
        grade = "需关注"
    else:
        grade = "高风险"
    return score, grade


def diagnose(session: Session, settings: Settings, *, period: str, as_of: date | None = None) -> HealthReport:
    household_id = get_household(session).id
    as_of = as_of or today_local(settings.timezone)
    summary = period_summary(session, household_id, period, currency=settings.currency)
    sheet = balance_sheet(session, household_id, as_of=as_of, settings=settings)

    avg_essential, months_used = average_monthly_expense(
        session, household_id, period=period, months=3, essential=True
    )
    income_values: list[int] = []
    for offset in range(1, 4):
        monthly = period_summary(session, household_id, previous_period(period, offset))
        if monthly.income_minor > 0:
            income_values.append(monthly.income_minor)
    if not income_values and summary.income_minor > 0:
        income_values.append(summary.income_minor)
    avg_income = sum(income_values) // len(income_values) if income_values else 0

    liabilities = [
        l
        for l in session.scalars(
            select(Liability).where(
                Liability.household_id == household_id, Liability.is_active.is_(True)
            )
        )
        if l.currency == settings.currency
    ]
    monthly_debt = sum(l.monthly_payment_minor for l in liabilities)

    findings: list[Finding] = []

    # 1. Emergency fund
    emergency = (
        Decimal(sheet.liquid_assets_minor) / Decimal(avg_essential)
        if avg_essential > 0 and sheet.has_data
        else None
    )
    if emergency is None:
        findings.append(
            Finding(
                "emergency_months", "应急资金覆盖", None, None, UNAVAILABLE,
                "建议 ≥ 6 个月", "流动资产 / 近3个月平均必要支出",
                ["accounts", "assets", "transactions"],
                "先录入账户余额/流动资产，并导入至少一个月支出账单。",
            )
        )
    else:
        status = GOOD if emergency >= 6 else WATCH if emergency >= 3 else RISK
        findings.append(
            Finding(
                "emergency_months", "应急资金覆盖", f"{emergency:.1f} 个月", f"{emergency:.2f}",
                status, "≥6 良好 / ≥3 关注 / <3 风险",
                "流动资产 / 近3个月平均必要支出",
                ["accounts", "assets", "transactions"],
                None if status != RISK else "优先补足 3–6 个月必要支出到高流动性账户。",
            )
        )

    # 2. Savings rate
    savings = summary.savings_rate
    if savings is None:
        findings.append(
            Finding(
                "savings_rate", "储蓄率", None, None, UNAVAILABLE,
                "建议 ≥ 20%", "月度结余 / 月度收入", ["transactions"],
                "导入收入账单后再评估储蓄率。",
            )
        )
    else:
        status = GOOD if savings >= Decimal("0.2") else WATCH if savings >= Decimal("0.05") else RISK
        findings.append(
            Finding(
                "savings_rate", "储蓄率", pct(summary.net_minor, summary.income_minor),
                str(savings), status, "≥20% 良好 / ≥5% 关注 / <5% 风险",
                "月度结余 / 月度收入", ["transactions"],
                None if status != RISK else "检查大额非必要支出，设定分类预算。",
            )
        )

    # 3. Debt / assets
    if not sheet.has_data or not sheet.has_liability_data:
        findings.append(
            Finding(
                "debt_ratio", "资产负债率", None, None, UNAVAILABLE,
                "建议 ≤ 30%", "总负债 / 总资产", ["liabilities", "assets"],
                "录入资产与负债后评估杠杆水平。",
            )
        )
    else:
        debt_ratio = sheet.debt_ratio
        if debt_ratio is None:
            findings.append(
                Finding(
                    "debt_ratio", "资产负债率", None, None, UNAVAILABLE,
                    "≤30% 良好 / ≤50% 关注 / >50% 风险",
                    "总负债 / 总资产", ["liabilities", "assets"],
                    "总资产为 0，无法评估杠杆。",
                )
            )
        else:
            status = GOOD if debt_ratio <= Decimal("0.3") else WATCH if debt_ratio <= Decimal("0.5") else RISK
            findings.append(
                Finding(
                    "debt_ratio", "资产负债率",
                    pct(sheet.liabilities_minor, sheet.total_assets_minor),
                    str(debt_ratio),
                    status, "≤30% 良好 / ≤50% 关注 / >50% 风险",
                    "总负债 / 总资产", ["liabilities", "assets"],
                    None if status != RISK else "优先偿还高息负债，避免新增消费贷。",
                )
            )

    # 4. Debt service ratio
    if not sheet.has_liability_data:
        findings.append(
            Finding(
                "debt_service_ratio", "债务负担率", None, None, UNAVAILABLE,
                "≤30% 良好 / ≤50% 关注 / >50% 风险",
                "月还款额 / 近3个月平均月收入", ["liabilities", "transactions"],
                "录入负债与月供后再评估。",
            )
        )
    elif monthly_debt == 0:
        findings.append(
            Finding(
                "debt_service_ratio", "债务负担率", "0.0%", "0", GOOD,
                "≤30% 良好 / ≤50% 关注 / >50% 风险",
                "月还款额 / 近3个月平均月收入", ["liabilities", "transactions"],
                None,
            )
        )
    elif avg_income <= 0:
        findings.append(
            Finding(
                "debt_service_ratio", "债务负担率", None, None, UNAVAILABLE,
                "≤30% 良好 / ≤50% 关注 / >50% 风险",
                "月还款额 / 近3个月平均月收入", ["liabilities", "transactions"],
                "缺少收入数据，无法评估月供压力。",
            )
        )
    else:
        dsr = ratio(monthly_debt, avg_income)
        status = GOOD if dsr <= Decimal("0.3") else WATCH if dsr <= Decimal("0.5") else RISK
        findings.append(
            Finding(
                "debt_service_ratio", "债务负担率",
                pct(monthly_debt, avg_income), str(dsr), status,
                "≤30% 良好 / ≤50% 关注 / >50% 风险",
                "月还款额 / 近3个月平均月收入", ["liabilities", "transactions"],
                None if status != RISK else "月供已超过收入一半，考虑延长期限或重组债务。",
            )
        )

    # 5. Liquidity
    if not sheet.has_data:
        findings.append(
            Finding(
                "liquid_ratio", "流动资产比例", None, None, UNAVAILABLE,
                "建议 ≥ 20%", "流动资产 / 总资产", ["accounts", "assets"],
                "录入资产后评估流动性。",
            )
        )
    else:
        liquid_ratio = sheet.liquid_ratio
        if liquid_ratio is None:
            findings.append(
                Finding(
                    "liquid_ratio", "流动资产比例", None, None, UNAVAILABLE,
                    "≥20% 良好 / ≥10% 关注 / <10% 风险",
                    "流动资产 / 总资产（自住房不计）", ["accounts", "assets"],
                    "总资产为 0，无法评估流动性。",
                )
            )
        else:
            status = (
                GOOD if liquid_ratio >= Decimal("0.2") else WATCH if liquid_ratio >= Decimal("0.1") else RISK
            )
            findings.append(
                Finding(
                    "liquid_ratio", "流动资产比例",
                    pct(sheet.liquid_assets_minor, sheet.total_assets_minor),
                    str(liquid_ratio), status, "≥20% 良好 / ≥10% 关注 / <10% 风险",
                    "流动资产 / 总资产（自住房不计）", ["accounts", "assets"],
                    None if status != RISK else "减少低流动性资产占比，保留应急现金。",
                )
            )

    # 6. Investment concentration
    portfolio = InvestmentService(session, settings).portfolio()
    included_positions = [p for p in portfolio.positions if p.currency == settings.currency]
    total_investment = portfolio.market_value_minor
    if total_investment <= 0:
        findings.append(
            Finding(
                "investment_concentration", "单一持仓集中度", None, None, UNAVAILABLE,
                "建议 ≤ 50%", "最大持仓市值 / 投资总市值",
                ["investment_positions"],
                "无投资持仓或未提供估值。",
            )
        )
    else:
        largest = max(
            (
                position.market_value_minor
                if position.market_value_minor is not None
                else position.cost_minor
            )
            for position in included_positions
        )
        concentration = ratio(largest, total_investment)
        status = GOOD if concentration <= Decimal("0.5") else WATCH if concentration <= Decimal("0.7") else RISK
        findings.append(
            Finding(
                "investment_concentration", "单一持仓集中度",
                pct(largest, total_investment), str(concentration), status,
                "≤50% 良好 / ≤70% 关注 / >70% 风险",
                "最大持仓市值 / 投资总市值", ["investment_positions"],
                None if status != RISK else "分散到不同资产类别，避免单一产品风险。",
            )
        )

    # Data quality (not scored)
    pending = session.execute(
        select(Transaction)
        .where(
            Transaction.household_id == household_id,
            Transaction.occurred_at
            >= datetime.combine(date.fromisoformat(f"{period}-01"), time.min),
            Transaction.occurred_at
            <= datetime.combine(
                month_end(period), time.max.replace(microsecond=0)
            ),
            Transaction.status == TxnStatus.PENDING_REVIEW,
            Transaction.is_deleted.is_(False),
        )
        .limit(1)
    ).first()
    if pending is not None:
        findings.append(
            Finding(
                "pending_review", "待确认交易", "存在", None, WATCH,
                "0 笔", "status=pending_review 的交易", ["transactions"],
                "先处理待确认交易，诊断结果才完整。",
            )
        )

    unavailable = [f.label for f in findings if f.status == UNAVAILABLE]
    score, grade = _score([f for f in findings if f.key != "pending_review"])
    return HealthReport(
        period=period,
        as_of=as_of,
        score=score,
        grade=grade,
        findings=findings,
        unavailable=unavailable,
    )
