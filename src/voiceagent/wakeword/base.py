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
import os
from abc import ABC, abstractmethod
from array import array
from collections.abc import Sequence
from dataclasses import dataclass


def is_model_path(spec: str) -> bool:
    """True if a wakeword model spec is a file path rather than a pretrained name."""
    if spec.endswith((".onnx", ".tflite")):
        return True
    return os.sep in spec or (os.altsep is not None and os.altsep in spec)


def validate_model_specs(models: Sequence[str]) -> None:
    """Check that any path-like model specs exist and are ONNX (our inference path).

    Pretrained names (e.g. ``alexa``) pass through untouched.
    """
    for spec in models:
        if not is_model_path(spec):
            continue
        if not os.path.isfile(spec):
            raise FileNotFoundError(f"wakeword model file not found: {spec}")
        if spec.endswith(".tflite"):
            raise ValueError(
                f"custom wakeword model must be .onnx (the ONNX inference path is "
                f"used); got a .tflite file: {spec}. Export/convert it to .onnx."
            )


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
