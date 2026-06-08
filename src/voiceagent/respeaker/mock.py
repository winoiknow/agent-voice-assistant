"""In-memory XvfHost for development and tests: records every command."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from voiceagent.logging_setup import get_logger
from voiceagent.respeaker.base import RGB, LedEffect, XvfHost

log = get_logger("respeaker.mock")


@dataclass
class LedState:
    effect: LedEffect = LedEffect.OFF
    color: RGB = (0, 0, 0)
    brightness: int = 0
    speed: int = 0


class MockXvfHost(XvfHost):
    def __init__(self) -> None:
        self.params: dict[str, list[float]] = {}
        self.led = LedState()
        self.saved: int = 0
        # Ordered log of (command, args) for assertions.
        self.commands: list[tuple[str, tuple[object, ...]]] = []

    def _record(self, command: str, *args: object) -> None:
        self.commands.append((command, args))

    async def get_param(self, name: str) -> list[float]:
        self._record("get", name)
        return self.params.get(name, [])

    async def set_param(self, name: str, values: Sequence[float]) -> None:
        self._record("set", name, tuple(values))
        self.params[name] = list(values)
        log.debug("mock_set_param", name=name, values=list(values))

    async def save_configuration(self) -> None:
        self._record("save")
        self.saved += 1

    async def led_effect(self, effect: LedEffect) -> None:
        self._record("led_effect", effect)
        self.led.effect = effect

    async def led_color(self, rgb: RGB) -> None:
        self._record("led_color", rgb)
        self.led.color = rgb

    async def led_brightness(self, value: int) -> None:
        self._record("led_brightness", value)
        self.led.brightness = value

    async def led_speed(self, value: int) -> None:
        self._record("led_speed", value)
        self.led.speed = value
