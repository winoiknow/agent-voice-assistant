"""Audio I/O subsystem: capture, playback/cue, and music ducking."""

from __future__ import annotations

from voiceagent.audio.base import AudioIO
from voiceagent.audio.factory import create_audio_io
from voiceagent.audio.types import AudioDevice, AudioFormat

__all__ = ["AudioIO", "AudioDevice", "AudioFormat", "create_audio_io"]
