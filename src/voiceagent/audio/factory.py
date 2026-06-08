"""Select an AudioIO implementation from config."""

from __future__ import annotations

from voiceagent.audio.base import AudioIO
from voiceagent.audio.types import AudioFormat
from voiceagent.config import AudioConfig


def create_audio_io(cfg: AudioConfig) -> AudioIO:
    capture_format = AudioFormat(rate=cfg.capture_rate, channels=1)
    playback_format = AudioFormat(rate=cfg.playback_rate, channels=1)
    frame_samples = capture_format.samples_for_ms(cfg.capture_frame_ms)

    if cfg.backend == "mock":
        from voiceagent.audio.mock import MockAudioIO

        return MockAudioIO(
            capture_format,
            playback_format,
            frame_samples=frame_samples,
        )

    from voiceagent.audio.sounddevice_io import SounddeviceAudioIO

    return SounddeviceAudioIO(
        capture_format,
        playback_format,
        frame_samples=frame_samples,
        capture_device=cfg.capture_device,
        playback_device=cfg.playback_device,
        music_target=cfg.music_target,
    )
