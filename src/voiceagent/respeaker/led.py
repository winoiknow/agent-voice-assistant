"""Map semantic device states to LED-ring primitives.

The orchestrator (Phase 6) drives this with :class:`LedState`; here we translate
each state into the nearest available firmware effect using the configured colors.

Per ARCHITECTURE.md §3.8 the desired cues include motion ("green sequential loop",
"sequential blue") and a "flash", which the firmware does not expose. Those degrade
to the nearest steady/breath primitive; THINKING maps exactly to BREATH (pulsing).
Keeping this mapping in one place means the upgrade to true per-pixel animation
(if/when available) touches nothing else.
"""

from __future__ import annotations

from enum import Enum

from voiceagent.config import LedConfig
from voiceagent.logging_setup import get_logger
from voiceagent.respeaker.base import RGB, LedEffect, XvfHost

log = get_logger("respeaker.led")

_ERROR_COLOR: RGB = (255, 0, 0)


class LedState(Enum):
    IDLE = "idle"  # normal ops — ring off
    ENGAGING = "engaging"  # wake just detected (target: green flash -> loop)
    LISTENING = "listening"  # user speaking (target: green loop)
    THINKING = "thinking"  # awaiting model (pulsing blue — exact)
    SPEAKING = "speaking"  # response playing (target: sequential blue)
    ERROR = "error"  # fail-safe


class LedController:
    def __init__(self, host: XvfHost, cfg: LedConfig) -> None:
        self.host = host
        self.cfg = cfg
        self._last: LedState | None = None

    def _plan(self, state: LedState) -> tuple[LedEffect, RGB] | None:
        """Return (effect, color) for a state, or None for 'off'."""
        if state in (LedState.IDLE,):
            return None
        if state in (LedState.ENGAGING, LedState.LISTENING):
            # Target motion/flash not in firmware -> nearest steady green.
            return (LedEffect.SINGLE, self.cfg.listen_color)
        if state is LedState.THINKING:
            return (LedEffect.BREATH, self.cfg.think_color)  # exact: pulsing
        if state is LedState.SPEAKING:
            # Target sequential blue -> nearest steady blue.
            return (LedEffect.SINGLE, self.cfg.speak_color)
        if state is LedState.ERROR:
            return (LedEffect.BREATH, _ERROR_COLOR)
        return None  # pragma: no cover - exhaustive above

    async def show(self, state: LedState) -> None:
        if not self.cfg.enabled:
            return
        plan = self._plan(state)
        log.debug("led_show", state=state.value, plan=plan)
        if plan is None:
            await self.host.led_off()
        else:
            effect, color = plan
            await self.host.led_brightness(self.cfg.brightness)
            await self.host.led_color(color)
            await self.host.led_effect(effect)
        self._last = state

    async def off(self) -> None:
        await self.show(LedState.IDLE)
