"""Asset register with append-only valuation history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.constants import AssetType, AuditAction
from ..core.dates import today_local
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.money import format_money, ratio, to_minor
from ..database.audit import record_audit
from ..database.init import get_household
from ..database.models import Asset, AssetValuation, FamilyMember


@dataclass
class AssetLine:
    id: str
    name: str
    asset_type: str
    value_minor: int
    owned_value_minor: int
    ownership_share_bps: int
    cost_minor: int
    currency: str
    is_liquid: bool
    is_primary_residence: bool
    valuation_date: str | None
    valuation_source: str | None
    stale: bool

    def as_dict(self, currency: str) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.asset_type,
            "value": format_money(self.value_minor, currency),
            "value_minor": self.value_minor,
            "owned_value": format_money(self.owned_value_minor, currency),
            "owned_value_minor": self.owned_value_minor,
            "ownership_share": str(Decimal(self.ownership_share_bps) / 10000),
            "cost": format_money(self.cost_minor, self.currency),
            "gain": format_money(
                self.owned_value_minor
                - int(self.cost_minor * self.ownership_share_bps / 10000),
                self.currency,
            ),
            "liquid": self.is_liquid,
            "primary_residence": self.is_primary_residence,
            "valuation_date": self.valuation_date,
            "valuation_source": self.valuation_source,
            "stale": self.stale,
        }


class AssetService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.household = get_household(session)
        self.household_id = self.household.id

    def add(
        self,
        *,
        name: str,
        asset_type: str,
        value: str | int,
        acquisition_cost: str | int | None = None,
        is_liquid: bool = False,
        is_primary_residence: bool = False,
        member_id: str | None = None,
        ownership_share: Decimal | str | None = None,
        valued_on: date | None = None,
        valuation_source: str = "manual",
        currency: str | None = None,
        notes: str | None = None,
        actor: str = "cli",
    ) -> Asset:
        if asset_type not in {t.value for t in AssetType}:
            raise ValidationError(f"未知资产类型: {asset_type}")
        value_minor = _minor(value, currency or self.settings.currency)
        if value_minor < 0:
            raise ValidationError("资产估值不能为负")
        cost_minor = (
            _minor(acquisition_cost, currency or self.settings.currency)
            if acquisition_cost is not None
            else value_minor
        )
        share_bps = 10000
        if ownership_share is not None:
            share = Decimal(str(ownership_share))
            if share <= 0 or share > 1:
                raise ValidationError("ownership_share 必须在 (0, 1] 区间")
            share_bps = int(share * 10000)
        if member_id:
            member = self.session.get(FamilyMember, member_id)
            if member is None or member.household_id != self.household_id:
                raise ValidationError(f"家庭成员不存在: {member_id}")
        asset = Asset(
            id=new_id("asset"),
            household_id=self.household_id,
            member_id=member_id,
            name=name.strip(),
            asset_type=asset_type,
            currency=(currency or self.settings.currency).upper(),
            ownership_share_bps=share_bps,
            acquisition_cost_minor=cost_minor,
            current_value_minor=value_minor,
            valuation_date=valued_on or today_local(self.settings.timezone),
            valuation_source=valuation_source,
            is_liquid=is_liquid,
            is_primary_residence=is_primary_residence,
            notes=notes,
        )
        self.session.add(asset)
        self.session.flush()
        self.session.add(
            AssetValuation(
                id=new_id("valuation"),
                asset_id=asset.id,
                value_minor=value_minor,
                currency=asset.currency,
                valued_on=asset.valuation_date,
                source=valuation_source,
                note="initial",
            )
        )
        record_audit(
            self.session,
            household_id=self.household_id,
            action=AuditAction.CREATE,
            entity_type="asset",
            entity_id=asset.id,
            after={"name": asset.name, "value_minor": value_minor},
            actor=actor,
        )
        self.session.flush()
        return asset

    def value(
        self,
        asset_ref: str,
        *,
        value: str | int,
        valued_on: date | None = None,
        source: str = "manual",
        note: str | None = None,
        actor: str = "cli",
    ) -> Asset:
        asset = self.get(asset_ref)
        valued_on = valued_on or today_local(self.settings.timezone)
        value_minor = _minor(value, asset.currency)
        if value_minor < 0:
            raise ValidationError("资产估值不能为负")
        existing = self.session.scalar(
            select(AssetValuation).where(
                AssetValuation.asset_id == asset.id,
                AssetValuation.valued_on == valued_on,
                AssetValuation.source == source,
            )
        )
        before = {"value_minor": asset.current_value_minor, "date": str(asset.valuation_date)}
        if existing is None:
            self.session.add(
                AssetValuation(
                    id=new_id("valuation"),
                    asset_id=asset.id,
                    value_minor=value_minor,
                    currency=asset.currency,
                    valued_on=valued_on,
                    source=source,
                    note=note,
                )
            )
        else:
            existing.value_minor = value_minor
            existing.note = note or existing.note
        # Never overwrite history; only move "current" forward.
        if asset.valuation_date is None or valued_on >= asset.valuation_date:
            asset.current_value_minor = value_minor
            asset.valuation_date = valued_on
            asset.valuation_source = source
        record_audit(
            self.session,
            household_id=self.household_id,
            action=AuditAction.UPDATE,
            entity_type="asset",
            entity_id=asset.id,
            before=before,
            after={"value_minor": value_minor, "date": str(valued_on), "source": source},
            detail="valuation",
            actor=actor,
        )
        self.session.flush()
        return asset

    def get(self, ref: str) -> Asset:
        asset = self.session.get(Asset, ref)
        if asset is None:
            asset = self.session.scalar(
                select(Asset).where(
                    Asset.household_id == self.household_id, Asset.name == ref
                )
            )
        if asset is None:
            raise ValidationError(f"资产不存在: {ref}")
        return asset

    def list(self, *, include_inactive: bool = False) -> list[AssetLine]:
        stmt = select(Asset).where(Asset.household_id == self.household_id)
        if not include_inactive:
            stmt = stmt.where(Asset.is_active.is_(True))
        lines: list[AssetLine] = []
        today = today_local(self.settings.timezone)
        for asset in self.session.scalars(stmt.order_by(Asset.created_at.asc())):
            stale = (
                asset.valuation_date is None
                or (today - asset.valuation_date).days > 365
            )
            lines.append(
                AssetLine(
                    id=asset.id,
                    name=asset.name,
                    asset_type=asset.asset_type,
                    value_minor=asset.current_value_minor,
                    owned_value_minor=int(
                        asset.current_value_minor * asset.ownership_share_bps / 10000
                    ),
                    ownership_share_bps=asset.ownership_share_bps,
                    cost_minor=asset.acquisition_cost_minor,
                    currency=asset.currency,
                    is_liquid=asset.is_liquid,
                    is_primary_residence=asset.is_primary_residence,
                    valuation_date=asset.valuation_date.isoformat() if asset.valuation_date else None,
                    valuation_source=asset.valuation_source,
                    stale=stale,
                )
            )
        return lines

    def history(self, asset_ref: str) -> list[AssetValuation]:
        asset = self.get(asset_ref)
        return list(
            self.session.scalars(
                select(AssetValuation)
                .where(AssetValuation.asset_id == asset.id)
                .order_by(AssetValuation.valued_on.asc())
            )
        )

    def summary(self) -> dict:
        all_lines = self.list()
        lines = [line for line in all_lines if line.currency == self.settings.currency]
        excluded = sorted({line.currency for line in all_lines} - {self.settings.currency})
        total = sum(line.owned_value_minor for line in lines)
        liquid = sum(
            line.owned_value_minor
            for line in lines
            if line.is_liquid and not line.is_primary_residence
        )
        by_type: dict[str, int] = {}
        for line in lines:
            by_type[line.asset_type] = by_type.get(line.asset_type, 0) + line.owned_value_minor
        return {
            "total_minor": total,
            "liquid_minor": liquid,
            "liquid_ratio": str(ratio(liquid, total)) if total else None,
            "by_type": {
                k: {"amount": format_money(v, self.settings.currency), "amount_minor": v}
                for k, v in sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)
            },
            "assets": [line.as_dict(self.settings.currency) for line in lines],
            "excluded_currencies": excluded,
            "stale": [line.name for line in lines if line.stale],
        }


def _minor(value: str | int, currency: str) -> int:
    if isinstance(value, int):
        return value
    return to_minor(value, currency)
