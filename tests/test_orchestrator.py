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
        self.listening = False

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

    def begin_listening(self) -> None:
        self.listening = True

    def stop(self) -> None:
        self._stop.set()


def _settings(tmp: Path | None = None, **over: Any) -> Settings:
    data: dict[str, Any] = {
        "audio": {"backend": "mock"},
        "wakeword": {"engine": "mock"},
        "respeaker": {"simulate": True},
        "realtime": {"follow_up_window_s": 0.15, "post_close_grace_s": 0.0},
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


def _wav(path: Path) -> str:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x01" * 64)
    return str(path)


async def test_warmup_acks_and_listens_on_connected(tmp_path: Path) -> None:
    # On "connected" the orchestrator plays the acknowledge earcon, goes green,
    # and releases the conversation's mic gate — the wake word is never streamed.
    ack = _wav(tmp_path / "ack.wav")
    settings = _settings(wakeword={"engine": "mock", "wake_sound": ack})
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512, max_frames=2)
    led = RecordingLed()
    convs: list[FakeConversation] = []

    def factory(on_event: Any, preroll: bytes) -> FakeConversation:
        c = FakeConversation(on_event, [
            {"kind": "connected"},
            {"kind": "response_done", "status": "completed"},
        ], hold=True)
        convs.append(c)
        return c

    orch = Orchestrator(settings, io, OneShotWake(), led, conversation_factory=factory)  # type: ignore[arg-type]
    await asyncio.wait_for(orch.run(), timeout=3.0)
    assert convs[0].listening is True  # begin_listening() was called
    assert io.total_played_bytes > 0  # acknowledge earcon played
    # ENGAGING (connecting) precedes LISTENING (speak now).
    assert led.states.index(LedState.ENGAGING) < led.states.index(LedState.LISTENING)
    assert led.states[-1] is LedState.IDLE


async def test_watchdog_aborts_stalled_turn(tmp_path: Path) -> None:
    # A turn that produces no events (s2s went silent) must not hang: the watchdog
    # fires, the fail-safe earcon plays, and we return to idle.
    earcon = _wav(tmp_path / "err.wav")
    settings = _settings(
        feedback={"error_sound": earcon},
        realtime={"turn_watchdog_s": 0.2, "follow_up_window_s": 0.15,
                  "post_close_grace_s": 0.0},
    )
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512, max_frames=2)
    led = RecordingLed()

    def factory(on_event: Any, preroll: bytes) -> FakeConversation:
        return FakeConversation(on_event, [], hold=True)  # never emits anything

    orch = Orchestrator(settings, io, OneShotWake(), led, conversation_factory=factory)  # type: ignore[arg-type]
    await asyncio.wait_for(orch.run(), timeout=3.0)
    assert io.total_played_bytes > 0  # fail-safe earcon played
    assert led.states[-1] is LedState.IDLE


async def test_shutdown_interrupts_active_turn() -> None:
    # SIGTERM mid-turn must stop the conversation and exit, not wait it out.
    settings = _settings(realtime={"turn_watchdog_s": 0, "follow_up_window_s": 99,
                                   "post_close_grace_s": 0.0})
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512,
                     frame_interval_s=0.01)
    led = RecordingLed()
    convs: list[FakeConversation] = []

    def factory(on_event: Any, preroll: bytes) -> FakeConversation:
        c = FakeConversation(on_event, [{"kind": "connected"}], hold=True)
        convs.append(c)
        return c

    orch = Orchestrator(settings, io, OneShotWake(), led, conversation_factory=factory)  # type: ignore[arg-type]
    run_task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.1)  # let it wake and enter the conversation
    orch.request_shutdown()
    await asyncio.wait_for(run_task, timeout=3.0)
    assert convs[0]._stop.is_set()  # conversation was told to stop
    assert led.states[-1] is LedState.IDLE
