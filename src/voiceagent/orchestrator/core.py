"""The orchestrator: the hands-free state machine.

IDLE (wake-word listening) → ENGAGING (wake cue + LED) → a realtime conversation
whose LED cues track the turn (LISTENING/THINKING/SPEAKING) → multi-turn via a
follow-up window → CLOSING on a closer phrase or timeout → IDLE. Errors fall back
to an earcon and IDLE.

Components (audio, wake detector, LED, conversation) are injected, so the whole
machine runs with mocks off-device and with real hardware on the SBC.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol

from voiceagent.audio.base import AudioIO
from voiceagent.config import Settings
from voiceagent.logging_setup import get_logger
from voiceagent.orchestrator.closer import is_closer
from voiceagent.realtime.session import EventCallback, RealtimeSession
from voiceagent.respeaker.led import LedController, LedState
from voiceagent.wakeword.base import WakeDetector, WakeEvent

log = get_logger("orchestrator")

_SENTINEL = object()


class Conversation(Protocol):
    async def run(self) -> None: ...
    def stop(self) -> None: ...


# Factory: given an event callback and the wake pre-roll, build a Conversation.
ConversationFactory = Callable[[EventCallback, bytes], Conversation]


class _SafeDict(defaultdict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        audio_io: AudioIO,
        wake_detector: WakeDetector,
        led: LedController,
        *,
        media: Any = None,
        conversation_factory: ConversationFactory | None = None,
    ) -> None:
        self.settings = settings
        self.audio = audio_io
        self.wake = wake_detector
        self.led = led
        self.media = media
        self._factory = conversation_factory or self._default_factory
        self._shutdown = asyncio.Event()
        # None (not IDLE) so the first _set_state(IDLE) actually fires and forces
        # the ring off — clearing any effect a previously killed run left set.
        self._state: LedState | None = None

    def request_shutdown(self) -> None:
        self._shutdown.set()

    # ── main loop ────────────────────────────────────────────────
    async def run(self) -> None:
        if self.media is not None:
            await self.media.start()
        try:
            async with self.audio:
                await self._set_state(LedState.IDLE)
                log.info("orchestrator_ready", device=self.settings.device.name)
                while not self._shutdown.is_set():
                    wake = await self._wait_for_wake()
                    if wake is None:
                        break
                    try:
                        await self._converse(wake)
                    except Exception as exc:
                        log.error("conversation_failed", error=str(exc))
                        await self._fail_safe()
                await self._set_state(LedState.IDLE)
                log.info("orchestrator_stopped")
        finally:
            if self.media is not None:
                await self.media.stop()

    async def _wait_for_wake(self) -> WakeEvent | None:
        log.info("idle_listening")
        async for frame in self.audio.capture_stream():
            if self._shutdown.is_set():
                return None
            event = self.wake.process(frame)
            if event is not None:
                log.info("wake_detected", model=event.model, score=round(event.score, 3))
                return event
        return None

    # ── one conversation ─────────────────────────────────────────
    async def _converse(self, wake: WakeEvent) -> None:
        events: asyncio.Queue[Any] = asyncio.Queue()
        conv = self._factory(events.put_nowait, wake.preroll)

        await self._engage()
        run_task = asyncio.create_task(conv.run())
        run_task.add_done_callback(lambda _t: events.put_nowait(_SENTINEL))

        loop = asyncio.get_running_loop()
        deadline: float | None = None
        try:
            while True:
                timeout = None if deadline is None else max(0.0, deadline - loop.time())
                try:
                    event = await asyncio.wait_for(events.get(), timeout=timeout)
                except TimeoutError:
                    log.info("followup_window_elapsed")
                    conv.stop()
                    deadline = None
                    continue
                if event is _SENTINEL:
                    break
                deadline = await self._apply_event(event, conv, deadline)
        finally:
            conv.stop()
        # Re-raise a conversation failure (e.g. connection error) so run() can run
        # the fail-safe; otherwise close cleanly.
        await run_task
        await self._close()

    async def _apply_event(
        self, event: dict[str, Any], conv: Conversation, deadline: float | None
    ) -> float | None:
        loop = asyncio.get_running_loop()
        kind = event["kind"]
        if kind in ("barge_in", "speech_started"):
            await self._set_state(LedState.LISTENING)
            return None  # user is talking again; cancel the follow-up timer
        if kind in ("speech_stopped", "response_started"):
            await self._set_state(LedState.THINKING)
        elif kind == "audio":
            if self._state is not LedState.SPEAKING:
                await self._set_state(LedState.SPEAKING)
        elif kind == "response_done":
            await self._set_state(LedState.LISTENING)
            return loop.time() + self.settings.realtime.follow_up_window_s
        elif kind == "user_transcript" and event.get("final"):
            if is_closer(event.get("text", ""), self.settings.realtime.closer_phrases):
                log.info("closer_detected", text=event.get("text", ""))
                conv.stop()
                return None
        elif kind == "error":
            log.warning("realtime_error", **{k: event.get(k) for k in ("type", "message")})
            conv.stop()
            return None
        return deadline

    # ── feedback + lifecycle ─────────────────────────────────────
    async def _engage(self) -> None:
        await self._set_state(LedState.ENGAGING)
        if self.media is not None:
            with contextlib.suppress(Exception):
                await self.media.on_turn_start()
        sound = self.settings.wakeword.wake_sound
        if sound:
            with contextlib.suppress(Exception):
                await self.audio.play_wav(sound)

    async def _close(self) -> None:
        if self.media is not None:
            with contextlib.suppress(Exception):
                await self.media.on_turn_end()
        await self._set_state(LedState.IDLE)
        log.info("conversation_closed")

    async def _fail_safe(self) -> None:
        if self.media is not None:
            with contextlib.suppress(Exception):
                await self.media.on_turn_end()  # make sure music resumes
        sound = self.settings.feedback.error_sound
        if sound:
            with contextlib.suppress(Exception):
                await self.audio.play_wav(sound)
        await self._set_state(LedState.IDLE)

    async def _set_state(self, state: LedState) -> None:
        if state is self._state:
            return
        self._state = state
        log.info("state", value=state.value)
        with contextlib.suppress(Exception):
            await self.led.show(state)

    # ── default conversation = realtime session ──────────────────
    def _default_factory(self, on_event: EventCallback, preroll: bytes) -> Conversation:
        rt = self.settings.realtime
        if rt.instructions:
            ctx = _SafeDict(
                str,
                device=self.settings.device.name,
                room=self.settings.device.room or "the room",
            )
            rt = rt.model_copy(update={"instructions": rt.instructions.format_map(ctx)})
        return RealtimeSession(
            rt,
            self.audio,
            capture_rate=self.settings.audio.capture_rate,
            playback_rate=self.settings.audio.playback_rate,
            on_event=on_event,
            preroll=preroll,
        )
