"""Column synonyms and mapping suggestions for non-standard bills."""

from __future__ import annotations

import re
from typing import Any, Iterable

FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": (
        "交易时间", "交易日期", "记账日期", "入账日期", "发生时间", "创建时间", "付款时间",
        "交易创建时间", "日期", "time", "date", "transaction date", "posted date",
    ),
    "amount": ("金额(元)", "金额（元）", "交易金额", "发生额", "金额", "amount", "amt"),
    "inflow": ("收入金额", "收入", "贷方发生额", "贷方金额", "存入", "credit", "inflow"),
    "outflow": ("支出金额", "支出", "借方发生额", "借方金额", "支取", "debit", "outflow"),
    "direction": ("收/支", "收支", "借贷标志", "交易方向", "资金方向", "收付标志", "方向"),
    "counterparty": (
        "交易对方", "对方户名", "对方账户名", "对方名称", "商户名称", "收款方", "付款方",
        "counterparty", "merchant", "payee",
    ),
    "description": (
        "商品", "商品名称", "商品说明", "摘要", "用途", "备注", "交易说明", "memo",
        "description", "remark",
    ),
    "external_id": (
        "交易单号", "交易号", "交易流水号", "流水号", "凭证号", "交易订单号", "商户单号",
        "transaction id", "reference",
    ),
    "status": ("当前状态", "交易状态", "状态", "status"),
    "currency": ("币种", "货币", "currency"),
    "type": ("交易类型", "类型", "交易分类", "业务类型", "type"),
    "balance": ("余额", "账户余额", "balance"),
    "fee": ("服务费（元）", "服务费(元)", "手续费", "fee"),
}


def normalize_header(header: str) -> str:
    text = str(header).strip().lower()
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"[\s_\-]+", "", text)
    return text


def _variants(name: str) -> list[str]:
    norm = normalize_header(name)
    variants = {norm}
    # "金额(元)" also matches "金额"
    variants.add(re.sub(r"\(.*?\)", "", norm))
    return [v for v in variants if v]


def find_column(headers: Iterable[str], field: str) -> str | None:
    header_list = [str(h) for h in headers]
    normalized = [(h, normalize_header(h)) for h in header_list]
    for synonym in FIELD_SYNONYMS.get(field, ()):
        for variant in _variants(synonym):
            for header, norm in normalized:
                if norm == variant:
                    return header
    for synonym in FIELD_SYNONYMS.get(field, ()):
        for variant in _variants(synonym):
            if len(variant) < 2:
                continue
            for header, norm in normalized:
                if variant in norm:
                    return header
    return None


def find_columns(headers: Iterable[str], field: str) -> list[str]:
    """All columns whose header matches a field's synonyms (for amount parsing)."""
    header_list = [str(h) for h in headers]
    matches: list[str] = []
    for synonym in FIELD_SYNONYMS.get(field, ()):
        for variant in _variants(synonym):
            for header in header_list:
                if variant in normalize_header(header) and header not in matches:
                    matches.append(header)
    return matches


def get(record: dict[str, Any], column: str | None) -> str:
    if not column:
        return ""
    value = record.get(column, "")
    if value is None:
        return ""
    return str(value).strip()


def suggest_mapping(headers: Iterable[str]) -> dict[str, Any]:
    """Suggest a column mapping and report what is still missing."""
    headers = list(headers)
    mapping: dict[str, str | None] = {}
    for field in ("date", "amount", "inflow", "outflow", "direction", "counterparty",
                  "description", "external_id", "status", "currency", "type"):
        mapping[field] = find_column(headers, field)
    has_amount = bool(mapping["amount"] or mapping["inflow"] or mapping["outflow"])
    missing = []
    if not mapping["date"]:
        missing.append("date")
    if not has_amount:
        missing.append("amount")
    return {
        "mapping": {k: v for k, v in mapping.items() if v},
        "headers": headers,
        "missing": missing,
        "ready": not missing,
    }


def apply_mapping(record: dict[str, Any], mapping: dict[str, str | None]) -> dict[str, str]:
    return {field: get(record, column) for field, column in mapping.items() if column}
