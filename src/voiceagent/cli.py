"""Command-line entrypoint.

    voiceagent run [--config PATH]            Start the assistant (runs until stopped).
    voiceagent check-config [--config PATH]   Validate config and print the resolved,
                                              secret-redacted settings.
    voiceagent audio-test [--config PATH]     Capture, play back, cue, and duck demo.
    voiceagent led-test [STATE] [--config …]  Drive the LED ring (one state or all).
    voiceagent respeaker-tune [--config …]    Apply DSP tuning and read it back.
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
from voiceagent.diagnostics import run_audio_test, run_led_test, run_respeaker_tune
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
    except _ConfigExit as exit_:
        return exit_.code
    return 2  # unreachable: subparser is required


if __name__ == "__main__":
    raise SystemExit(main())
