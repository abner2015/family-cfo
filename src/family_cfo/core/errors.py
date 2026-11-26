"""Errors raised by Family CFO.

All errors carry a stable machine-readable ``code`` so the CLI / Skill can map
them to actionable messages without parsing prose.
"""

from __future__ import annotations


class FamilyCFOError(Exception):
    code = "family_cfo_error"
    exit_code = 1

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ConfigError(FamilyCFOError):
    code = "config_error"


class ValidationError(FamilyCFOError):
    code = "validation_error"
    exit_code = 2


class MoneyError(ValidationError):
    code = "money_error"


class ImportError_(FamilyCFOError):
    code = "import_error"


class UnknownFormatError(ImportError_):
    code = "unknown_format"


class DuplicateFileError(ImportError_):
    code = "duplicate_file"


class MappingRequiredError(ImportError_):
    """Raised when a bill cannot be parsed without a user-confirmed column map."""

    code = "mapping_required"

    def __init__(self, message: str, *, suggestion: dict | None = None) -> None:
        super().__init__(message)
        self.suggestion = suggestion or {}

    def as_dict(self) -> dict:
        data = super().as_dict()
        data["suggestion"] = self.suggestion
        return data


class ReconciliationError(FamilyCFOError):
    code = "reconciliation_error"


class SettlementError(FamilyCFOError):
    code = "settlement_error"


class BackupError(FamilyCFOError):
    code = "backup_error"


class FeishuError(FamilyCFOError):
    code = "feishu_error"


class FeishuNotConfigured(FeishuError):
    code = "feishu_not_configured"


class FeishuAuthError(FeishuError):
    code = "feishu_auth_error"


class FeishuApiError(FeishuError):
    code = "feishu_api_error"

    def __init__(self, message: str, *, api_code: int | None = None, detail: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.api_code = api_code


class ConflictError(FamilyCFOError):
    code = "conflict"


def exit_code_for(exc: BaseException) -> int:
    if isinstance(exc, FamilyCFOError):
        return exc.exit_code
    return 1
