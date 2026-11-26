"""SQLAlchemy 2.x models for the Family CFO ledger.

Monetary columns are integer minor units (fen).  ``amount_base_minor`` is the
value in the household base currency; the original ``amount_minor`` and
``currency`` are always kept separately.  Non-monetary decimals (quantities,
prices, rates) use :class:`DecimalText` so SQLite never stores them as floats.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..core.constants import (
    AccountType,
    BudgetPeriod,
    BudgetStatus,
    CategoryKind,
    Direction,
    ImportStatus,
    SettlementStatus,
    TxnStatus,
    TxnType,
)
from ..core.money import decimal_str


def local_now() -> datetime:
    return datetime.now().replace(microsecond=0)


class DecimalText(TypeDecorator):
    """Exact decimal storage for quantities / prices / rates."""

    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> str | None:
        if value is None:
            return None
        return decimal_str(value)

    def process_result_value(self, value: Any, dialect) -> Decimal | None:
        if value is None or value == "":
            return None
        return Decimal(str(value))


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=local_now, onupdate=local_now, nullable=False
    )


# ---------------------------------------------------------------------------
# Household & members
# ---------------------------------------------------------------------------
class Household(Base, TimestampMixin):
    __tablename__ = "households"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class FamilyMember(Base, TimestampMixin):
    __tablename__ = "family_members"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(40), default="member", nullable=False)
    birth_date: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("household_id", "name"),)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), default=AccountType.BANK, nullable=False)
    institution: Mapped[str | None] = mapped_column(String(120))
    platform: Mapped[str | None] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    last4: Mapped[str | None] = mapped_column(String(4))
    is_asset: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_liability: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opening_balance_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_as_of: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("household_id", "name"),)


# ---------------------------------------------------------------------------
# Import batches & raw rows
# ---------------------------------------------------------------------------
class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batches"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    archive_path: Mapped[str | None] = mapped_column(String(1024))
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=ImportStatus.PENDING, nullable=False)
    detected_platform: Mapped[str | None] = mapped_column(String(40))
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    period: Mapped[str | None] = mapped_column(String(7), index=True)
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    error_report_path: Mapped[str | None] = mapped_column(String(1024))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("household_id", "file_hash"),
        Index("ix_import_batches_status_period", "status", "period"),
    )


class RawTransaction(Base):
    __tablename__ = "raw_transactions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(20), default="parsed", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)

    __table_args__ = (UniqueConstraint("batch_id", "row_number"),)


# ---------------------------------------------------------------------------
# Categories & rules
# ---------------------------------------------------------------------------
class TransactionCategory(Base, TimestampMixin):
    __tablename__ = "transaction_categories"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    household_id: Mapped[str | None] = mapped_column(ForeignKey("households.id"))
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default=CategoryKind.EXPENSE, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("transaction_categories.id"))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_essential: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    keywords: Mapped[str | None] = mapped_column(Text)  # JSON array of seeds

    __table_args__ = (Index("ix_categories_household_kind", "household_id", "kind"),)


class CategoryRule(Base, TimestampMixin):
    __tablename__ = "category_rules"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("transaction_categories.id"), nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    match_type: Mapped[str] = mapped_column(String(20), default="keyword", nullable=False)
    pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(10))
    txn_type: Mapped[str | None] = mapped_column(String(30))
    source_platform: Mapped[str | None] = mapped_column(String(40))
    min_amount_minor: Mapped[int | None] = mapped_column(Integer)
    max_amount_minor: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str] = mapped_column(String(40), default="system", nullable=False)

    __table_args__ = (Index("ix_rules_household_priority", "household_id", "priority"),)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id"), nullable=False, index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(120))
    source_platform: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    source_batch_id: Mapped[str | None] = mapped_column(ForeignKey("import_batches.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), default=Direction.OUTFLOW, nullable=False)
    txn_type: Mapped[str] = mapped_column(String(30), default=TxnType.EXPENSE, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    amount_base_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    fx_rate: Mapped[Decimal | None] = mapped_column(DecimalText)
    counterparty: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("transaction_categories.id"), index=True
    )
    member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    payer_member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    owner_member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    beneficiary: Mapped[str | None] = mapped_column(String(80))
    is_shared: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=TxnStatus.CONFIRMED, nullable=False)
    stats_included: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    review_reason: Mapped[str | None] = mapped_column(String(40))
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    duplicate_of_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))
    refund_of_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))
    transfer_group_id: Mapped[str | None] = mapped_column(String(40))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("source_platform", "external_id"),
        Index("ix_txn_household_occurred", "household_id", "occurred_at"),
        Index("ix_txn_household_dedupe", "household_id", "dedupe_key"),
        Index("ix_txn_status_review", "status", "review_reason"),
        CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        CheckConstraint("amount_base_minor >= 0", name="amount_base_non_negative"),
    )


class TransferMatch(Base):
    __tablename__ = "transfer_matches"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outflow_txn_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    inflow_txn_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    confidence_bps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    match_method: Mapped[str] = mapped_column(String(40), default="auto", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)

    __table_args__ = (UniqueConstraint("outflow_txn_id", "inflow_txn_id"),)


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    refund_txn_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id"), nullable=False, unique=True
    )
    original_txn_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    method: Mapped[str] = mapped_column(String(40), default="auto", nullable=False)
    confidence_bps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)


# ---------------------------------------------------------------------------
# Assets & liabilities
# ---------------------------------------------------------------------------
class Asset(Base, TimestampMixin):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(24), default="deposit", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    ownership_share_bps: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    acquisition_cost_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_value_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    valuation_date: Mapped[date | None] = mapped_column(Date)
    valuation_source: Mapped[str | None] = mapped_column(String(80))
    is_liquid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_primary_residence: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "ownership_share_bps >= 0 AND ownership_share_bps <= 10000", name="valid_share"
        ),
        CheckConstraint("current_value_minor >= 0", name="value_non_negative"),
    )


class AssetValuation(Base):
    __tablename__ = "asset_valuations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    value_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    valued_on: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="manual", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)

    __table_args__ = (UniqueConstraint("asset_id", "valued_on", "source"),)


class Liability(Base, TimestampMixin):
    __tablename__ = "liabilities"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    liability_type: Mapped[str] = mapped_column(String(24), default="mortgage", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    principal_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    annual_rate_bps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    repayment_method: Mapped[str] = mapped_column(String(32), default="equal_installment", nullable=False)
    repayment_cycle: Mapped[str] = mapped_column(String(16), default="monthly", nullable=False)
    next_payment_date: Mapped[date | None] = mapped_column(Date)
    term_months: Mapped[int | None] = mapped_column(Integer)
    remaining_months: Mapped[int | None] = mapped_column(Integer)
    monthly_payment_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("balance_minor >= 0", name="balance_non_negative"),
        CheckConstraint("annual_rate_bps >= 0", name="rate_non_negative"),
    )


class LiabilityPayment(Base):
    __tablename__ = "liability_payments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    liability_id: Mapped[str] = mapped_column(
        ForeignKey("liabilities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    principal_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    interest_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
class Budget(Base, TimestampMixin):
    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), default="家庭预算", nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), default=BudgetPeriod.MONTHLY, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=BudgetStatus.ACTIVE, nullable=False)
    total_amount_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("household_id", "period_type", "period_start", "version"),
        Index("ix_budgets_lookup", "household_id", "period_type", "period_start", "status"),
    )


class BudgetItem(Base):
    __tablename__ = "budget_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    budget_id: Mapped[str] = mapped_column(
        ForeignKey("budgets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[str | None] = mapped_column(ForeignKey("transaction_categories.id"))
    member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    item_type: Mapped[str] = mapped_column(String(10), default="variable", nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Investments
# ---------------------------------------------------------------------------
class InvestmentProduct(Base, TimestampMixin):
    __tablename__ = "investment_products"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), default="fund", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    market: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("household_id", "symbol", "product_type"),)


class InvestmentTransaction(Base):
    __tablename__ = "investment_transactions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        ForeignKey("investment_products.id"), nullable=False, index=True
    )
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    txn_type: Mapped[str] = mapped_column(String(16), default="buy", nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"), nullable=False)
    gross_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fee_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    net_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)


class InvestmentPosition(Base, TimestampMixin):
    __tablename__ = "investment_positions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        ForeignKey("investment_products.id"), nullable=False, index=True
    )
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    quantity: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"), nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"), nullable=False)
    cost_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    market_value_minor: Mapped[int | None] = mapped_column(Integer)
    valuation_date: Mapped[date | None] = mapped_column(Date)
    valuation_source: Mapped[str | None] = mapped_column(String(80))
    realized_pnl_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unrealized_pnl_minor: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=local_now, onupdate=local_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)

    __table_args__ = (UniqueConstraint("product_id", "account_id"),)


# ---------------------------------------------------------------------------
# Goals, snapshots, sync, audit, config, settlement
# ---------------------------------------------------------------------------
class FinancialGoal(Base, TimestampMixin):
    __tablename__ = "financial_goals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    goal_type: Mapped[str] = mapped_column(String(24), default="wealth", nullable=False)
    target_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    current_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date)
    monthly_saving_minor: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    risk_tolerance: Mapped[str] = mapped_column(String(16), default="moderate", nullable=False)
    assumptions: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)

    __table_args__ = (
        CheckConstraint("target_minor > 0", name="target_positive"),
        CheckConstraint("current_minor >= 0", name="current_non_negative"),
    )


class FinancialSnapshot(Base):
    __tablename__ = "financial_snapshots"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    total_assets_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_liabilities_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    net_worth_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    liquid_assets_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_income_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_expense_minor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)

    __table_args__ = (UniqueConstraint("household_id", "as_of"),)


class SyncRecord(Base):
    __tablename__ = "sync_records"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), default="push", nullable=False)
    remote_table: Mapped[str | None] = mapped_column(String(120))
    remote_record_id: Mapped[str | None] = mapped_column(String(80))
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    remote_modified_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=local_now, onupdate=local_now, nullable=False
    )

    __table_args__ = (UniqueConstraint("entity_type", "entity_id", "direction"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str | None] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    actor: Mapped[str] = mapped_column(String(80), default="cli", nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(40), nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)

    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class SystemConfig(Base):
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=local_now, onupdate=local_now, nullable=False
    )


class Settlement(Base, TimestampMixin):
    __tablename__ = "settlements"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=SettlementStatus.COLLECTING, nullable=False)
    expected_sources: Mapped[str | None] = mapped_column(Text)  # JSON array
    checklist_json: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_by: Mapped[str | None] = mapped_column(String(80))
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    override_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("household_id", "period"),)


class SettlementAdjustment(Base):
    __tablename__ = "settlement_adjustments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    settlement_id: Mapped[str] = mapped_column(
        ForeignKey("settlements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=local_now, nullable=False)
