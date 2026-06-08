"""reSpeaker XVF3800 control abstraction (the ``xvf_host`` surface).

Exposes DSP parameter get/set and the firmware LED primitives. Two backends: a
:class:`MockXvfHost` for dev/CI and a real subprocess wrapper over the prebuilt
``xvf_host`` binary.

The firmware LED effects are a *fixed set* (see :class:`LedEffect`). Higher-level
"chase"/"flash" cues are not firmware primitives; the LED controller approximates
them with the nearest available effect (see :mod:`voiceagent.respeaker.led`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import IntEnum

RGB = tuple[int, int, int]


class LedEffect(IntEnum):
    """xvf_host LED_EFFECT modes (the only effects the firmware exposes)."""

    OFF = 0
    BREATH = 1  # pulsing
    RAINBOW = 2
    SINGLE = 3  # steady single color
    DOA = 4  # direction-of-arrival visualization


class XvfHost(ABC):
    """Control surface for the XVF3800 over xvf_host."""

    @abstractmethod
    async def get_param(self, name: str) -> list[float]:
        ...

    @abstractmethod
    async def set_param(self, name: str, values: Sequence[float]) -> None:
        ...

    @abstractmethod
    async def save_configuration(self) -> None:
        ...

    async def apply_tuning(self, tuning: dict[str, list[float]], *, save: bool = False) -> None:
        """Apply a name->values map of DSP params at startup."""
        for name, values in tuning.items():
            await self.set_param(name, values)
        if save and tuning:
            await self.save_configuration()

    # ── LED primitives ───────────────────────────────────────────
    @abstractmethod
    async def led_effect(self, effect: LedEffect) -> None:
        ...

    @abstractmethod
    async def led_color(self, rgb: RGB) -> None:
        ...

    @abstractmethod
    async def led_brightness(self, value: int) -> None:
        ...

    @abstractmethod
    async def led_speed(self, value: int) -> None:
        ...

    async def led_off(self) -> None:
        await self.led_effect(LedEffect.OFF)
