from __future__ import annotations

import base64
from array import array
from types import SimpleNamespace
from typing import Any

import pytest

from voiceagent.audio.mock import MockAudioIO
from voiceagent.audio.types import AudioFormat
from voiceagent.config import RealtimeConfig
from voiceagent.realtime import RealtimeSession, build_session_update, wire_rate


# ── session.update builder (pure) ───────────────────────────────
def test_session_update_default_is_24k_standard() -> None:
    upd = build_session_update(RealtimeConfig(instructions="be brief", voice="af_heart"))
    sess = upd["session"]
    assert upd["type"] == "session.update"
    assert sess["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert sess["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert sess["audio"]["input"]["turn_detection"] == {
        "type": "server_vad", "interrupt_response": True
    }
    assert sess["audio"]["output"]["voice"] == "af_heart"
    assert sess["instructions"] == "be brief"


def test_session_update_native_16k_omits_format() -> None:
    upd = build_session_update(RealtimeConfig(native_16k=True))
    sess = upd["session"]
    assert "format" not in sess["audio"]["input"]
    assert "format" not in sess["audio"]["output"]


def test_session_update_vad_off() -> None:
    upd = build_session_update(RealtimeConfig(server_vad=False))
    assert "turn_detection" not in upd["session"]["audio"]["input"]


def test_wire_rate() -> None:
    assert wire_rate(RealtimeConfig()) == 24000
    assert wire_rate(RealtimeConfig(native_16k=True)) == 16000


# ── resampling (needs numpy+scipy) ──────────────────────────────
def test_resample_roundtrip() -> None:
    pytest.importorskip("scipy")
    from voiceagent.realtime import resample_pcm16

    a = array("h", [int(8000 * (1 if i % 20 < 10 else -1)) for i in range(1600)])
    pcm = a.tobytes()
    up = resample_pcm16(pcm, 16000, 24000)
    assert len(up) // 2 == pytest.approx(1600 * 24000 / 16000, rel=0.01)
    back = resample_pcm16(up, 24000, 16000)
    assert len(back) // 2 == pytest.approx(1600, rel=0.01)


def test_resample_noop_same_rate() -> None:
    from voiceagent.realtime import resample_pcm16

    assert resample_pcm16(b"\x01\x02", 16000, 16000) == b"\x01\x02"
    assert resample_pcm16(b"", 16000, 24000) == b""


# ── session loop over a fake connection ─────────────────────────
class FakeConn:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)
        self.sent: list[dict[str, Any]] = []

    async def send(self, event: dict[str, Any]) -> None:
        self.sent.append(event)

    async def recv(self) -> Any:
        if self._events:
            return self._events.pop(0)
        raise StopAsyncIteration


def _evt(type_: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **kw)


async def test_session_loop_handles_events_and_barge_in() -> None:
    # native_16k => no resampling so the loop needs no numpy/scipy.
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512, max_frames=2)
    cfg = RealtimeConfig(native_16k=True)
    audio_pcm = b"\x10\x00" * 320
    events = [
        _evt("session.created"),
        _evt("conversation.item.input_audio_transcription.completed", transcript="hello there"),
        _evt("response.created"),
        _evt("response.output_audio.delta", delta=base64.b64encode(audio_pcm).decode()),
        _evt("response.output_audio_transcript.done", transcript="hi, how can I help?"),
        _evt("input_audio_buffer.speech_started"),  # barge-in
        _evt("response.done", response=SimpleNamespace(status="completed")),
    ]
    captured: list[dict[str, Any]] = []
    sess = RealtimeSession(
        cfg, io, capture_rate=16000, playback_rate=16000, on_event=captured.append
    )
    conn = FakeConn(events)
    async with io:
        await sess.run_with_connection(conn, duration_s=2.0)

    kinds = [e["kind"] for e in captured]
    assert "connected" in kinds
    assert {"kind": "user_transcript", "text": "hello there", "final": True} in captured
    assert any(e["kind"] == "assistant_transcript" for e in captured)
    assert "barge_in" in kinds
    assert any(e["kind"] == "response_done" for e in captured)

    # audio delta was written to the streaming sink; barge-in cleared it.
    assert bytes(io.stream_written) == audio_pcm
    assert io.stream_clears == 1
    # session.update was sent first, then mic frames appended.
    assert conn.sent[0]["type"] == "session.update"
    assert any(s["type"] == "input_audio_buffer.append" for s in conn.sent)


async def test_session_loop_surfaces_errors() -> None:
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512, max_frames=1)
    cfg = RealtimeConfig(native_16k=True)
    events = [_evt("error", error=SimpleNamespace(type="invalid_request", message="bad"))]
    captured: list[dict[str, Any]] = []
    sess = RealtimeSession(cfg, io, capture_rate=16000, playback_rate=16000,
                           on_event=captured.append)
    async with io:
        await sess.run_with_connection(FakeConn(events), duration_s=2.0)
    assert {"kind": "error", "type": "invalid_request", "message": "bad"} in captured
