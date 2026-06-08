from __future__ import annotations

import wave
from pathlib import Path

from voiceagent.audio import AudioFormat, create_audio_io
from voiceagent.audio.mock import MockAudioIO, tone_source
from voiceagent.config import AudioConfig


def _fmt() -> tuple[AudioFormat, AudioFormat]:
    return AudioFormat(16000, 1), AudioFormat(24000, 1)


async def test_mock_capture_stream_yields_frames() -> None:
    cap, play = _fmt()
    io = MockAudioIO(cap, play, frame_samples=512, max_frames=5)
    frames: list[bytes] = []
    async with io:
        async for frame in io.capture_stream():
            frames.append(frame)
    assert len(frames) == 5
    assert all(len(f) == 512 * 2 for f in frames)  # int16 mono


async def test_mock_playback_and_gain_recorded() -> None:
    cap, play = _fmt()
    io = MockAudioIO(cap, play)
    async with io:
        await io.play_pcm(b"\x00\x01" * 100)
        await io.set_music_gain(0.2)
        await io.set_music_gain(1.0)
    assert io.total_played_bytes == 200
    assert io.music_gain_history == [0.2, 1.0]
    assert io.music_gain == 1.0


async def test_play_wav_reads_format(tmp_path: Path) -> None:
    wav_path = tmp_path / "cue.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\x10\x00" * 256)
    cap, play = _fmt()
    io = MockAudioIO(cap, play)
    async with io:
        await io.play_wav(wav_path)
    data, fmt = io.played[-1]
    assert fmt == AudioFormat(22050, 1)  # plays at the wav's own rate
    assert len(data) == 256 * 2


def test_tone_source_produces_nonzero() -> None:
    src = tone_source(64, 16000, 440.0)
    frame = src(1)
    assert len(frame) == 64 * 2
    assert any(b != 0 for b in frame)


def test_factory_selects_mock() -> None:
    io = create_audio_io(AudioConfig(backend="mock"))
    assert isinstance(io, MockAudioIO)
    # capture frame size derived from capture_frame_ms (32ms @ 16k = 512)
    assert io.frame_samples == 512
