"""Structured logging with hard secret redaction.

release_rules §17 and §22: secrets must never reach logs, and logs must stay
useful across releases. Redaction is a logging filter rather than a convention
at each call site, because a convention only has to be forgotten once.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

# Patterns that must never survive into a log line. Ordered cheapest-first.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Our own API keys
    (re.compile(r"\bem_(live|test)_[A-Za-z0-9]+"), "em_***"),
    # MXRoute API keys: Mx + hex + K1
    (re.compile(r"\bMx[0-9a-fA-F]{28,}K\d\b"), "Mx***"),
    # Fernet keys: 43 urlsafe-base64 chars plus '='. A trailing \b cannot match
    # here -- '=' is a non-word character, so at end-of-string there is no
    # boundary to find. Use explicit lookarounds instead.
    (
        re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=-])"),
        "***key***",
    ),
    # Anything that looks like a DSN password
    (re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)"), r"\1***\2"),
    # Explicit key=value secrets
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)" r"(\s*[=:]\s*)(\S+)"
        ),
        r"\1\2***",
    ),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer ***"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact_value(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_redact_value(a) for a in record.args)
        return True


def _redact_value(value: object) -> object:
    return redact(value) if isinstance(value, str) else value


class JsonFormatter(logging.Formatter):
    """Logs to stdout as JSON (release_rules §19: logs via standard streams)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["error"] = redact(self.formatException(record.exc_info))
        for key, value in getattr(record, "context", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # SQLAlchemy echo would put bodies and credentials into logs. Keep it off
    # regardless of root level.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # httpx logs a line per request at INFO, which buries CLI output and adds
    # nothing we do not already log ourselves with more context.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
