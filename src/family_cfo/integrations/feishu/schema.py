"""Bitable table/field schema definitions.

The YAML file ``config/feishu_tables.yaml`` is the editable source; the Python
defaults below are used when it is missing so a fresh checkout still works.
Field type mapping to Bitable: text=1, number=2, single_select=3, date=5,
checkbox=7, url=15.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ...core.errors import ConfigError, FamilyCFOError

FIELD_TYPES = {
    "text": 1,
    "number": 2,
    "single_select": 3,
    "date": 5,
    "checkbox": 7,
    "url": 15,
}


@dataclass
class FieldSpec:
    name: str
    type: str = "text"
    property: dict[str, Any] | None = None

    def as_api_field(self) -> dict:
        payload: dict[str, Any] = {"field_name": self.name, "type": FIELD_TYPES[self.type]}
        if self.property:
            payload["property"] = self.property
        return payload


@dataclass
class TableSpec:
    key: str
    name: str
    entity: str
    primary_field: str
    fields: list[FieldSpec] = field(default_factory=list)
    # "month" -> rows are partitioned by a YYYY-MM field inside one table;
    # the sync service maintains per-month views and archives old months
    # to local exports so the remote table never hits its row cap.
    periodic: str | None = None

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def api_fields(self) -> list[dict]:
        return [f.as_api_field() for f in self.fields]


DEFAULT_TABLES: dict[str, dict] = {
    "transactions": {
        "name": "FamilyCFO_交易明细",
        "entity": "transaction",
        "primary_field": "业务ID",
        "periodic": "month",
        "fields": [
            ("业务ID", "text"),
            ("日期", "date"),
            ("方向", "text"),
            ("类型", "text"),
            ("金额", "number"),
            ("币种", "text"),
            ("分类", "text"),
            ("账户", "text"),
            ("对方", "text"),
            ("说明", "text"),
            ("状态", "text"),
            ("来源", "text"),
            ("纳入统计", "checkbox"),
            ("月份", "text"),
        ],
    },
    "assets": {
        "name": "FamilyCFO_资产",
        "entity": "asset",
        "primary_field": "业务ID",
        "fields": [
            ("业务ID", "text"),
            ("名称", "text"),
            ("类型", "text"),
            ("估值", "number"),
            ("成本", "number"),
            ("流动资产", "checkbox"),
            ("自住房", "checkbox"),
            ("估值日期", "date"),
            ("估值来源", "text"),
        ],
    },
    "liabilities": {
        "name": "FamilyCFO_负债",
        "entity": "liability",
        "primary_field": "业务ID",
        "fields": [
            ("业务ID", "text"),
            ("名称", "text"),
            ("类型", "text"),
            ("余额", "number"),
            ("利率", "number"),
            ("月供", "number"),
            ("下次还款日", "date"),
            ("剩余月数", "number"),
        ],
    },
    "budgets": {
        "name": "FamilyCFO_预算",
        "entity": "budget_item",
        "primary_field": "业务ID",
        "fields": [
            ("业务ID", "text"),
            ("账期", "text"),
            ("版本", "number"),
            ("分类", "text"),
            ("预算金额", "number"),
            ("实际金额", "number"),
            ("执行率", "number"),
            ("预算ID", "text"),
        ],
    },
    "goals": {
        "name": "FamilyCFO_目标",
        "entity": "goal",
        "primary_field": "业务ID",
        "fields": [
            ("业务ID", "text"),
            ("名称", "text"),
            ("类型", "text"),
            ("目标金额", "number"),
            ("当前金额", "number"),
            ("目标日期", "date"),
            ("每月储蓄", "number"),
            ("缺口", "number"),
            ("完成率", "number"),
            ("风险承受", "text"),
        ],
    },
    "monthly_reports": {
        "name": "FamilyCFO_月度报告",
        "entity": "monthly_report",
        "primary_field": "业务ID",
        "fields": [
            ("业务ID", "text"),
            ("账期", "text"),
            ("结算状态", "text"),
            ("收入", "number"),
            ("支出", "number"),
            ("结余", "number"),
            ("储蓄率", "number"),
            ("总资产", "number"),
            ("总负债", "number"),
            ("净资产", "number"),
            ("生成日期", "date"),
        ],
    },
    "annual_summaries": {
        # The app lacks the bitable table-management role and cannot create
        # tables (1254302 RolePermNotAllow); renaming is blocked too. So the
        # annual roll-up reuses the existing empty "数据表", adding the
        # year-specific columns alongside its original "文本" column.
        #
        # NOTE: annual_summaries and category_annual_summaries share this one
        # physical table, so they must declare the SAME field set; rows are
        # told apart by the 业务ID prefix ("annual:" vs "category_annual:").
        "name": "数据表",
        "entity": "annual_summary",
        "primary_field": "业务ID",
        "fields": [
            ("业务ID", "text"),
            ("年份", "text"),
            ("分类", "text"),
            ("笔数", "number"),
            ("收入", "number"),
            ("支出", "number"),
            ("结余", "number"),
            ("储蓄率", "number"),
            ("总资产", "number"),
            ("总负债", "number"),
            ("净资产", "number"),
            ("金额", "number"),
            ("占比", "number"),
            ("生成日期", "date"),
        ],
    },
    "category_annual_summaries": {
        "name": "数据表",
        "entity": "category_annual_summary",
        "primary_field": "业务ID",
        "fields": [
            ("业务ID", "text"),
            ("年份", "text"),
            ("分类", "text"),
            ("笔数", "number"),
            ("收入", "number"),
            ("支出", "number"),
            ("结余", "number"),
            ("储蓄率", "number"),
            ("总资产", "number"),
            ("总负债", "number"),
            ("净资产", "number"),
            ("金额", "number"),
            ("占比", "number"),
            ("生成日期", "date"),
        ],
    },
}


def load_schema(project_root: Path) -> dict[str, TableSpec]:
    path = project_root / "config" / "feishu_tables.yaml"
    raw: dict[str, Any]
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"飞书表结构配置解析失败: {path}", detail=str(exc)) from exc
    else:
        raw = {}
    table_defs = raw.get("tables", raw) or {}
    specs: dict[str, TableSpec] = {}
    for key, default in DEFAULT_TABLES.items():
        definition = table_defs.get(key, {})
        fields_raw = definition.get("fields")
        if fields_raw:
            fields = [
                FieldSpec(name=f["name"], type=f.get("type", "text"), property=f.get("property"))
                if isinstance(f, dict)
                else FieldSpec(name=str(f))
                for f in fields_raw
            ]
        else:
            fields = [FieldSpec(name=name, type=ftype) for name, ftype in default["fields"]]
        specs[key] = TableSpec(
            key=key,
            name=definition.get("name") or default["name"],
            entity=definition.get("entity") or default["entity"],
            primary_field=definition.get("primary_field") or default["primary_field"],
            fields=fields,
            periodic=definition.get("periodic") or default.get("periodic"),
        )
    return specs


def ensure_remote_tables(client, specs: dict[str, TableSpec]) -> dict[str, str]:
    """Create missing tables/fields and return ``{schema_key: table_id}``.

    Periodic specs (``periodic="month"``) are skipped here: their concrete
    tables are named ``<base>_<YYYY-MM>`` and are created on demand by the
    sync service when a month actually has rows.
    """
    from ...core.logging import get_logger

    log = get_logger("feishu.schema")
    existing = {t.get("name"): t for t in client.list_tables()}
    result: dict[str, str] = {}
    for key, spec in specs.items():
        table = existing.get(spec.name)
        if table is None:
            if spec.periodic:
                # Try to create it, but tolerate a permission denial: the
                # production app lacks the bitable table-management role
                # (1254302 RolePermNotAllow) and periodic tables are expected
                # to exist already, having been made in the Feishu UI.
                try:
                    created = client.create_table(spec.name, spec.api_fields())
                    table_id = created.get("table_id") or created.get("id")
                    log.info("created bitable %s (%s)", spec.name, table_id)
                    if table_id:
                        result[key] = table_id
                        existing[spec.name] = {"table_id": table_id, "name": spec.name}
                    continue
                except FamilyCFOError as exc:
                    code = getattr(exc, "api_code", None)
                    if code == 1254302:
                        log.warning(
                            "no table-management role for %s; create in the Feishu UI",
                            spec.name,
                        )
                        continue
                    raise
            created = client.create_table(spec.name, spec.api_fields())
            table_id = created.get("table_id") or created.get("id")
            log.info("created bitable %s (%s)", spec.name, table_id)
            if table_id:
                # Multiple specs may share one physical table (annual roll-ups);
                # refresh the snapshot so the second one reuses it.
                existing[spec.name] = {"table_id": table_id, "name": spec.name}
        else:
            table_id = table.get("table_id") or table.get("id")
            remote_fields = {f.get("field_name") for f in client.list_fields(table_id)}
            for field_spec in spec.fields:
                if field_spec.name not in remote_fields:
                    client.create_field(table_id, field_spec.as_api_field())
                    log.info("created field %s in %s", field_spec.name, spec.name)
        if not table_id:
            raise ConfigError(f"无法获取飞书数据表 ID: {spec.name}")
        result[key] = table_id
    return result
