"""structlog configuration with secret redaction.

Two output modes: ``console`` (human-readable, colorized when a TTY) and ``json``
(one object per line, for log shipping). A redaction processor masks values whose
key looks sensitive, as a defense-in-depth backstop on top of ``SecretStr`` — so a
token that slips into a log event as a plain string is still masked.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
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


def _renderer(fmt: str, *, colors: bool) -> Processor:
    if fmt == "json":
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=colors)


def _formatter(fmt: str, shared: list[Processor], *, colors: bool) -> logging.Formatter:
    """A stdlib formatter that renders structlog records (and stdlib ones, via the
    foreign pre-chain) so multiple handlers can each use their own format."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _renderer(fmt, colors=colors),
        ],
    )


def configure_logging(cfg: LoggingConfig) -> None:
    """Configure structlog + stdlib logging from the resolved config.

    Routes structlog through stdlib logging so output can fan out to multiple
    handlers — stdout (journald) plus an optional rotating structured log file,
    each with its own format.
    """
    level = logging.getLevelName(cfg.level)
    if not isinstance(level, int):  # getLevelName returns a str for unknown names
        level = logging.INFO

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):  # idempotent: clear prior config
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_formatter(cfg.format, shared, colors=sys.stdout.isatty()))
    root.addHandler(console)

    if cfg.file:
        Path(cfg.file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(Path(cfg.file).expanduser()),
            maxBytes=cfg.file_max_bytes,
            backupCount=cfg.file_backups,
        )
        file_handler.setFormatter(_formatter(cfg.file_format, shared, colors=False))
        root.addHandler(file_handler)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
