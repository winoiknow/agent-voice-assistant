"""Real wake detector backed by openWakeWord.

Imported only when ``wakeword.engine: openwakeword`` is selected, so the heavier
deps (openwakeword + onnxruntime + numpy — the ``wakeword`` extra) aren't needed
for dev/CI. Incoming frames are buffered into openWakeWord's preferred 80 ms
(1280-sample @ 16 kHz) windows before each prediction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from voiceagent.logging_setup import get_logger
from voiceagent.wakeword.base import PrerollBuffer, WakeDetector, WakeEvent

log = get_logger("wakeword.openwakeword")

CHUNK_SAMPLES = 1280  # 80 ms @ 16 kHz — openWakeWord's expected frame
CHUNK_BYTES = CHUNK_SAMPLES * 2


class OpenWakeWordDetector(WakeDetector):
    def __init__(
        self,
        *,
        models: Sequence[str],
        threshold: float,
        vad_threshold: float,
        rate: int,
        preroll_bytes: int,
        cooldown_samples: int,
    ) -> None:
        try:
            import numpy as np
            import openwakeword
            from openwakeword.model import Model
        except ImportError as exc:  # pragma: no cover - exercised only on-device
            raise RuntimeError(
                "openWakeWord is required for wakeword.engine: openwakeword; install "
                "the 'wakeword' extra (pip install '.[wakeword]') or set "
                "wakeword.engine: mock for development"
            ) from exc

        if rate != 16000:
            raise ValueError("openWakeWord requires a 16 kHz capture rate")

        self._np = np
        # Ensure the shared feature models (melspectrogram/embedding) are present.
        openwakeword.utils.download_models()
        model_kwargs: dict[str, Any] = {"inference_framework": "onnx"}
        if models:
            model_kwargs["wakeword_models"] = list(models)
        if vad_threshold > 0:
            model_kwargs["vad_threshold"] = vad_threshold
        self._model = Model(**model_kwargs)

        self.threshold = threshold
        self.rate = rate
        self.cooldown_samples = cooldown_samples
        self._preroll = PrerollBuffer(preroll_bytes)
        self._pending = bytearray()
        self._cooldown = 0

    def process(self, frame: bytes) -> WakeEvent | None:
        self._preroll.extend(frame)
        if self._cooldown > 0:
            self._cooldown -= len(frame) // 2
            # Keep the model's internal buffer warm but skip detection.
            self._pending.clear()
            return None

        self._pending += frame
        while len(self._pending) >= CHUNK_BYTES:
            chunk = bytes(self._pending[:CHUNK_BYTES])
            del self._pending[:CHUNK_BYTES]
            arr = self._np.frombuffer(chunk, dtype=self._np.int16)
            scores: dict[str, float] = self._model.predict(arr)
            best_model, best_score = max(scores.items(), key=lambda kv: kv[1], default=("", 0.0))
            if best_score >= self.threshold:
                self._cooldown = self.cooldown_samples
                event = WakeEvent(
                    model=best_model,
                    score=float(best_score),
                    preroll=self._preroll.snapshot(),
                    rate=self.rate,
                )
                log.info("wake", model=best_model, score=round(best_score, 3),
                         preroll_ms=event.preroll_ms)
                return event
        return None

    def reset(self) -> None:
        self._preroll.clear()
        self._pending.clear()
        self._cooldown = 0
        if hasattr(self._model, "reset"):
            self._model.reset()
