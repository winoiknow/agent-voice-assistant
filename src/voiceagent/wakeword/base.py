"""Wake-word detection abstraction.

A :class:`WakeDetector` is fed int16-mono PCM frames (at the capture rate) and
returns a :class:`WakeEvent` when the wake word fires. Detectors keep a pre-roll
ring buffer so the event carries the audio leading up to detection — the
orchestrator streams that to the realtime server so the user's first words aren't
clipped.

Two implementations: a dependency-free :class:`~voiceagent.wakeword.mock.MockWakeDetector`
for dev/CI and the real openWakeWord engine.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from array import array
from dataclasses import dataclass


@dataclass(frozen=True)
class WakeEvent:
    """A wake-word detection."""

    model: str
    score: float
    preroll: bytes  # int16 mono @ ``rate`` — audio just before/at detection
    rate: int

    @property
    def preroll_ms(self) -> int:
        return (len(self.preroll) // 2) * 1000 // self.rate if self.rate else 0


def frame_rms(pcm: bytes) -> float:
    """RMS amplitude of int16-mono PCM (0..32767). stdlib only (audioop-free)."""
    if not pcm:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class PrerollBuffer:
    """A byte ring buffer holding the most recent ``max_bytes`` of PCM."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._buf = bytearray()

    def extend(self, data: bytes) -> None:
        self._buf += data
        overflow = len(self._buf) - self.max_bytes
        if overflow > 0:
            del self._buf[:overflow]

    def snapshot(self) -> bytes:
        return bytes(self._buf)

    def clear(self) -> None:
        self._buf.clear()


class WakeDetector(ABC):
    """Feed PCM frames; get a WakeEvent when the wake word fires."""

    @abstractmethod
    def process(self, frame: bytes) -> WakeEvent | None:
        """Process one int16-mono frame. Returns an event on detection, else None."""

    def reset(self) -> None:  # noqa: B027 - optional hook; default is a no-op
        """Clear any internal state (pre-roll, cooldown, model buffers)."""
