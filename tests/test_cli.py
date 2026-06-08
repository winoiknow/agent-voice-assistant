from __future__ import annotations

from pathlib import Path

import pytest

from voiceagent.cli import main


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in list(os.environ):
        if key.startswith("VOICEAGENT_") or key == "VA_CONFIG":
            monkeypatch.delenv(key, raising=False)


def test_check_config_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("device:\n  name: bench\nrealtime:\n  api_key: shh\n")
    rc = main(["check-config", "--config", str(cfg_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "bench" in out
    assert "shh" not in out  # secret must not be printed
    assert "config source" in out


def test_check_config_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["check-config", "--config", "/no/such/file.yaml"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not found" in err


def test_check_config_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("audio:\n  duck_level: 9\n")
    rc = main(["check-config", "--config", str(cfg_file)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid configuration" in err


def test_no_subcommand_errors() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "voiceagent" in capsys.readouterr().out


def _mock_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("audio:\n  backend: mock\nrespeaker:\n  simulate: true\n")
    return cfg


def test_audio_test_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["audio-test", "--config", str(_mock_config(tmp_path))])
    assert rc == 0
    assert '"backend": "mock"' in capsys.readouterr().out


def test_led_test_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["led-test", "thinking", "--config", str(_mock_config(tmp_path))])
    assert rc == 0
    assert "thinking" in capsys.readouterr().out


def test_led_test_rejects_bad_state(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["led-test", "bogus", "--config", str(_mock_config(tmp_path))])


def test_respeaker_tune_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "respeaker:\n  simulate: true\n  tuning:\n    PP_AGCGAIN: [1]\n"
    )
    rc = main(["respeaker-tune", "--config", str(cfg)])
    assert rc == 0
    assert "PP_AGCGAIN" in capsys.readouterr().out


def test_audio_test_invalid_config(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("audio:\n  duck_level: 9\n")
    assert main(["audio-test", "--config", str(cfg)]) == 1
