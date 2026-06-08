"""structlog configuration with secret redaction.

Two output modes: ``console`` (human-readable, colorized when a TTY) and ``json``
(one object per line, for log shipping). A redaction processor masks values whose
key looks sensitive, as a defense-in-depth backstop on top of ``SecretStr`` — so a
token that slips into a log event as a plain string is still masked.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from voiceagent.config import LoggingConfig

# Substrings that mark a key as sensitive (case-insensitive).
_SENSITIVE_HINTS: tuple[str, ...] = ("token", "api_key", "apikey", "secret", "password")
_REDACTED = "***redacted***"


def _redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict.keys()):
        lowered = key.lower()
        if any(hint in lowered for hint in _SENSITIVE_HINTS):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(cfg: LoggingConfig) -> None:
    """Configure structlog + stdlib logging from the resolved config."""
    level = logging.getLevelName(cfg.level)
    if not isinstance(level, int):  # getLevelName returns a str for unknown names
        level = logging.INFO

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
    ]

    renderer: Processor
    if cfg.format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
