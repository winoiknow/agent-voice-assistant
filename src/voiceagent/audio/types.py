"""Shared audio value types (int16 PCM assumed throughout)."""

from __future__ import annotations

from dataclasses import dataclass

BYTES_PER_SAMPLE = 2  # int16


@dataclass(frozen=True)
class AudioFormat:
    """A PCM audio format. Samples are signed 16-bit little-endian."""

    rate: int
    channels: int = 1

    @property
    def bytes_per_frame(self) -> int:
        """Bytes for one frame (one sample across all channels)."""
        return BYTES_PER_SAMPLE * self.channels

    def frame_bytes(self, num_samples: int) -> int:
        return num_samples * self.bytes_per_frame

    def samples_for_ms(self, ms: float) -> int:
        return int(self.rate * ms / 1000)


@dataclass(frozen=True)
class AudioDevice:
    """A capture/playback device as reported by the backend."""

    index: int | None
    name: str
    max_input_channels: int
    max_output_channels: int
