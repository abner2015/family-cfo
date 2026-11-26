"""Database bootstrap: schema, household, default categories, rules, accounts."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import AccountType, CategoryKind
from ..core.ids import new_id
from ..core.logging import get_logger
from .base import Database
from .models import (
    Account,
    CategoryRule,
    FamilyMember,
    Household,
    SystemConfig,
    TransactionCategory,
)

log = get_logger("db.init")

SCHEMA_VERSION = "1"

# slug, name, kind, essential, sort, keywords
DEFAULT_CATEGORIES: list[tuple[str, str, str, bool, int, list[str]]] = [
    ("food", "餐饮", CategoryKind.EXPENSE, True, 10,
     ["餐饮", "外卖", "美团", "饿了么", "餐厅", "饭店", "咖啡", "奶茶", "食堂", "早餐", "午餐", "晚餐", "肯德基", "麦当劳", "星巴克", "海底捞", "火锅", "烧烤", "面馆", "快餐", "小吃"]),
    ("groceries", "日用品", CategoryKind.EXPENSE, True, 20,
     ["超市", "便利店", "沃尔玛", "永辉", "盒马", "京东超市", "日用品", "洗护", "生鲜", "菜市场"]),
    ("housing", "住房", CategoryKind.EXPENSE, True, 30,
     ["房租", "物业", "房贷", "租金", "中介费", "维修"]),
    ("utilities", "水电物业", CategoryKind.EXPENSE, True, 40,
     ["水费", "电费", "燃气", "天然气", "供暖", "物业费", "宽带", "话费", "通讯"]),
    ("transport", "交通", CategoryKind.EXPENSE, True, 50,
     ["地铁", "公交", "打车", "滴滴", "出租车", "加油", "停车", "高速", "铁路", "12306", "机票", "交通"]),
    ("healthcare", "医疗", CategoryKind.EXPENSE, True, 60,
     ["医院", "药店", "药房", "挂号", "体检", "医疗", "诊所", "牙科", "同仁堂", "药品", "买药"]),
    ("education", "教育", CategoryKind.EXPENSE, True, 70,
     ["学费", "培训", "课程", "书店", "文具", "学校", "幼儿园", "教育", "教材"]),
    ("insurance", "保险", CategoryKind.EXPENSE, True, 80,
     ["保险", "保费", "重疾", "医疗险", "车险"]),
    ("shopping", "购物", CategoryKind.EXPENSE, False, 90,
     ["淘宝", "天猫", "京东", "拼多多", "唯品会", "服装", "鞋", "数码", "家电", "购物", "商场", "百货", "消费"]),
    ("entertainment", "娱乐", CategoryKind.EXPENSE, False, 100,
     ["电影", "游戏", "健身", "演出", "会员", "视频", "音乐", "娱乐", "KTV"]),
    ("travel", "旅游", CategoryKind.EXPENSE, False, 110,
     ["旅游", "酒店", "民宿", "景点", "携程", "飞猪", "度假", "门票"]),
    ("social_gifts", "人情往来", CategoryKind.EXPENSE, False, 120,
     ["礼金", "红包", "份子", "礼品", "人情", "随礼"]),
    ("other_expense", "其他支出", CategoryKind.EXPENSE, False, 900, ["其他支出"]),
    ("salary", "工资", CategoryKind.INCOME, False, 10, ["工资", "薪资", "代发工资", "薪酬"]),
    ("bonus", "奖金", CategoryKind.INCOME, False, 20, ["奖金", "年终奖", "绩效"]),
    ("side_job", "兼职", CategoryKind.INCOME, False, 30, ["兼职", "稿费", "劳务", "副业"]),
    ("investment_income", "投资收益", CategoryKind.INCOME, False, 40,
     ["分红", "利息", "理财收益", "股息", "基金收益"]),
    ("other_income", "其他收入", CategoryKind.INCOME, False, 900, ["其他收入", "退款入账", "报销"]),
    ("internal_transfer", "内部转账", CategoryKind.TRANSFER, False, 10,
     ["转账", "内部转账", "提现", "充值", "零钱通", "余额宝"]),
    ("credit_card_repayment", "信用卡还款", CategoryKind.TRANSFER, False, 20,
     ["信用卡还款", "还款", "自动还款"]),
]

# (slug, category slug, pattern, priority)
DEFAULT_RULES: list[tuple[str, str, str, int]] = [
    ("r_food_meituan", "food", "美团", 10),
    ("r_food_eleme", "food", "饿了么", 10),
    ("r_food_coffee", "food", "咖啡", 20),
    ("r_grocery_hema", "groceries", "盒马", 10),
    ("r_grocery_jd", "groceries", "京东超市", 10),
    ("r_util_telecom", "utilities", "话费", 10),
    ("r_transport_didi", "transport", "滴滴", 10),
    ("r_transport_metro", "transport", "地铁", 10),
    ("r_health_pharmacy", "healthcare", "药房", 10),
    ("r_edu_tuition", "education", "学费", 10),
    ("r_ins_premium", "insurance", "保费", 10),
    ("r_social_gift", "social_gifts", "礼金", 10),
    ("r_cc_repay", "credit_card_repayment", "信用卡还款", 5),
    ("r_internal_transfer", "internal_transfer", "转账", 30),
]


@dataclass
class InitResult:
    household_id: str
    member_id: str
    created: bool
    category_count: int
    account_count: int


def _to_bps(rate: str) -> int:
    from decimal import Decimal

    return int((Decimal(rate) * 10000).to_integral_value())


def ensure_schema(db: Database) -> None:
    from .migrate import run_migrations

    run_migrations(db)
    with db.session() as session:
        row = session.get(SystemConfig, "schema_version")
        if row is None:
            session.add(SystemConfig(key="schema_version", value=SCHEMA_VERSION))
        else:
            row.value = SCHEMA_VERSION


def seed_categories(session: Session, household_id: str) -> dict[str, TransactionCategory]:
    existing = {
        c.slug: c
        for c in session.scalars(
            select(TransactionCategory).where(
                (TransactionCategory.household_id == household_id)
                | (TransactionCategory.household_id.is_(None))
            )
        )
    }
    created: dict[str, TransactionCategory] = {}
    for slug, name, kind, essential, order, keywords in DEFAULT_CATEGORIES:
        cat = existing.get(slug)
        if cat is None:
            cat = TransactionCategory(
                id=new_id("category"),
                slug=slug,
                household_id=household_id,
                name=name,
                kind=kind,
                is_system=True,
                is_essential=essential,
                sort_order=order,
                keywords=json.dumps(keywords, ensure_ascii=False),
            )
            session.add(cat)
            session.flush()
        created[slug] = cat
    return created


def seed_rules(session: Session, household_id: str, categories: dict[str, TransactionCategory]) -> None:
    existing = {
        r.id
        for r in session.scalars(
            select(CategoryRule).where(CategoryRule.household_id == household_id)
        )
    }
    for slug, cat_slug, pattern, priority in DEFAULT_RULES:
        if slug in existing or cat_slug not in categories:
            continue
        session.add(
            CategoryRule(
                id=slug if slug not in existing else new_id("rule"),
                household_id=household_id,
                category_id=categories[cat_slug].id,
                priority=priority,
                match_type="keyword",
                pattern=pattern,
                is_active=True,
                created_by="system",
            )
        )


DEFAULT_ACCOUNTS = [
    ("微信零钱", AccountType.PAYMENT_PLATFORM, "微信支付", "wechat", True, False),
    ("支付宝余额", AccountType.PAYMENT_PLATFORM, "支付宝", "alipay", True, False),
    ("银行卡", AccountType.BANK, None, "bank", True, False),
    ("现金", AccountType.CASH, None, "cash", True, False),
    ("信用卡", AccountType.CREDIT_CARD, None, "credit_card", False, True),
]


def seed_accounts(session: Session, household_id: str, member_id: str, currency: str) -> list[Account]:
    accounts: list[Account] = []
    for name, acc_type, institution, platform, is_asset, is_liability in DEFAULT_ACCOUNTS:
        acc = Account(
            id=new_id("account"),
            household_id=household_id,
            member_id=member_id if not is_liability else None,
            name=name,
            account_type=acc_type,
            institution=institution,
            platform=platform,
            currency=currency,
            is_asset=is_asset,
            is_liability=is_liability,
        )
        session.add(acc)
        accounts.append(acc)
    return accounts


def initialize(
    db: Database,
    settings: Settings,
    *,
    household_name: str | None = None,
    member_name: str = "我",
    reset: bool = False,
) -> InitResult:
    """Create schema and seed a new household. Idempotent unless ``reset``."""
    ensure_schema(db)
    with db.session() as session:
        existing = session.scalar(select(Household).limit(1))
        if existing is not None and not reset:
            member = session.scalar(
                select(FamilyMember).where(FamilyMember.household_id == existing.id).limit(1)
            )
            cats = seed_categories(session, existing.id)
            seed_rules(session, existing.id, cats)
            count = len(
                session.scalars(
                    select(Account).where(Account.household_id == existing.id)
                ).all()
            )
            return InitResult(
                household_id=existing.id,
                member_id=member.id if member else "",
                created=False,
                category_count=len(cats),
                account_count=count,
            )
        household_id = new_id("household")
        household = Household(
            id=household_id,
            name=household_name or settings.household_name,
            base_currency=settings.currency,
        )
        session.add(household)
        session.flush()
        member = FamilyMember(
            id=new_id("member"), household_id=household_id, name=member_name, role="owner"
        )
        session.add(member)
        session.flush()
        cats = seed_categories(session, household_id)
        seed_rules(session, household_id, cats)
        accounts = seed_accounts(session, household_id, member.id, settings.currency)
        session.flush()
        session.add(
            SystemConfig(
                key="default_categories",
                value=json.dumps(sorted(cats), ensure_ascii=False),
            )
        )
        return InitResult(
            household_id=household_id,
            member_id=member.id,
            created=True,
            category_count=len(cats),
            account_count=len(accounts),
        )


def get_household(session: Session) -> Household:
    household = session.scalar(select(Household).limit(1))
    if household is None:
        from ..core.errors import ConfigError

        raise ConfigError("数据库尚未初始化，请先运行 `family-cfo init`。")
    return household
