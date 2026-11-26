"""Investment products, trades, positions and P&L.

P&L is split into:

* cash flow: dividends received (never mixed into market value change)
* realised P&L: proceeds minus average cost on sells
* unrealised P&L: only when a user-supplied valuation exists — prices are
  never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import AuditAction, InvTxnType, ProductType
from ..core.dates import today_local
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.money import decimal_str, format_money, ratio, to_minor
from ..database.audit import record_audit
from ..database.init import get_household
from ..database.models import (
    Account,
    InvestmentPosition,
    InvestmentProduct,
    InvestmentTransaction,
)


@dataclass
class PositionView:
    product_id: str
    symbol: str
    name: str
    product_type: str
    currency: str
    quantity: Decimal
    avg_cost: Decimal
    cost_minor: int
    market_value_minor: int | None
    unrealized_minor: int | None
    realized_minor: int
    valuation_date: date | None
    valuation_source: str | None
    estimated: bool

    def as_dict(self, currency: str) -> dict:
        return {
            "product_id": self.product_id,
            "symbol": self.symbol,
            "name": self.name,
            "type": self.product_type,
            "quantity": decimal_str(self.quantity),
            "avg_cost": decimal_str(self.avg_cost, 6),
            "cost": format_money(self.cost_minor, currency),
            "market_value": format_money(self.market_value_minor, currency)
            if self.market_value_minor is not None
            else None,
            "unrealized": format_money(self.unrealized_minor, currency)
            if self.unrealized_minor is not None
            else None,
            "realized": format_money(self.realized_minor, currency),
            "valuation_date": self.valuation_date.isoformat() if self.valuation_date else None,
            "valuation_source": self.valuation_source,
            "estimated": self.estimated,
            "cost_minor": self.cost_minor,
            "market_value_minor": self.market_value_minor,
            "unrealized_minor": self.unrealized_minor,
            "realized_minor": self.realized_minor,
        }


@dataclass
class Portfolio:
    positions: list[PositionView] = field(default_factory=list)
    dividends_minor: int = 0
    fees_minor: int = 0
    currency: str = "CNY"

    @property
    def cost_minor(self) -> int:
        return sum(p.cost_minor for p in self.positions)

    @property
    def market_value_minor(self) -> int:
        return sum(
            p.market_value_minor if p.market_value_minor is not None else p.cost_minor
            for p in self.positions
        )

    @property
    def realized_minor(self) -> int:
        return sum(p.realized_minor for p in self.positions)

    @property
    def unrealized_minor(self) -> int | None:
        if any(p.market_value_minor is None for p in self.positions):
            return None
        return self.market_value_minor - self.cost_minor

    def as_dict(self) -> dict:
        allocation: dict[str, int] = {}
        for position in self.positions:
            value = (
                position.market_value_minor
                if position.market_value_minor is not None
                else position.cost_minor
            )
            allocation[position.product_type] = allocation.get(position.product_type, 0) + value
        total = self.market_value_minor
        return {
            "currency": self.currency,
            "cost": format_money(self.cost_minor, self.currency),
            "market_value": format_money(self.market_value_minor, self.currency),
            "realized_pnl": format_money(self.realized_minor, self.currency),
            "unrealized_pnl": format_money(self.unrealized_minor, self.currency)
            if self.unrealized_minor is not None
            else None,
            "dividends": format_money(self.dividends_minor, self.currency),
            "fees": format_money(self.fees_minor, self.currency),
            "allocation": {
                k: {
                    "amount": format_money(v, self.currency),
                    "amount_minor": v,
                    "share": str(ratio(v, total)) if total else None,
                }
                for k, v in sorted(allocation.items(), key=lambda kv: kv[1], reverse=True)
            },
            "positions": [p.as_dict(self.currency) for p in self.positions],
            "estimated_positions": [p.name for p in self.positions if p.estimated],
        }


class InvestmentService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id

    def add_product(
        self,
        *,
        symbol: str,
        name: str,
        product_type: str = ProductType.FUND,
        currency: str | None = None,
        market: str | None = None,
        notes: str | None = None,
    ) -> InvestmentProduct:
        if product_type not in {t.value for t in ProductType}:
            raise ValidationError(f"未知产品类型: {product_type}")
        existing = self.session.scalar(
            select(InvestmentProduct).where(
                InvestmentProduct.household_id == self.household_id,
                InvestmentProduct.symbol == symbol,
                InvestmentProduct.product_type == product_type,
            )
        )
        if existing is not None:
            return existing
        product = InvestmentProduct(
            id=new_id("product"),
            household_id=self.household_id,
            symbol=symbol.strip(),
            name=name.strip(),
            product_type=product_type,
            currency=(currency or self.settings.currency).upper(),
            market=market,
            notes=notes,
        )
        self.session.add(product)
        self.session.flush()
        return product

    def get_product(self, ref: str) -> InvestmentProduct:
        product = self.session.get(InvestmentProduct, ref)
        if product is None:
            product = self.session.scalar(
                select(InvestmentProduct).where(
                    InvestmentProduct.household_id == self.household_id,
                    (InvestmentProduct.symbol == ref) | (InvestmentProduct.name == ref),
                )
            )
        if product is None:
            raise ValidationError(f"投资产品不存在: {ref}")
        return product

    def record(
        self,
        *,
        product_ref: str,
        txn_type: str,
        trade_date: date,
        quantity: Decimal | str = "0",
        price: Decimal | str = "0",
        amount: str | int | None = None,
        fee: str | int = 0,
        account_id: str | None = None,
        note: str | None = None,
        actor: str = "cli",
    ) -> InvestmentTransaction:
        if txn_type not in {t.value for t in InvTxnType}:
            raise ValidationError(f"未知交易类型: {txn_type}")
        product = self.get_product(product_ref)
        qty = Decimal(str(quantity))
        unit_price = Decimal(str(price))
        if qty < 0:
            raise ValidationError("数量不能为负（卖出也填正数）")
        if account_id:
            account = self.session.get(Account, account_id)
            if account is None or account.household_id != self.household_id:
                raise ValidationError(f"账户不存在: {account_id}")
        if amount is not None:
            gross = to_minor(amount, product.currency)
        else:
            gross = to_minor(qty * unit_price, product.currency)
        fee_minor = to_minor(fee, product.currency) if fee else 0
        net = gross + fee_minor if txn_type == InvTxnType.BUY else gross - fee_minor
        txn = InvestmentTransaction(
            id=new_id("inv_txn"),
            household_id=self.household_id,
            product_id=product.id,
            account_id=account_id,
            txn_type=txn_type,
            trade_date=trade_date,
            quantity=qty,
            price=unit_price,
            gross_minor=gross,
            fee_minor=fee_minor,
            net_minor=net,
            currency=product.currency,
            note=note,
        )
        self.session.add(txn)
        self.session.flush()
        position = self.recompute(product.id, account_id)
        record_audit(
            self.session,
            household_id=self.household_id,
            action=AuditAction.CREATE,
            entity_type="investment_transaction",
            entity_id=txn.id,
            after={
                "product": product.symbol,
                "type": txn_type,
                "quantity": decimal_str(qty),
                "gross_minor": gross,
                "fee_minor": fee_minor,
                "position_qty": decimal_str(position.quantity),
            },
            actor=actor,
        )
        return txn

    def recompute(self, product_id: str, account_id: str | None) -> InvestmentPosition:
        txns = list(
            self.session.scalars(
                select(InvestmentTransaction)
                .where(
                    InvestmentTransaction.product_id == product_id,
                    InvestmentTransaction.account_id == account_id,
                )
                .order_by(InvestmentTransaction.trade_date.asc(), InvestmentTransaction.created_at.asc())
            )
        )
        quantity = Decimal("0")
        cost = 0
        realized = 0
        for txn in txns:
            qty = Decimal(str(txn.quantity))
            if txn.txn_type == InvTxnType.BUY:
                if qty <= 0:
                    raise ValidationError("买入数量必须大于 0")
                quantity += qty
                cost += txn.net_minor
            elif txn.txn_type == InvTxnType.SELL:
                if qty <= 0:
                    raise ValidationError("卖出数量必须大于 0")
                if qty > quantity:
                    raise ValidationError(
                        f"卖出数量超过持仓: {decimal_str(qty)} > {decimal_str(quantity)}"
                    )
                avg = Decimal(cost) / quantity if quantity else Decimal("0")
                cost_out = int((avg * qty).to_integral_value(rounding=ROUND_HALF_UP))
                realized += txn.net_minor - cost_out
                quantity -= qty
                cost -= cost_out
            elif txn.txn_type == InvTxnType.DIVIDEND:
                realized += 0  # cash flow tracked separately
            elif txn.txn_type == InvTxnType.FEE:
                cost += txn.fee_minor
                realized -= txn.fee_minor
        position = self.session.scalar(
            select(InvestmentPosition).where(
                InvestmentPosition.product_id == product_id,
                InvestmentPosition.account_id == account_id,
            )
        )
        if position is None:
            position = InvestmentPosition(
                id=new_id("position"),
                household_id=self.household_id,
                product_id=product_id,
                account_id=account_id,
            )
            self.session.add(position)
        before_value = position.market_value_minor
        position.quantity = quantity
        position.cost_minor = max(cost, 0)
        position.avg_cost = (
            (Decimal(position.cost_minor) / quantity).quantize(Decimal("0.00000001"))
            if quantity > 0
            else Decimal("0")
        )
        position.realized_pnl_minor = realized
        if quantity == 0:
            position.market_value_minor = 0
            position.unrealized_pnl_minor = None
        elif before_value is not None:
            position.unrealized_pnl_minor = before_value - position.cost_minor
        self.session.flush()
        return position

    def revalue(
        self,
        product_ref: str,
        *,
        market_value: str | int,
        valued_on: date | None = None,
        source: str = "manual",
        account_id: str | None = None,
    ) -> InvestmentPosition:
        product = self.get_product(product_ref)
        position = self.session.scalar(
            select(InvestmentPosition).where(
                InvestmentPosition.product_id == product.id,
                InvestmentPosition.account_id == account_id,
            )
        )
        if position is None:
            raise ValidationError("该产品尚无持仓，无法估值")
        value_minor = (
            market_value if isinstance(market_value, int) else to_minor(market_value, product.currency)
        )
        if value_minor < 0:
            raise ValidationError("市值不能为负")
        position.market_value_minor = value_minor
        position.valuation_date = valued_on or today_local(self.settings.timezone)
        position.valuation_source = source
        position.unrealized_pnl_minor = value_minor - position.cost_minor
        record_audit(
            self.session,
            household_id=self.household_id,
            action=AuditAction.UPDATE,
            entity_type="investment_position",
            entity_id=position.id,
            after={"market_value_minor": value_minor, "source": source},
            detail="revaluation",
        )
        self.session.flush()
        return position

    def portfolio(self) -> Portfolio:
        portfolio = Portfolio(currency=self.settings.currency)
        for position in self.session.scalars(
            select(InvestmentPosition).where(
                InvestmentPosition.household_id == self.household_id
            )
        ):
            if position.quantity <= 0:
                continue
            product = self.session.get(InvestmentProduct, position.product_id)
            if product is None:
                continue
            estimated = position.market_value_minor is None or position.valuation_date is None
            portfolio.positions.append(
                PositionView(
                    product_id=product.id,
                    symbol=product.symbol,
                    name=product.name,
                    product_type=product.product_type,
                    currency=product.currency,
                    quantity=Decimal(str(position.quantity)),
                    avg_cost=Decimal(str(position.avg_cost)),
                    cost_minor=position.cost_minor,
                    market_value_minor=position.market_value_minor,
                    unrealized_minor=position.unrealized_pnl_minor,
                    realized_minor=position.realized_pnl_minor,
                    valuation_date=position.valuation_date,
                    valuation_source=position.valuation_source,
                    estimated=estimated,
                )
            )
        for txn in self.session.scalars(
            select(InvestmentTransaction).where(
                InvestmentTransaction.household_id == self.household_id
            )
        ):
            portfolio.fees_minor += txn.fee_minor
            if txn.txn_type == InvTxnType.DIVIDEND:
                portfolio.dividends_minor += txn.net_minor
        portfolio.positions.sort(key=lambda p: p.cost_minor, reverse=True)
        return portfolio

    def history(self, product_ref: str | None = None) -> list[InvestmentTransaction]:
        stmt = select(InvestmentTransaction).where(
            InvestmentTransaction.household_id == self.household_id
        )
        if product_ref:
            stmt = stmt.where(InvestmentTransaction.product_id == self.get_product(product_ref).id)
        return list(
            self.session.scalars(
                stmt.order_by(InvestmentTransaction.trade_date.asc(), InvestmentTransaction.created_at.asc())
            )
        )
