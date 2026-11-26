"""Shared enumerations and constants.

Values are stored as strings in SQLite so migrations stay simple and exports
remain human readable.
"""

from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"


class TxnType(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    REFUND = "refund"
    REVERSAL = "reversal"
    TRANSFER = "transfer"
    CREDIT_CARD_REPAYMENT = "credit_card_repayment"
    TOPUP = "topup"
    WITHDRAWAL = "withdrawal"
    INVESTMENT_BUY = "investment_buy"
    INVESTMENT_SELL = "investment_sell"
    LOAN_OUT = "loan_out"
    LOAN_REPAY = "loan_repay"
    FEE = "fee"
    UNKNOWN = "unknown"


#: Types that must not count as household income/expense in analytics.
NON_FLOW_TYPES = {
    TxnType.TRANSFER,
    TxnType.CREDIT_CARD_REPAYMENT,
    TxnType.TOPUP,
    TxnType.WITHDRAWAL,
    TxnType.INVESTMENT_BUY,
    TxnType.INVESTMENT_SELL,
    TxnType.LOAN_OUT,
    TxnType.LOAN_REPAY,
}

EXPENSE_TYPES = {TxnType.EXPENSE, TxnType.REFUND, TxnType.REVERSAL, TxnType.FEE}


class TxnStatus(StrEnum):
    CONFIRMED = "confirmed"
    PENDING_REVIEW = "pending_review"
    VOID = "void"
    REVERSED = "reversed"


class AccountType(StrEnum):
    BANK = "bank"
    CASH = "cash"
    CREDIT_CARD = "credit_card"
    PAYMENT_PLATFORM = "payment_platform"
    INVESTMENT = "investment"
    OTHER = "other"


class CategoryKind(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    EXCLUDED = "excluded"


class AssetType(StrEnum):
    DEPOSIT = "deposit"
    CASH = "cash"
    PROPERTY = "property"
    VEHICLE = "vehicle"
    STOCK = "stock"
    FUND = "fund"
    BOND = "bond"
    WEALTH_MGMT = "wealth_mgmt"
    OTHER = "other"


class LiabilityType(StrEnum):
    MORTGAGE = "mortgage"
    AUTO_LOAN = "auto_loan"
    CONSUMER_LOAN = "consumer_loan"
    CREDIT_CARD = "credit_card"
    OTHER = "other"


class BudgetPeriod(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


class BudgetStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CLOSED = "closed"


class BudgetItemType(StrEnum):
    FIXED = "fixed"
    VARIABLE = "variable"


class GoalType(StrEnum):
    EMERGENCY = "emergency"
    EDUCATION = "education"
    RETIREMENT = "retirement"
    HOUSING = "housing"
    MAJOR_PURCHASE = "major_purchase"
    WEALTH = "wealth"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    ACHIEVED = "achieved"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class ProductType(StrEnum):
    STOCK = "stock"
    FUND = "fund"
    BOND = "bond"
    OTHER = "other"


class InvTxnType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    FEE = "fee"


class ImportStatus(StrEnum):
    PENDING = "pending"
    PARSED = "parsed"
    IMPORTING = "importing"
    IMPORTED = "imported"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class MatchStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SyncDirection(StrEnum):
    PUSH = "push"
    PULL = "pull"


class SyncStatus(StrEnum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    CONFLICT = "conflict"
    SKIPPED = "skipped"


class SettlementStatus(StrEnum):
    COLLECTING = "COLLECTING"
    IMPORTING = "IMPORTING"
    RECONCILING = "RECONCILING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY_TO_CLOSE = "READY_TO_CLOSE"
    CLOSED = "CLOSED"


class RuleMatchType(StrEnum):
    KEYWORD = "keyword"
    REGEX = "regex"
    COUNTERPARTY = "counterparty"
    AMOUNT_RANGE = "amount_range"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    SOFT_DELETE = "soft_delete"
    RESTORE = "restore"
    IMPORT = "import"
    BACKUP = "backup"
    SYNC = "sync"
    SETTLE = "settle"
    REOPEN = "reopen"
    LOGIN_READ = "read"


REVIEW_DUPLICATE = "possible_duplicate"
REVIEW_UNCATEGORIZED = "uncategorized"
REVIEW_AMBIGUOUS_TRANSFER = "ambiguous_transfer"
REVIEW_REFUND_UNLINKED = "refund_unlinked"
REVIEW_REVERSAL_UNLINKED = "reversal_unlinked"
REVIEW_PLATFORM_CLEARING = "platform_clearing"
REVIEW_AMOUNT_ANOMALY = "amount_anomaly"
REVIEW_MAPPING = "mapping_required"
REVIEW_MISSING_SOURCE = "missing_source"

SOURCE_WECHAT = "wechat"
SOURCE_ALIPAY = "alipay"
SOURCE_BANK = "bank"
SOURCE_CREDIT_CARD = "credit_card"
SOURCE_MANUAL = "manual"
SOURCE_OTHER = "other"

DEFAULT_TIMEZONE = "Asia/Shanghai"
