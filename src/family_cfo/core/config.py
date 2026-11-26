"""Configuration loading.

Precedence (lowest to highest):
    1. built-in defaults
    2. ``config/config.yaml`` (or ``$FAMILY_CFO_CONFIG``)
    3. environment variables / ``.env``
    4. explicit overrides passed to :func:`load_config`

Secrets live only in the environment / ``.env`` and are never written to the
database or logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from .errors import ConfigError
from .money import DEFAULT_CURRENCY, parse_money

ENV_PREFIX = "FAMILY_CFO_"
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def find_project_root(start: Path | None = None) -> Path:
    """Locate the project root (contains pyproject.toml) without hardcoding."""
    env_root = os.environ.get(f"{ENV_PREFIX}ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "pyproject.toml").exists() or (candidate / "skills").is_dir():
            return candidate
    return cur


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    db_path: Path
    household_name: str = "我家"
    currency: str = DEFAULT_CURRENCY
    timezone: str = "Asia/Shanghai"
    review_threshold: Decimal = Decimal("200")
    dedupe_window_days: int = 2
    transfer_window_days: int = 3
    auto_backup: bool = True
    feishu_app_id: str = field(default="", repr=False)
    feishu_app_secret: str = field(default="", repr=False)
    feishu_base_url: str = "https://open.feishu.cn"
    feishu_app_token: str = field(default="", repr=False)
    feishu_table_prefix: str = "FamilyCFO_"
    feishu_timeout: int = 20
    feishu_max_retries: int = 3
    feishu_rate_limit: int = 10
    config_file: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ paths
    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def failed_dir(self) -> Path:
        return self.data_dir / "failed"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def config_dir(self) -> Path:
        return self.project_root / "config"

    @property
    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret and self.feishu_app_token)

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.inbox_dir,
            self.processed_dir,
            self.archive_dir,
            self.failed_dir,
            self.reports_dir,
            self.backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                # Financial data is private to the account running the tool.
                path.chmod(0o700)
            except OSError:  # pragma: no cover - exotic filesystems
                pass

    def with_overrides(self, **kwargs: Any) -> "Settings":
        return replace(self, **kwargs)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件解析失败: {path}", detail=str(exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件必须是映射结构: {path}")
    return data


def _resolve_path(root: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def load_config(
    *,
    root: Path | None = None,
    config_file: Path | None = None,
    env: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    project_root = (root or find_project_root()).resolve()
    env_map = dict(os.environ)
    dotenv_path = project_root / ".env"
    if dotenv_path.exists():
        # .env fills gaps but never overrides an explicitly exported variable.
        for key, value in dotenv_values(dotenv_path).items():
            if value is not None and key not in env_map:
                env_map[key] = value
    if env is not None:
        env_map.update({k: str(v) for k, v in env.items()})

    cfg_path = Path(
        env_map.get(f"{ENV_PREFIX}CONFIG")
        or config_file
        or (project_root / "config" / "config.yaml")
    )
    if not cfg_path.is_absolute():
        cfg_path = (project_root / cfg_path).resolve()
    yaml_cfg = _read_yaml(cfg_path)

    def pick(key: str, default: Any, *, env_key: str | None = None) -> Any:
        env_name = env_key or f"{ENV_PREFIX}{key.upper()}"
        if env_name in env_map and str(env_map[env_name]).strip() != "":
            return env_map[env_name]
        if key in yaml_cfg:
            return yaml_cfg[key]
        section = yaml_cfg.get("family_cfo", {})
        if isinstance(section, dict) and key in section:
            return section[key]
        return default

    data_dir = _resolve_path(project_root, pick("data_dir", "./data"))
    db_path = _resolve_path(project_root, pick("db_path", str(data_dir / "family_cfo.db")))
    raw = dict(yaml_cfg)

    overrides = overrides or {}
    values: dict[str, Any] = {
        "project_root": project_root,
        "data_dir": data_dir,
        "db_path": db_path,
        "household_name": str(pick("household", "我家")),
        "currency": str(pick("currency", DEFAULT_CURRENCY)).upper(),
        "timezone": str(pick("timezone", "Asia/Shanghai")),
        "review_threshold": parse_money(pick("review_threshold", "200")),
        "dedupe_window_days": _as_int(pick("dedupe_window_days", 2), 2),
        "transfer_window_days": _as_int(pick("transfer_window_days", 3), 3),
        "auto_backup": _as_bool(pick("auto_backup", True), True),
        "feishu_app_id": str(env_map.get(f"{ENV_PREFIX}FEISHU_APP_ID", "")).strip(),
        "feishu_app_secret": str(env_map.get(f"{ENV_PREFIX}FEISHU_APP_SECRET", "")).strip(),
        "feishu_base_url": str(
            env_map.get(f"{ENV_PREFIX}FEISHU_BASE_URL")
            or pick("feishu_base_url", "https://open.feishu.cn")
        ).rstrip("/"),
        "feishu_app_token": str(env_map.get(f"{ENV_PREFIX}FEISHU_APP_TOKEN", "")).strip(),
        "feishu_table_prefix": str(pick("feishu_table_prefix", "FamilyCFO_")),
        "feishu_timeout": _as_int(pick("feishu_timeout", 20), 20),
        "feishu_max_retries": _as_int(pick("feishu_max_retries", 3), 3),
        "feishu_rate_limit": _as_int(pick("feishu_rate_limit", 10), 10),
        "config_file": cfg_path if cfg_path.exists() else None,
        "raw": raw,
    }
    for key, value in overrides.items():
        if value is not None:
            values[key] = value
    settings = Settings(**values)
    if settings.currency != settings.currency.upper():
        raise ConfigError("currency 必须是大写 ISO 代码")
    return settings


def redact_config(settings: Settings) -> dict[str, Any]:
    return {
        "project_root": str(settings.project_root),
        "data_dir": str(settings.data_dir),
        "db_path": str(settings.db_path),
        "household": settings.household_name,
        "currency": settings.currency,
        "timezone": settings.timezone,
        "review_threshold": str(settings.review_threshold),
        "dedupe_window_days": settings.dedupe_window_days,
        "transfer_window_days": settings.transfer_window_days,
        "feishu": {
            "configured": settings.feishu_configured,
            "app_id": "***" if settings.feishu_app_id else "",
            "app_token": "***" if settings.feishu_app_token else "",
            "base_url": settings.feishu_base_url,
            "app_secret": "***" if settings.feishu_app_secret else "",
        },
    }
