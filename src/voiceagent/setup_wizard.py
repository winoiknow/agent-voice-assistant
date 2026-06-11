"""Interactive configuration wizard (``voiceagent init``).

Writes a ``config.yaml`` plus a separate secrets env file (API key + HA token),
validated against the config model. Secrets never land in ``config.yaml`` — they're
written to an env file (mode 600) that the systemd unit loads, and read at runtime
via the ``VOICEAGENT_…`` env override mechanism.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from voiceagent.config import Settings

InputFn = Callable[[str], str]


def _ask(prompt: str, default: str = "", *, input_fn: InputFn) -> str:
    suffix = f" [{default}]" if default else ""
    value = input_fn(f"{prompt}{suffix}: ").strip()
    return value or default


def _ask_bool(prompt: str, default: bool, *, input_fn: InputFn) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input_fn(f"{prompt} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def _ws_to_http(url: str) -> str:
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url


def build_config(
    answers: dict[str, Any], *, xvf_host_path: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Turn wizard answers into (config dict, secrets env dict). Pure + testable."""
    config: dict[str, Any] = {
        "device": {"name": answers["device_name"], "room": answers.get("room") or None},
        "realtime": {
            "ws_base_url": answers["ws_base_url"],
            "base_url": _ws_to_http(answers["ws_base_url"]),
            "model": answers.get("model_name", "local"),
            "instructions": (
                "You are a helpful far-field voice assistant on a small device in "
                "{room}. Replies are spoken, so keep them brief and conversational."
            ),
        },
        "wakeword": {"models": [answers["wakeword_model"]]},
        "respeaker": {"enabled": True, "simulate": False, "xvf_host_path": xvf_host_path},
        "logging": {"level": "INFO"},
    }
    if answers.get("wake_sound"):
        config["wakeword"]["wake_sound"] = answers["wake_sound"]

    if answers.get("pulse_default", True):
        config["audio"] = {
            "backend": "pipewire",
            "capture_device": "default",
            "playback_device": "default",
            "capture_channels": 2,  # XVF3800 stereo -> keep CH0 only (AEC'd)
            "capture_pick_channel": 0,
            "playback_rate": 16000,
        }
    else:
        config["audio"] = {
            "backend": "alsa",
            "capture_device": answers.get("capture_device") or None,
            "playback_device": answers.get("playback_device") or None,
        }

    media: dict[str, Any] = {}
    if answers.get("sendspin_enabled"):
        media["sendspin"] = {
            "enabled": True,
            "name": answers.get("sendspin_name") or answers["device_name"],
            "audio_device": "default",
            "hardware_volume": False,
        }
    if answers.get("ha_enabled"):
        media["home_assistant"] = {
            "enabled": True,
            "base_url": answers["ha_base_url"],
            "media_player_entity": answers["ha_entity"],
        }
        media["on_turn"] = "duck"
    if media:
        config["media"] = media

    secrets: dict[str, str] = {}
    if answers.get("realtime_api_key"):
        secrets["VOICEAGENT_REALTIME__API_KEY"] = answers["realtime_api_key"]
    if answers.get("ha_enabled") and answers.get("ha_token"):
        secrets["VOICEAGENT_MEDIA__HOME_ASSISTANT__TOKEN"] = answers["ha_token"]
    return config, secrets


def collect_answers(
    default_model: str, *, default_wake_sound: str = "", input_fn: InputFn
) -> dict[str, Any]:
    a: dict[str, Any] = {}
    a["device_name"] = _ask("Device name", socket.gethostname(), input_fn=input_fn)
    a["room"] = _ask("Room (for the prompt, optional)", input_fn=input_fn)
    a["ws_base_url"] = _ask(
        "speech2speech WebSocket base URL", "wss://realtime.anteon.group/v1", input_fn=input_fn
    )
    a["model_name"] = _ask("Realtime model name", "local", input_fn=input_fn)
    a["realtime_api_key"] = _ask(
        "speech2speech API key (blank if none / already set)", input_fn=input_fn
    )
    a["wakeword_model"] = _ask(
        "Wake-word model (name or .onnx path)", default_model, input_fn=input_fn
    )
    a["wake_sound"] = _ask(
        "Acknowledge .wav played when listening starts", default_wake_sound, input_fn=input_fn
    )
    a["pulse_default"] = _ask_bool(
        "Capture/play via PulseAudio 'default' (recommended on a desktop/pulse SBC)",
        True, input_fn=input_fn,
    )
    if not a["pulse_default"]:
        a["capture_device"] = _ask("Capture device (name/index)", input_fn=input_fn)
        a["playback_device"] = _ask("Playback device (name/index)", input_fn=input_fn)

    a["sendspin_enabled"] = _ask_bool("Run the sendspin music player?", True, input_fn=input_fn)
    if a["sendspin_enabled"]:
        a["sendspin_name"] = _ask("sendspin player name", a["device_name"], input_fn=input_fn)
    a["ha_enabled"] = _ask_bool(
        "Control music via Home Assistant (pause/duck)?", True, input_fn=input_fn
    )
    if a["ha_enabled"]:
        a["ha_base_url"] = _ask(
            "Home Assistant base URL", "https://ha.anteon.group", input_fn=input_fn
        )
        a["ha_token"] = _ask("HA long-lived token (blank if already set)", input_fn=input_fn)
        a["ha_entity"] = _ask(
            "HA media_player entity id",
            f"media_player.{a.get('sendspin_name', a['device_name']).replace('-', '_')}",
            input_fn=input_fn,
        )
    return a


def write_outputs(
    config: dict[str, Any], secrets: dict[str, str], *, config_path: Path, secrets_path: Path
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    if secrets:
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k}={v}" for k, v in secrets.items()]
        secrets_path.write_text("\n".join(lines) + "\n")
        secrets_path.chmod(0o600)


def run_wizard(
    *,
    config_path: Path,
    secrets_path: Path,
    xvf_host_path: str,
    default_model: str = "alexa",
    default_wake_sound: str = "",
    input_fn: InputFn = input,
    force: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    if config_path.exists() and not force and not _ask_bool(
        f"{config_path} exists. Overwrite?", False, input_fn=input_fn
    ):
        raise SystemExit("Keeping existing config; aborting wizard.")
    answers = collect_answers(
        default_model, default_wake_sound=default_wake_sound, input_fn=input_fn
    )
    config, secrets = build_config(answers, xvf_host_path=xvf_host_path)
    # Secrets are optional fields, so the config validates on its own.
    try:
        Settings.model_validate(config)
    except ValidationError as exc:
        raise SystemExit(f"Config failed validation:\n{exc}") from exc
    write_outputs(config, secrets, config_path=config_path, secrets_path=secrets_path)
    return config, secrets
