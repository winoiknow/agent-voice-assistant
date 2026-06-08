"""In-memory audio backend for development and tests.

Generates synthetic capture frames, records everything played and every music-gain
change, and exposes fake devices. No system audio, no third-party deps.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Callable

from voiceagent.audio.base import AudioIO
from voiceagent.audio.types import AudioDevice, AudioFormat
from voiceagent.logging_setup import get_logger

log = get_logger("audio.mock")

# A frame generator takes the running frame index and returns one frame of PCM
# bytes (int16 mono). Default = silence.
FrameSource = Callable[[int], bytes]


def silence_source(frame_samples: int) -> FrameSource:
    blank = b"\x00\x00" * frame_samples
    return lambda _i: blank


def tone_source(frame_samples: int, rate: int, freq: float = 440.0) -> FrameSource:
    """A continuous sine tone, useful for hearing/seeing the playback path."""

    def gen(i: int) -> bytes:
        start = i * frame_samples
        out = bytearray()
        for n in range(frame_samples):
            t = (start + n) / rate
            val = int(0.3 * 32767 * math.sin(2 * math.pi * freq * t))
            out += int(val).to_bytes(2, "little", signed=True)
        return bytes(out)

    return gen


class MockAudioIO(AudioIO):
    def __init__(
        self,
        capture_format: AudioFormat,
        playback_format: AudioFormat,
        *,
        frame_samples: int = 512,
        frame_interval_s: float = 0.0,
        frame_source: FrameSource | None = None,
        max_frames: int | None = None,
    ) -> None:
        super().__init__(capture_format, playback_format)
        self.frame_samples = frame_samples
        self.frame_interval_s = frame_interval_s
        self.frame_source = frame_source or silence_source(frame_samples)
        self.max_frames = max_frames

        self.started = False
        self._stop = asyncio.Event()
        # Inspection surface for tests.
        self.played: list[tuple[bytes, AudioFormat]] = []
        self.music_gain: float = 1.0
        self.music_gain_history: list[float] = []

    async def start(self) -> None:
        self.started = True
        self._stop.clear()
        log.info("mock_audio_started", capture=self.capture_format.rate)

    async def stop(self) -> None:
        self.started = False
        self._stop.set()
        log.info("mock_audio_stopped")

    def list_devices(self) -> list[AudioDevice]:
        return [
            AudioDevice(index=0, name="mock-respeaker-xvf3800", max_input_channels=1,
                        max_output_channels=2),
        ]

    async def capture_stream(self) -> AsyncIterator[bytes]:
        i = 0
        while not self._stop.is_set():
            if self.max_frames is not None and i >= self.max_frames:
                break
            yield self.frame_source(i)
            i += 1
            if self.frame_interval_s:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.frame_interval_s)
            else:
                await asyncio.sleep(0)

    async def play_pcm(self, data: bytes, fmt: AudioFormat | None = None) -> None:
        self.played.append((data, fmt or self.playback_format))
        log.debug("mock_play", bytes=len(data))

    async def set_music_gain(self, level: float) -> None:
        self.music_gain = level
        self.music_gain_history.append(level)
        log.info("mock_music_gain", level=level)

    # ── test helpers ─────────────────────────────────────────────
    @property
    def total_played_bytes(self) -> int:
        return sum(len(d) for d, _ in self.played)
