"""Wake-word detection: openWakeWord (or a mock) with a pre-roll buffer."""

from __future__ import annotations

from voiceagent.wakeword.base import (
    PrerollBuffer,
    WakeDetector,
    WakeEvent,
    frame_rms,
    is_model_path,
    validate_model_specs,
)
from voiceagent.wakeword.factory import create_wake_detector

__all__ = [
    "WakeDetector",
    "WakeEvent",
    "PrerollBuffer",
    "frame_rms",
    "is_model_path",
    "validate_model_specs",
    "create_wake_detector",
]
