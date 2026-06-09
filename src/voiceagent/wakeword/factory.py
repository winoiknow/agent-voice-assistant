"""Select a WakeDetector implementation from config."""

from __future__ import annotations

from voiceagent.config import WakewordConfig
from voiceagent.wakeword.base import WakeDetector


def create_wake_detector(cfg: WakewordConfig, capture_rate: int) -> WakeDetector:
    preroll_bytes = int(capture_rate * cfg.preroll_s) * 2
    cooldown_samples = int(capture_rate * cfg.cooldown_s)

    if cfg.engine == "mock":
        from voiceagent.wakeword.mock import MockWakeDetector

        return MockWakeDetector(
            rate=capture_rate,
            preroll_bytes=preroll_bytes,
            cooldown_samples=cooldown_samples,
            trigger_rms=cfg.mock_trigger_rms,
        )

    from voiceagent.wakeword.openwakeword_detector import OpenWakeWordDetector

    return OpenWakeWordDetector(
        models=cfg.models,
        threshold=cfg.threshold,
        vad_threshold=cfg.vad_threshold,
        rate=capture_rate,
        preroll_bytes=preroll_bytes,
        cooldown_samples=cooldown_samples,
    )
