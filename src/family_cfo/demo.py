"""Synthetic demo data (explicitly labelled, opt-in).

Never runs automatically and refuses to touch a ledger that already has
transactions or assets.  Every description is prefixed with ``演示`` so demo
rows can never be mistaken for real household data.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from .assets.accounts import AccountService
from .assets.service import AssetService
from .budgets.service import BudgetService
from .core.config import Settings
from .core.constants import TxnStatus
from .core.dates import add_months, today_local
from .core.errors import ValidationError
from .core.money import to_minor
from .database.base import Database
from .database.init import get_household
from .database.models import (
    Asset,
    FinancialGoal,
    InvestmentProduct,
    Transaction,
    TransactionCategory,
)
from .investments.service import InvestmentService
from .liabilities.service import LiabilityService
from .planning.service import PlanningService
from .transactions.reconcile import Reconciler
from .transactions.service import TransactionService

DEMO_PREFIX = "演示"


def _categories(session) -> dict[str, str]:
    return {
        c.slug: c.id
        for c in session.scalars(select(TransactionCategory))
    }


def seed_demo(db: Database, settings: Settings, *, actor: str = "demo") -> dict:
    with db.session() as session:
        household = get_household(session)
        if session.scalar(select(Transaction).limit(1)) is not None:
            raise ValidationError("台账已有交易，拒绝写入示例数据。请使用空数据库或 --reset。")
        if session.scalar(select(Asset).limit(1)) is not None:
            raise ValidationError("台账已有资产，拒绝写入示例数据。")

        cats = _categories(session)
        tsvc = TransactionService(session, settings)
        accounts = AccountService(session, settings)
        bank = accounts.get("银行卡")
        wechat = accounts.get("微信零钱")
        alipay = accounts.get("支付宝余额")

        today = today_local(settings.timezone)
        current = f"{today.year:04d}-{today.month:02d}"
        months = [add_months(current, -2), add_months(current, -1), current]

        def add(
            when: str,
            amount: str,
            direction: str,
            txn_type: str,
            account,
            category: str | None,
            counterparty: str,
            *,
            external: str,
            description: str | None = None,
            status: str = TxnStatus.CONFIRMED,
            reason: str | None = None,
        ):
            return tsvc.create(
                account_id=account.id,
                occurred_at=when,
                direction=direction,
                amount_minor=to_minor(amount, settings.currency),
                txn_type=txn_type,
                category_id=cats.get(category) if category else None,
                counterparty=f"{DEMO_PREFIX}-{counterparty}",
                description=description or f"{DEMO_PREFIX}数据",
                source_platform="demo",
                external_id=external,
                status=status,
                review_reason=reason,
                notes="合成示例数据，可安全删除",
            ).transaction

        seq = 0

        def next_external() -> str:
            nonlocal seq
            seq += 1
            return f"demo-{seq:04d}"

        for month in months:
            add(f"{month}-01 09:00:00", "25000", "inflow", "income", bank, "salary",
                "示例公司", external=next_external(), description=f"{DEMO_PREFIX}-工资")
            add(f"{month}-03 09:00:00", "6800", "outflow", "expense", bank, "housing",
                "示例房东", external=next_external(), description=f"{DEMO_PREFIX}-房租")
            add(f"{month}-06 12:00:00", "1200", "outflow", "expense", wechat, "food",
                "示例餐厅", external=next_external(), description=f"{DEMO_PREFIX}-餐饮")
            add(f"{month}-09 18:00:00", "300", "outflow", "expense", alipay, "transport",
                "示例地铁", external=next_external(), description=f"{DEMO_PREFIX}-交通")
            add(f"{month}-12 20:00:00", "800", "outflow", "expense", alipay, "education",
                "示例书店", external=next_external(), description=f"{DEMO_PREFIX}-课程")
            add(f"{month}-18 20:00:00", "260", "outflow", "expense", wechat, "utilities",
                "示例电力", external=next_external(), description=f"{DEMO_PREFIX}-电费")

        # Refund pair in the latest month
        add(f"{current}-20 10:00:00", "399", "outflow", "expense", wechat, "shopping",
            "示例商城", external=next_external(), description=f"{DEMO_PREFIX}-购物")
        add(f"{current}-22 10:00:00", "399", "inflow", "refund", wechat, None,
            "示例商城", external=next_external(), description=f"{DEMO_PREFIX}-退款")

        # Internal transfer: bank -> WeChat wallet
        add(f"{current}-15 09:00:00", "2000", "outflow", "transfer", bank, None,
            "本人微信", external=next_external(), description=f"{DEMO_PREFIX}-内部转账")
        add(f"{current}-15 09:05:00", "2000", "inflow", "topup", wechat, None,
            "零钱充值", external=next_external(), description=f"{DEMO_PREFIX}-充值")

        # Credit-card repayment (one side only; stays non-flow)
        add(f"{current}-25 09:00:00", "3000", "outflow", "credit_card_repayment", bank, None,
            "示例信用卡", external=next_external(), description=f"{DEMO_PREFIX}-信用卡还款")

        accounts.set_balance("银行卡", to_minor("52000", settings.currency), as_of=today, source="demo")
        accounts.set_balance("微信零钱", to_minor("1500", settings.currency), as_of=today, source="demo")
        accounts.set_balance("支付宝余额", to_minor("2200", settings.currency), as_of=today, source="demo")
        accounts.set_balance("信用卡", to_minor("3000", settings.currency), as_of=today, source="demo")

        assets = AssetService(session, settings)
        assets.add(
            name=f"{DEMO_PREFIX}-自住房", asset_type="property", value="2000000",
            acquisition_cost="1600000", is_primary_residence=True,
            valued_on=today, valuation_source="demo",
        )
        assets.add(
            name=f"{DEMO_PREFIX}-货币基金", asset_type="deposit", value="50000",
            acquisition_cost="50000", is_liquid=True,
            valued_on=today, valuation_source="demo",
        )

        liability = LiabilityService(session, settings).add(
            name=f"{DEMO_PREFIX}-房贷", liability_type="mortgage", principal="1000000",
            balance="800000", annual_rate="0.049", remaining_months=240,
            monthly_payment="6500", next_payment_date=today + timedelta(days=10),
        )

        BudgetService(session, settings).set_budget(
            period=current,
            items=[
                {"category": "food", "amount": "2000"},
                {"category": "education", "amount": "1000"},
                {"category": "housing", "amount": "7000"},
            ],
            name=f"{DEMO_PREFIX}-本月预算",
        )

        PlanningService(session, settings).add_goal(
            name=f"{DEMO_PREFIX}-教育金", goal_type="education", target="200000",
            current="50000", target_date=date(today.year + 4, 9, 1), monthly_saving="3000",
            assumptions="示例假设，仅用于演示",
        )

        investments = InvestmentService(session, settings)
        product = investments.add_product(
            symbol="DEMO001", name=f"{DEMO_PREFIX}-指数基金", product_type="fund"
        )
        investments.record(
            product_ref=product.id, txn_type="buy", trade_date=today - timedelta(days=30),
            quantity="1000", price="1.2", fee="1.2", note=f"{DEMO_PREFIX}数据",
        )
        investments.revalue(product.id, market_value="1300", valued_on=today, source="demo")

        Reconciler(session, settings, period=current).run()

        counts = {
            "transactions": len(list(session.scalars(select(Transaction)))),
            "assets": 2,
            "liabilities": 1,
            "budget_version": 1,
            "goals": len(
                list(
                    session.scalars(
                        select(FinancialGoal).where(
                            FinancialGoal.household_id == household.id
                        )
                    )
                )
            ),
            "investment_products": len(list(session.scalars(select(InvestmentProduct)))),
        }
        return {
            "demo": True,
            "warning": "以下全部为合成示例数据，可与真实账本区分（对方/说明均以“演示”开头）。",
            "periods": months,
            "counts": counts,
            "liability_id": liability.id,
        }
