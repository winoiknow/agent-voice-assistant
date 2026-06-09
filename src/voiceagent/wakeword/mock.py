"""Dependency-free wake detector: fires when a frame's RMS exceeds a threshold.

Useful for dev/CI and for a quick "does the pipeline wake?" smoke on real hardware
without pulling openWakeWord — clap or speak loudly to trigger it.
"""

from __future__ import annotations

from voiceagent.logging_setup import get_logger
from voiceagent.wakeword.base import PrerollBuffer, WakeDetector, WakeEvent, frame_rms

log = get_logger("wakeword.mock")

_FULL_SCALE = 32767.0


class MockWakeDetector(WakeDetector):
    def __init__(
        self,
        *,
        rate: int,
        preroll_bytes: int,
        cooldown_samples: int,
        trigger_rms: float = 1500.0,
        model: str = "mock",
    ) -> None:
        self.rate = rate
        self.trigger_rms = trigger_rms
        self.cooldown_samples = cooldown_samples
        self.model = model
        self._preroll = PrerollBuffer(preroll_bytes)
        self._cooldown = 0

    def process(self, frame: bytes) -> WakeEvent | None:
        self._preroll.extend(frame)
        if self._cooldown > 0:
            self._cooldown -= len(frame) // 2
            return None
        rms = frame_rms(frame)
        if rms >= self.trigger_rms:
            self._cooldown = self.cooldown_samples
            event = WakeEvent(
                model=self.model,
                score=min(1.0, rms / _FULL_SCALE),
                preroll=self._preroll.snapshot(),
                rate=self.rate,
            )
            log.info("mock_wake", rms=round(rms, 1), preroll_ms=event.preroll_ms)
            return event
        return None

    def reset(self) -> None:
        self._preroll.clear()
        self._cooldown = 0
