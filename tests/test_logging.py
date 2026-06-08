from __future__ import annotations

from voiceagent.config import LoggingConfig
from voiceagent.logging_setup import _REDACTED, _redact_secrets, configure_logging


def test_redacts_sensitive_keys() -> None:
    event = {
        "event": "connect",
        "api_key": "abc123",
        "ha_token": "tok",
        "password": "p",
        "host": "10.0.0.1",
    }
    out = _redact_secrets(None, "info", dict(event))
    assert out["api_key"] == _REDACTED
    assert out["ha_token"] == _REDACTED
    assert out["password"] == _REDACTED
    assert out["host"] == "10.0.0.1"  # non-sensitive untouched
    assert out["event"] == "connect"


def test_configure_logging_console_and_json_do_not_raise() -> None:
    configure_logging(LoggingConfig(level="DEBUG", format="console"))
    configure_logging(LoggingConfig(level="INFO", format="json"))
