from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any

from voiceagent.audio.mock import MockAudioIO
from voiceagent.audio.types import AudioFormat
from voiceagent.config import Settings
from voiceagent.orchestrator import is_closer, strip_label
from voiceagent.orchestrator.core import Orchestrator
from voiceagent.respeaker.led import LedState
from voiceagent.wakeword.base import WakeEvent


# ── closer matching ─────────────────────────────────────────────
def test_strip_label() -> None:
    assert strip_label("[Eric] goodbye") == "goodbye"
    assert strip_label("no label here") == "no label here"


def test_is_closer() -> None:
    phrases = ["goodbye", "that's all", "stop listening"]
    assert is_closer("[Eric] Goodbye.", phrases)
    assert is_closer("ok that's all for now", phrases)
    assert not is_closer("what time is it?", phrases)


# ── fakes ───────────────────────────────────────────────────────
class OneShotWake:
    """Fires a WakeEvent on the first frame, then never again."""

    def __init__(self, preroll: bytes = b"") -> None:
        self._fired = False
        self.preroll = preroll

    def process(self, frame: bytes) -> WakeEvent | None:
        if not self._fired:
            self._fired = True
            return WakeEvent("test", 0.9, self.preroll, 16000)
        return None

    def reset(self) -> None:
        self._fired = False


class RecordingLed:
    def __init__(self) -> None:
        self.states: list[LedState] = []

    async def show(self, state: LedState) -> None:
        self.states.append(state)

    async def off(self) -> None:
        self.states.append(LedState.IDLE)


class FakeConversation:
    def __init__(self, on_event: Any, events: list[dict[str, Any]], *,
                 hold: bool = True, raise_exc: Exception | None = None) -> None:
        self._on = on_event
        self._events = events
        self._hold = hold
        self._raise = raise_exc
        self._stop = asyncio.Event()

    async def run(self) -> None:
        if self._raise is not None:
            raise self._raise
        for ev in self._events:
            if self._stop.is_set():
                return
            self._on(ev)
            await asyncio.sleep(0)
        if self._hold:
            await self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


def _settings(tmp: Path | None = None, **over: Any) -> Settings:
    data: dict[str, Any] = {
        "audio": {"backend": "mock"},
        "wakeword": {"engine": "mock"},
        "respeaker": {"simulate": True},
        "realtime": {"follow_up_window_s": 0.15},
    }
    for k, v in over.items():
        data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.__setitem__(k, v)
    return Settings(**data)


def _orch(
    settings: Settings, events: list[dict[str, Any]], **conv_kw: Any
) -> tuple[Orchestrator, RecordingLed]:
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512, max_frames=2)
    led = RecordingLed()

    def factory(on_event: Any, preroll: bytes) -> FakeConversation:
        return FakeConversation(on_event, events, **conv_kw)

    orch = Orchestrator(settings, io, OneShotWake(), led, conversation_factory=factory)  # type: ignore[arg-type]
    return orch, led


async def test_full_cycle_drives_led_states() -> None:
    events = [
        {"kind": "speech_started"},
        {"kind": "speech_stopped"},
        {"kind": "response_started"},
        {"kind": "audio", "bytes": 100},
        {"kind": "response_done", "status": "completed"},
        {"kind": "user_transcript", "text": "[Eric] goodbye", "final": True},
    ]
    orch, led = _orch(_settings(), events)
    await asyncio.wait_for(orch.run(), timeout=3.0)
    # engaged, listened, thought, spoke, and returned to idle.
    assert LedState.ENGAGING in led.states
    assert LedState.LISTENING in led.states
    assert LedState.THINKING in led.states
    assert LedState.SPEAKING in led.states
    assert led.states[-1] is LedState.IDLE


async def test_followup_timeout_closes() -> None:
    events = [{"kind": "response_done", "status": "completed"}]
    orch, led = _orch(_settings(), events, hold=True)
    # No further speech -> follow-up window (0.15s) elapses -> closes.
    await asyncio.wait_for(orch.run(), timeout=3.0)
    assert led.states[-1] is LedState.IDLE


async def test_closer_phrase_closes_immediately() -> None:
    events = [{"kind": "user_transcript", "text": "stop listening", "final": True}]
    orch, led = _orch(_settings(realtime={"closer_phrases": ["stop listening"],
                                          "follow_up_window_s": 99}), events, hold=True)
    await asyncio.wait_for(orch.run(), timeout=3.0)
    assert led.states[-1] is LedState.IDLE


async def test_conversation_error_triggers_failsafe(tmp_path: Path) -> None:
    earcon = tmp_path / "err.wav"
    with wave.open(str(earcon), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x01" * 64)
    settings = _settings(feedback={"error_sound": str(earcon)})
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512, max_frames=2)
    led = RecordingLed()

    def factory(on_event: Any, preroll: bytes) -> FakeConversation:
        return FakeConversation(on_event, [], raise_exc=RuntimeError("connect failed"))

    orch = Orchestrator(settings, io, OneShotWake(), led, conversation_factory=factory)  # type: ignore[arg-type]
    await asyncio.wait_for(orch.run(), timeout=3.0)
    # fail-safe played the error earcon and ended idle.
    assert io.total_played_bytes > 0
    assert led.states[-1] is LedState.IDLE
