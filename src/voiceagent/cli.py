"""Command-line entrypoint.

    voiceagent run [--config PATH]            Start the assistant (runs until stopped).
    voiceagent check-config [--config PATH]   Validate config and print the resolved,
                                              secret-redacted settings.
    voiceagent audio-test [--config PATH]     Capture, play back, cue, and duck demo.
    voiceagent led-test [STATE] [--config …]  Drive the LED ring (one state or all).
    voiceagent respeaker-tune [--config …]    Apply DSP tuning and read it back.
    voiceagent wake-test [-s SECONDS] [--config …]   Listen for the wake word.
    voiceagent realtime-test [-s SECONDS] [--config …]   One realtime conversation.
    voiceagent --version

Each command exits non-zero on a missing (2) or invalid (1) config.
The hardware commands run against mock backends when audio.backend: mock and
respeaker.simulate: true.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from voiceagent import __version__
from voiceagent.app import run_app
from voiceagent.config import Settings, load_config, resolve_config_path
from voiceagent.diagnostics import (
    run_arbitration_test,
    run_audio_test,
    run_led_test,
    run_media_test,
    run_realtime_test,
    run_respeaker_tune,
    run_wake_test,
)
from voiceagent.logging_setup import configure_logging
from voiceagent.respeaker import LedState

_LED_STATES = [s.value for s in LedState] + ["all"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voiceagent",
        description="Headless wake-word voice assistant for ARM64 SBCs.",
    )
    parser.add_argument("--version", action="version", version=f"voiceagent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def _with_config(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--config", "-c", default=None, help="Path to config.yaml.")
        return p

    _with_config(sub.add_parser("run", help="Start the assistant."))
    _with_config(sub.add_parser("check-config", help="Validate config and print it."))
    _with_config(sub.add_parser("audio-test", help="Capture/playback/cue/duck demo."))

    led_p = _with_config(sub.add_parser("led-test", help="Drive the LED ring."))
    led_p.add_argument(
        "state", nargs="?", default="all", choices=_LED_STATES,
        help="LED state to show (default: cycle all).",
    )

    _with_config(sub.add_parser("respeaker-tune", help="Apply DSP tuning and read back."))

    wake_p = _with_config(sub.add_parser("wake-test", help="Listen for the wake word."))
    wake_p.add_argument(
        "--seconds", "-s", type=float, default=20.0, help="How long to listen."
    )

    rt_p = _with_config(sub.add_parser("realtime-test", help="One realtime conversation."))
    rt_p.add_argument(
        "--seconds", "-s", type=float, default=30.0, help="How long to converse."
    )

    media_p = _with_config(sub.add_parser("media-test", help="Run sendspin for MA discovery."))
    media_p.add_argument(
        "--seconds", "-s", type=float, default=30.0, help="How long to run the daemon."
    )

    arb_p = _with_config(sub.add_parser(
        "arbitration-test", help="Multi-device wake arbitration over UDP broadcast."))
    arb_p.add_argument(
        "--seconds", "-s", type=float, default=20.0, help="How long to run."
    )

    init_p = sub.add_parser("init", help="Interactive config wizard.")
    init_p.add_argument("--config", "-c", default=None, help="config.yaml output path.")
    init_p.add_argument("--secrets", default=None, help="secrets env output path.")
    init_p.add_argument("--xvf-host-path", default="xvf_host", help="Path to the xvf_host binary.")
    init_p.add_argument("--default-model", default="alexa", help="Default wake-word model.")
    init_p.add_argument(
        "--ack-sound", default="", help="Default acknowledge .wav played when listening starts."
    )
    init_p.add_argument("--force", action="store_true", help="Overwrite existing config.")

    return parser


class _ConfigExit(Exception):
    """Raised to abort a command with a process exit code."""

    def __init__(self, code: int) -> None:
        self.code = code


def _load_or_exit(config_path: str | None) -> Settings:
    try:
        return load_config(config_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise _ConfigExit(2) from exc
    except ValidationError as exc:
        print("error: invalid configuration:", file=sys.stderr)
        print(exc, file=sys.stderr)
        raise _ConfigExit(1) from exc


def _cmd_check_config(config_path: str | None) -> int:
    resolved = None
    try:
        resolved = resolve_config_path(config_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    settings = _load_or_exit(config_path)
    source = str(resolved) if resolved else "(defaults only — no config file found)"
    print(f"config source: {source}")
    print(json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


def _cmd_run(config_path: str | None) -> int:
    settings = _load_or_exit(config_path)
    configure_logging(settings.logging)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_app(settings))
    return 0


def _cmd_audio_test(config_path: str | None) -> int:
    settings = _load_or_exit(config_path)
    configure_logging(settings.logging)
    result = asyncio.run(run_audio_test(settings))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_led_test(config_path: str | None, state: str) -> int:
    settings = _load_or_exit(config_path)
    configure_logging(settings.logging)
    result = asyncio.run(run_led_test(settings, state))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_respeaker_tune(config_path: str | None) -> int:
    settings = _load_or_exit(config_path)
    configure_logging(settings.logging)
    result = asyncio.run(run_respeaker_tune(settings))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_wake_test(config_path: str | None, seconds: float) -> int:
    settings = _load_or_exit(config_path)
    configure_logging(settings.logging)
    result = asyncio.run(run_wake_test(settings, seconds=seconds))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_realtime_test(config_path: str | None, seconds: float) -> int:
    settings = _load_or_exit(config_path)
    configure_logging(settings.logging)
    result = asyncio.run(run_realtime_test(settings, seconds=seconds))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_media_test(config_path: str | None, seconds: float) -> int:
    settings = _load_or_exit(config_path)
    configure_logging(settings.logging)
    result = asyncio.run(run_media_test(settings, seconds=seconds))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_arbitration_test(config_path: str | None, seconds: float) -> int:
    settings = _load_or_exit(config_path)
    configure_logging(settings.logging)
    result = asyncio.run(run_arbitration_test(settings, seconds=seconds))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    from pathlib import Path

    from voiceagent.config import _xdg_config_home
    from voiceagent.setup_wizard import run_wizard

    base = _xdg_config_home() / "voiceagent"
    config_path = Path(args.config) if args.config else base / "config.yaml"
    secrets_path = Path(args.secrets) if args.secrets else base / "secrets.env"
    run_wizard(
        config_path=config_path,
        secrets_path=secrets_path,
        xvf_host_path=args.xvf_host_path,
        default_model=args.default_model,
        default_wake_sound=args.ack_sound,
        force=args.force,
    )
    print(f"\nWrote {config_path}")
    if secrets_path.exists():
        print(f"Wrote {secrets_path} (secrets, mode 600)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check-config":
            return _cmd_check_config(args.config)
        if args.command == "run":
            return _cmd_run(args.config)
        if args.command == "audio-test":
            return _cmd_audio_test(args.config)
        if args.command == "led-test":
            return _cmd_led_test(args.config, args.state)
        if args.command == "respeaker-tune":
            return _cmd_respeaker_tune(args.config)
        if args.command == "wake-test":
            return _cmd_wake_test(args.config, args.seconds)
        if args.command == "realtime-test":
            return _cmd_realtime_test(args.config, args.seconds)
        if args.command == "media-test":
            return _cmd_media_test(args.config, args.seconds)
        if args.command == "arbitration-test":
            return _cmd_arbitration_test(args.config, args.seconds)
        if args.command == "init":
            return _cmd_init(args)
    except _ConfigExit as exit_:
        return exit_.code
    return 2  # unreachable: subparser is required


if __name__ == "__main__":
    raise SystemExit(main())
