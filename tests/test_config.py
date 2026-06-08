from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from voiceagent.config import Settings, load_config, resolve_config_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any VOICEAGENT_/VA_ env so tests are deterministic."""
    import os

    for key in list(os.environ):
        if key.startswith("VOICEAGENT_") or key == "VA_CONFIG":
            monkeypatch.delenv(key, raising=False)


def test_defaults_load_without_file() -> None:
    cfg = load_config(None)
    assert cfg.device.name == "voice-assistant"
    assert cfg.realtime.port == 8765
    assert cfg.realtime.native_16k is False
    assert cfg.audio.capture_rate == 16000
    assert cfg.logging.level == "INFO"


def test_yaml_overrides_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "device:\n  name: kitchen\nrealtime:\n  host: 10.0.0.5\n  port: 9000\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.device.name == "kitchen"
    assert cfg.realtime.host == "10.0.0.5"
    assert cfg.realtime.port == 9000
    # untouched keys keep defaults
    assert cfg.audio.backend == "pipewire"


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("realtime:\n  host: 10.0.0.5\n  port: 9000\n")
    monkeypatch.setenv("VOICEAGENT_REALTIME__HOST", "192.168.1.10")
    cfg = load_config(cfg_file)
    assert cfg.realtime.host == "192.168.1.10"  # env wins
    assert cfg.realtime.port == 9000  # yaml still applies where env is silent


def test_secret_is_redacted_in_dump(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("realtime:\n  api_key: super-secret-value\n")
    cfg = load_config(cfg_file)
    assert isinstance(cfg.realtime.api_key, SecretStr)
    assert cfg.realtime.api_key.get_secret_value() == "super-secret-value"
    dumped = cfg.model_dump(mode="json")
    assert "super-secret-value" not in str(dumped)
    assert dumped["realtime"]["api_key"] == "**********"


def test_invalid_value_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("audio:\n  duck_level: 5.0\n")  # > 1.0
    with pytest.raises(ValidationError):
        load_config(cfg_file)


def test_invalid_log_level_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("logging:\n  level: LOUD\n")
    with pytest.raises(ValidationError):
        load_config(cfg_file)


def test_log_level_is_uppercased(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("logging:\n  level: debug\n")
    assert load_config(cfg_file).logging.level == "DEBUG"


def test_unknown_key_is_forbidden(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("realtime:\n  nonsense: 1\n")
    with pytest.raises(ValidationError):
        load_config(cfg_file)


def test_missing_explicit_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        resolve_config_path("/nonexistent/path/config.yaml")


def test_va_config_env_selects_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_file = tmp_path / "via_env.yaml"
    cfg_file.write_text("device:\n  name: via-env\n")
    monkeypatch.setenv("VA_CONFIG", str(cfg_file))
    assert resolve_config_path(None) == cfg_file
    assert load_config(None).device.name == "via-env"


def test_example_config_is_valid() -> None:
    """The shipped example must always validate."""
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load_config(example)
    assert isinstance(cfg, Settings)
    assert cfg.device.name == "living-room"
