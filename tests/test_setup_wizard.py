from __future__ import annotations

from pathlib import Path

from voiceagent.config import load_config
from voiceagent.setup_wizard import build_config, run_wizard


def _answers(**over: object) -> dict[str, object]:
    a: dict[str, object] = {
        "device_name": "ha-panel",
        "room": "office",
        "ws_base_url": "wss://realtime.example/v1",
        "model_name": "local",
        "realtime_api_key": "sk-abc",
        "wakeword_model": "models/wakeword/hey_panel.onnx",
        "wake_sound": "",
        "pulse_default": True,
        "sendspin_enabled": True,
        "sendspin_name": "ha-panel-voice",
        "ha_enabled": True,
        "ha_base_url": "https://ha.example",
        "ha_token": "ha-tok",
        "ha_entity": "media_player.ha_panel_voice",
    }
    a.update(over)
    return a


def test_build_config_structure_and_secrets() -> None:
    config, secrets = build_config(_answers(), xvf_host_path="/opt/xvf_host")
    assert config["device"]["name"] == "ha-panel"
    assert config["realtime"]["ws_base_url"] == "wss://realtime.example/v1"
    assert config["realtime"]["base_url"] == "https://realtime.example/v1"  # ws->http
    assert config["wakeword"]["models"] == ["models/wakeword/hey_panel.onnx"]
    # pulse default => CH0-only capture
    assert config["audio"]["capture_channels"] == 2
    assert config["audio"]["capture_pick_channel"] == 0
    assert config["audio"]["capture_device"] == "default"
    assert config["respeaker"]["xvf_host_path"] == "/opt/xvf_host"
    assert config["media"]["on_turn"] == "duck"
    assert config["media"]["sendspin"]["enabled"] is True
    assert config["media"]["home_assistant"]["media_player_entity"] == "media_player.ha_panel_voice"
    # secrets are separated out, never in config
    assert secrets["VOICEAGENT_REALTIME__API_KEY"] == "sk-abc"
    assert secrets["VOICEAGENT_MEDIA__HOME_ASSISTANT__TOKEN"] == "ha-tok"
    assert "sk-abc" not in str(config)
    assert "ha-tok" not in str(config)


def test_build_config_no_ha_no_sendspin() -> None:
    config, secrets = build_config(
        _answers(ha_enabled=False, sendspin_enabled=False, realtime_api_key=""),
        xvf_host_path="xvf_host",
    )
    assert "media" not in config
    assert secrets == {}


def test_run_wizard_writes_valid_config(tmp_path: Path) -> None:
    responses = iter([
        "ha-panel", "office", "wss://realtime.example/v1", "local", "sk-abc",
        "models/wakeword/hey_panel.onnx", "",  # wake sound
        "y",  # pulse default
        "y", "ha-panel-voice",  # sendspin
        "y", "https://ha.example", "ha-tok", "media_player.ha_panel_voice",  # HA
    ])
    cfg = tmp_path / "config.yaml"
    sec = tmp_path / "secrets.env"
    run_wizard(
        config_path=cfg, secrets_path=sec, xvf_host_path="/opt/xvf_host",
        default_model="alexa", input_fn=lambda _p: next(responses), force=True,
    )
    assert cfg.exists() and sec.exists()
    assert oct(sec.stat().st_mode)[-3:] == "600"
    assert "VOICEAGENT_REALTIME__API_KEY=sk-abc" in sec.read_text()
    assert "sk-abc" not in cfg.read_text()  # secret not in config
    # and the written config loads + validates
    loaded = load_config(cfg)
    assert loaded.device.name == "ha-panel"
    assert loaded.media.on_turn == "duck"
