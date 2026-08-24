"""Small structured logging adapter with concise operator output."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any


class EventFormatter(logging.Formatter):
    """Format event fields as stable ``key=value`` pairs."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        event = getattr(record, "event", None)
        fields = getattr(record, "fields", {})
        suffix = _format_fields(fields) if isinstance(fields, Mapping) else ""
        event_prefix = f" [{event}]" if event else ""
        return f"{self.formatTime(record, '%H:%M:%S')}{event_prefix} {message}{suffix}"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the package logger once and return it."""
    logger = logging.getLogger("argus_patrol")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(EventFormatter())
        logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log a stable event name plus structured fields without sensitive values."""
    event = message.lower().replace(" ", "_").replace(".", "")
    logger.log(level, message, extra={"event": event, "fields": fields})


def _format_fields(fields: Mapping[str, Any]) -> str:
    if not fields:
        return ""
    return " " + " ".join(f"{key}={value!r}" for key, value in sorted(fields.items()))
