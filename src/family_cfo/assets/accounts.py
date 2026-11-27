"""Household accounts (cash, bank, cards, wallets, investment accounts)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import AccountType
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.logging import get_logger
from ..core.money import from_minor
from ..database.audit import record_audit
from ..database.init import get_household
from ..database.models import Account, FamilyMember

log = get_logger("assets.accounts")

PLATFORM_DEFAULT_ACCOUNT = {
    # 用户确认（2026-11-27）：微信/支付宝/京东都只是「招行储蓄卡(主账户)」的支付通道，
    # 钱最终从主账户出。若为每个渠道建独立账户，会让同一笔消费在两处重复计数。
    # 渠道信息统一记录在 transactions.source_platform，账户一律归主账户。
    "wechat": ("招行储蓄卡(主账户)", AccountType.BANK),
    "alipay": ("招行储蓄卡(主账户)", AccountType.BANK),
    "jd": ("招行储蓄卡(主账户)", AccountType.BANK),
    "other": ("招行储蓄卡(主账户)", AccountType.BANK),
    "bank": ("招行储蓄卡(主账户)", AccountType.BANK),
    "credit_card": ("京东白条(信用卡)", AccountType.CREDIT_CARD),
    "cash": ("现金", AccountType.CASH),
    "manual": ("现金", AccountType.CASH),
}


@dataclass
class BalanceInfo:
    account_id: str
    name: str
    stored_minor: int
    computed_minor: int
    difference_minor: int
    currency: str


class AccountService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id

    def list(self, *, include_inactive: bool = False) -> list[Account]:
        stmt = select(Account).where(Account.household_id == self.household_id)
        if not include_inactive:
            stmt = stmt.where(Account.is_active.is_(True))
        return list(self.session.scalars(stmt.order_by(Account.created_at.asc())))

    def get(self, ref: str) -> Account:
        account = self.session.get(Account, ref)
        if account is None:
            account = self.session.scalar(
                select(Account).where(
                    Account.household_id == self.household_id, Account.name == ref
                )
            )
        if account is None:
            raise ValidationError(f"账户不存在: {ref}")
        return account

    def create(
        self,
        *,
        name: str,
        account_type: str = AccountType.BANK,
        institution: str | None = None,
        platform: str | None = None,
        currency: str | None = None,
        member_id: str | None = None,
        is_asset: bool = True,
        is_liability: bool = False,
        opening_balance_minor: int = 0,
        last4: str | None = None,
    ) -> Account:
        if not name.strip():
            raise ValidationError("账户名称不能为空")
        exists = self.session.scalar(
            select(Account).where(
                Account.household_id == self.household_id, Account.name == name.strip()
            )
        )
        if exists is not None:
            raise ValidationError(f"账户已存在: {name}")
        if member_id:
            self._assert_member(member_id)
        account = Account(
            id=new_id("account"),
            household_id=self.household_id,
            member_id=member_id,
            name=name.strip(),
            account_type=account_type,
            institution=institution,
            platform=platform,
            currency=(currency or self.settings.currency).upper(),
            last4=last4,
            is_asset=is_asset,
            is_liability=is_liability,
            opening_balance_minor=opening_balance_minor,
            balance_minor=opening_balance_minor,
        )
        self.session.add(account)
        self.session.flush()
        record_audit(
            self.session,
            household_id=self.household_id,
            action="create",
            entity_type="account",
            entity_id=account.id,
            after={"name": account.name, "type": account.account_type},
        )
        return account

    def resolve(self, ref: str | None, *, platform: str | None = None) -> Account:
        if ref:
            return self.get(ref)
        key = (platform or "other").lower()
        name, acc_type = PLATFORM_DEFAULT_ACCOUNT.get(key, ("其他账户", AccountType.OTHER))
        existing = self.session.scalar(
            select(Account).where(
                Account.household_id == self.household_id, Account.name == name
            )
        )
        if existing is not None:
            return existing
        log.info("creating default account for platform %s: %s", key, name)
        return self.create(name=name, account_type=acc_type, platform=key)

    def set_balance(self, ref: str, balance_minor: int, *, as_of: date, source: str = "manual") -> Account:
        account = self.get(ref)
        if account.currency != self.settings.currency:
            raise ValidationError("多币种账户余额请使用原币账户，暂不支持混币")
        before = {"balance_minor": account.balance_minor, "as_of": str(account.balance_as_of)}
        account.balance_minor = balance_minor
        account.balance_as_of = as_of
        record_audit(
            self.session,
            household_id=self.household_id,
            action="update",
            entity_type="account",
            entity_id=account.id,
            before=before,
            after={"balance_minor": balance_minor, "as_of": str(as_of), "source": source},
            detail="balance_update",
        )
        return account

    def computed_balance(self, account: Account) -> int:
        from sqlalchemy import text

        signed = self.session.execute(
            text(
                """
                SELECT COALESCE(SUM(CASE WHEN direction = 'inflow' THEN amount_minor
                                         ELSE -amount_minor END), 0)
                FROM transactions
                WHERE account_id = :account_id AND is_deleted = 0 AND status != 'void'
                """
            ),
            {"account_id": account.id},
        ).scalar()
        return account.opening_balance_minor + int(signed or 0)

    def balance_report(self) -> list[BalanceInfo]:
        out: list[BalanceInfo] = []
        for account in self.list():
            computed = self.computed_balance(account)
            out.append(
                BalanceInfo(
                    account_id=account.id,
                    name=account.name,
                    stored_minor=account.balance_minor,
                    computed_minor=computed,
                    difference_minor=account.balance_minor - computed,
                    currency=account.currency,
                )
            )
        return out

    def _assert_member(self, member_id: str) -> None:
        member = self.session.get(FamilyMember, member_id)
        if member is None or member.household_id != self.household_id:
            raise ValidationError(f"家庭成员不存在: {member_id}")


def format_account(account: Account) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "type": account.account_type,
        "platform": account.platform,
        "currency": account.currency,
        "balance": str(from_minor(account.balance_minor, account.currency)),
        "balance_as_of": account.balance_as_of.isoformat() if account.balance_as_of else None,
        "is_liability": account.is_liability,
    }
