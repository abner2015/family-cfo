"""Logging with mandatory redaction of financial and credential material."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

TOKEN_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")
SECRET_KV_RE = re.compile(
    r"(?i)\b(app_secret|secret|password|passwd|token|authorization|card_no|card_number)"
    r"\s*[=:]\s*([^\s,;\"']+)"
)
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
LONG_NUMBER_RE = re.compile(r"\b\d{11,}\b")


def redact(text: str) -> str:
    """Mask credentials, card numbers and long account numbers in log output."""
    if not text:
        return text
    text = TOKEN_RE.sub(r"\1***", text)
    text = SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = CARD_RE.sub(lambda m: m.group(0)[:4] + "****" + m.group(0)[-2:], text)
    text = LONG_NUMBER_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-2:], text)
    return text


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        record.msg = redact(message)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


_CONFIGURED = False


def setup_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    quiet: bool = False,
) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("family_cfo")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if _CONFIGURED:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    if not quiet:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        stream.addFilter(RedactionFilter())
        logger.addHandler(stream)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(RedactionFilter())
        logger.addHandler(file_handler)
    _CONFIGURED = True
    return logger


def get_logger(name: str = "family_cfo") -> logging.Logger:
    return logging.getLogger(name if name.startswith("family_cfo") else f"family_cfo.{name}")
