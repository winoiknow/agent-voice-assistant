"""Audio I/O abstraction.

One interface, two implementations: a dependency-free :class:`MockAudioIO` for
development/CI, and a ``sounddevice`` backend for the device. The orchestrator and
realtime client only ever see this interface, so they run identically on a laptop
(mock) and on the SBC (real hardware).
"""

from __future__ import annotations

import wave
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

from voiceagent.audio.types import AudioDevice, AudioFormat


class AudioIO(ABC):
    """Capture, playback, cue, and music-ducking surface."""

    def __init__(self, capture_format: AudioFormat, playback_format: AudioFormat) -> None:
        self.capture_format = capture_format
        self.playback_format = playback_format

    # ── lifecycle ────────────────────────────────────────────────
    @abstractmethod
    async def start(self) -> None:
        """Open devices / streams."""

    @abstractmethod
    async def stop(self) -> None:
        """Close devices / streams. Safe to call more than once."""

    async def __aenter__(self) -> AudioIO:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ── devices ──────────────────────────────────────────────────
    @abstractmethod
    def list_devices(self) -> list[AudioDevice]:
        ...

    # ── capture ──────────────────────────────────────────────────
    @abstractmethod
    def capture_stream(self) -> AsyncIterator[bytes]:
        """Yield int16-mono PCM frames at ``capture_format`` until stopped."""

    # ── playback ─────────────────────────────────────────────────
    @abstractmethod
    async def play_pcm(self, data: bytes, fmt: AudioFormat | None = None) -> None:
        """Play raw int16 PCM, blocking until it finishes. Defaults to playback_format."""

    async def play_wav(self, path: str | Path) -> None:
        """Play a 16-bit PCM WAV file at its own sample rate."""
        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() != 2:
                raise ValueError(f"{path}: only 16-bit PCM WAV is supported")
            fmt = AudioFormat(rate=wf.getframerate(), channels=wf.getnchannels())
            data = wf.readframes(wf.getnframes())
        await self.play_pcm(data, fmt)

    # ── streaming playback (TTS) ─────────────────────────────────
    # A persistent output stream fed chunk-by-chunk, so realtime response audio
    # plays continuously and can be cut instantly on barge-in via clear().
    @abstractmethod
    async def play_stream_start(self, fmt: AudioFormat | None = None) -> None:
        """Open the streaming output. Defaults to playback_format."""

    @abstractmethod
    def play_stream_write(self, pcm: bytes) -> None:
        """Append PCM (at the stream's format) to the playback buffer."""

    @abstractmethod
    def play_stream_clear(self) -> None:
        """Drop any buffered playback audio immediately (barge-in)."""

    @abstractmethod
    async def play_stream_stop(self) -> None:
        """Close the streaming output."""

    # ── ducking ──────────────────────────────────────────────────
    @abstractmethod
    async def set_music_gain(self, level: float) -> None:
        """Set the music target's volume (0.0..1.0). No-op if no target is wired."""
