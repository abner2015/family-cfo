"""Saved column-mapping profiles.

Profiles live in ``config/mappings.yaml``::

    mappings:
      cmb_credit:
        platform: bank
        label: 招商银行信用卡流水
        mapping:
          date: 交易日期
          amount: 人民币金额
          direction: 借贷标志
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..core.errors import ConfigError, ValidationError
from .columns import suggest_mapping


def mappings_path(project_root: Path) -> Path:
    return project_root / "config" / "mappings.yaml"


def load_profiles(project_root: Path) -> dict[str, dict[str, Any]]:
    path = mappings_path(project_root)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"映射配置解析失败: {path}", detail=str(exc)) from exc
    mappings = data.get("mappings", data)
    if not isinstance(mappings, dict):
        raise ConfigError(f"映射配置格式错误: {path}")
    return mappings


def get_profile(project_root: Path, name: str) -> dict[str, Any]:
    profiles = load_profiles(project_root)
    if name not in profiles:
        raise ValidationError(
            f"未找到映射配置: {name}",
            detail=f"可用: {', '.join(sorted(profiles)) or '(无)'}",
        )
    profile = profiles[name]
    if "mapping" not in profile:
        raise ConfigError(f"映射配置 {name} 缺少 mapping 段")
    return profile


def save_profile(
    project_root: Path,
    name: str,
    mapping: dict[str, str],
    *,
    platform: str = "other",
    label: str | None = None,
) -> Path:
    path = mappings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {"mappings": {}}
    data = data or {"mappings": {}}
    data.setdefault("mappings", {})[name] = {
        "platform": platform,
        "label": label or name,
        "mapping": mapping,
    }
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def suggestion_for_path(headers: list[str]) -> dict[str, Any]:
    return suggest_mapping(headers)
