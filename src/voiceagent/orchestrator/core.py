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
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol

from voiceagent.audio.base import AudioIO
from voiceagent.config import Settings
from voiceagent.logging_setup import get_logger
from voiceagent.metrics import Metrics, MetricsReporter
from voiceagent.orchestrator.closer import is_closer
from voiceagent.realtime.session import EventCallback, RealtimeSession
from voiceagent.respeaker.led import LedController, LedState
from voiceagent.wakeword.base import WakeDetector, WakeEvent

log = get_logger("orchestrator")

_SENTINEL = object()
# How long a wake will wait for the warm connection before falling back to an
# inline connect. Normally the connection is already warm so acquire is instant;
# this only bounds the rare mid-re-warm (rapid re-wake) case.
_WARM_ACQUIRE_TIMEOUT_S = 1.0


class _StalledTurn(Exception):
    """The s2s server went silent mid-turn; recover via the fail-safe path."""


class Conversation(Protocol):
    async def run(self) -> None: ...
    def stop(self) -> None: ...
    def begin_listening(self) -> None: ...


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
        conn_manager: Any = None,
        arbitrator: Any = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.settings = settings
        self.audio = audio_io
        self.wake = wake_detector
        self.led = led
        self.media = media
        self._conn_manager = conn_manager
        self._arbitrator = arbitrator
        self.metrics = metrics or Metrics()
        self._reporter = MetricsReporter(self.metrics, settings.observability)
        self._state_since: dict[LedState, float] = {}
        self._factory = conversation_factory or self._default_factory
        self._shutdown = asyncio.Event()
        # None (not IDLE) so the first _set_state(IDLE) actually fires and forces
        # the ring off — clearing any effect a previously killed run left set.
        self._state: LedState | None = None
        # The conversation in flight, so shutdown can interrupt it mid-turn.
        self._active_conv: Conversation | None = None

    def request_shutdown(self) -> None:
        self._shutdown.set()
        # Break out of an in-flight turn so SIGTERM doesn't wait for it (or, worse,
        # for a hung turn) — otherwise systemd's stop timeout escalates to SIGKILL.
        conv = self._active_conv
        if conv is not None:
            with contextlib.suppress(Exception):
                conv.stop()

    # ── main loop ────────────────────────────────────────────────
    async def run(self) -> None:
        if self.media is not None:
            await self.media.start()
        if self._conn_manager is not None:
            await self._conn_manager.start()
        if self._arbitrator is not None:
            with contextlib.suppress(Exception):
                await self._arbitrator.start()
        await self._reporter.start()
        try:
            async with self.audio:
                await self._set_state(LedState.IDLE)
                log.info("orchestrator_ready", device=self.settings.device.name)
                while not self._shutdown.is_set():
                    wake = await self._wait_for_wake()
                    if wake is None:
                        break
                    if not await self._arbitrate(wake):
                        continue  # a louder peer is taking this one; stay idle
                    try:
                        await self._converse(wake)
                    except Exception as exc:
                        log.error("conversation_failed", error=str(exc))
                        self.metrics.incr("conversations_failed")
                        await self._fail_safe()
                    await self._post_close_settle()
                await self._set_state(LedState.IDLE)
                log.info("orchestrator_stopped")
        finally:
            with contextlib.suppress(Exception):
                await self._reporter.stop()
            if self._arbitrator is not None:
                with contextlib.suppress(Exception):
                    await self._arbitrator.stop()
            if self._conn_manager is not None:
                with contextlib.suppress(Exception):
                    await self._conn_manager.stop()
            if self.media is not None:
                await self.media.stop()

    async def _post_close_settle(self) -> None:
        """Drain the mic (no wake detection) so AEC re-converges after music resume."""
        grace = self.settings.realtime.post_close_grace_s
        if grace <= 0:
            return
        self.wake.reset()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        async for _frame in self.audio.capture_stream():
            if self._shutdown.is_set() or loop.time() >= deadline:
                break
        self.wake.reset()
        log.debug("post_close_settle_done", grace_s=grace)

    async def _arbitrate(self, wake: WakeEvent) -> bool:
        """Ask the multi-device arbitrator whether we should handle this wake.

        No arbitrator (single device / disabled) ⇒ always handle. On any error we
        fail open and handle, so arbitration can never swallow a wake outright.
        """
        if self._arbitrator is None:
            return True
        try:
            if await self._arbitrator.should_handle(wake):
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("arbitration_error_handling_anyway", error=str(exc))
            return True
        log.info("wake_suppressed_by_peer", model=wake.model)
        self.metrics.incr("wakes_suppressed")
        self.wake.reset()
        return False

    async def _wait_for_wake(self) -> WakeEvent | None:
        log.info("idle_listening")
        async for frame in self.audio.capture_stream():
            if self._shutdown.is_set():
                return None
            event = self.wake.process(frame)
            if event is not None:
                log.info("wake_detected", model=event.model, score=round(event.score, 3))
                self.metrics.incr("wakes_detected")
                return event
        return None

    # ── one conversation ─────────────────────────────────────────
    async def _converse(self, wake: WakeEvent) -> None:
        events: asyncio.Queue[Any] = asyncio.Queue()
        # We no longer hand the model the wake-word pre-roll: with the warm-up
        # handshake the user speaks fresh after the acknowledge earcon.
        conv = self._factory(events.put_nowait, b"")
        self._active_conv = conv
        self.metrics.incr("conversations_started")

        await self._engage()
        run_task = asyncio.create_task(conv.run())
        run_task.add_done_callback(lambda _t: events.put_nowait(_SENTINEL))

        loop = asyncio.get_running_loop()
        watchdog_s = self.settings.realtime.turn_watchdog_s
        followup_deadline: float | None = None
        watchdog_deadline: float | None = loop.time() + watchdog_s if watchdog_s > 0 else None
        listening = False
        stalled = False
        try:
            while True:
                deadlines = [d for d in (followup_deadline, watchdog_deadline) if d is not None]
                timeout = None if not deadlines else max(0.0, min(deadlines) - loop.time())
                try:
                    event = await asyncio.wait_for(events.get(), timeout=timeout)
                except TimeoutError:
                    now = loop.time()
                    if followup_deadline is not None and now >= followup_deadline:
                        log.info("followup_window_elapsed")
                        conv.stop()
                        followup_deadline = None
                        continue
                    log.warning("turn_watchdog_fired", waited_s=watchdog_s)
                    self.metrics.incr("turn_watchdog_fired")
                    stalled = True
                    conv.stop()
                    break
                if event is _SENTINEL:
                    break
                if watchdog_s > 0:  # any inbound activity is progress
                    watchdog_deadline = loop.time() + watchdog_s
                kind = event.get("kind")
                if not listening and kind != "error":
                    # First sign the connection is live (normally "connected"):
                    # acknowledge, go green, and release the mic. Robust to which
                    # session event the s2s server sends first.
                    listening = True
                    await self._begin_listening(conv)
                    if kind == "connected":
                        continue
                followup_deadline = await self._apply_event(event, conv, followup_deadline)
        finally:
            conv.stop()
            self._active_conv = None
        # Re-raise a conversation failure (e.g. connection error) so run() can run
        # the fail-safe; a stall recovers the same way; otherwise close cleanly.
        await run_task
        if stalled:
            raise _StalledTurn("no response from s2s within the turn watchdog window")
        await self._close()

    async def _apply_event(
        self, event: dict[str, Any], conv: Conversation, deadline: float | None
    ) -> float | None:
        loop = asyncio.get_running_loop()
        kind = event["kind"]
        if kind in ("barge_in", "speech_started"):
            if kind == "barge_in":
                self.metrics.incr("barge_ins")
            await self._set_state(LedState.LISTENING)
            return None  # user is talking again; cancel the follow-up timer
        if kind in ("speech_stopped", "response_started"):
            await self._set_state(LedState.THINKING)
        elif kind == "audio":
            if self._state is not LedState.SPEAKING:
                await self._set_state(LedState.SPEAKING)
                if self.media is not None:
                    # Catch music the agent started mid-turn (MA tools) so it
                    # ducks under the reply, not just music playing at wake time.
                    with contextlib.suppress(Exception):
                        await self.media.on_speaking()
        elif kind == "response_done":
            await self._set_state(LedState.LISTENING)
            return loop.time() + self.settings.realtime.follow_up_window_s
        elif kind == "tool_call":
            # Visible only if s2s forwards it; tools that run inside the Hermes
            # agent stay invisible (their latency is part of the THINKING gap).
            log.info("tool_call", name=event.get("name"))
            # MA drops the agent's volume command while the player is busy mid-turn,
            # so capture the requested level here and re-apply it at turn end.
            if self.media is not None:
                with contextlib.suppress(Exception):
                    self.media.note_volume_request(
                        event.get("name", ""), event.get("arguments", "")
                    )
        elif kind == "user_transcript" and event.get("final"):
            if is_closer(event.get("text", ""), self.settings.realtime.closer_phrases):
                log.info("closer_detected", text=event.get("text", ""))
                conv.stop()
                return None
        elif kind == "error":
            log.warning("realtime_error", **{k: event.get(k) for k in ("type", "message")})
            self.metrics.incr("realtime_errors")
            conv.stop()
            return None
        return deadline

    # ── feedback + lifecycle ─────────────────────────────────────
    async def _engage(self) -> None:
        # Wake fired: show "connecting" (not green yet) and free the player while
        # the realtime connection warms up in the background. The acknowledge
        # earcon + green LED come in _begin_listening once the connection is up.
        await self._set_state(LedState.ENGAGING)
        if self.media is not None:
            with contextlib.suppress(Exception):
                await self.media.on_turn_start()

    async def _begin_listening(self, conv: Conversation) -> None:
        # Connection is warmed up: acknowledge audibly, go green ("speak now"),
        # then release the mic so the user's request — not the wake word — is sent.
        sound = self.settings.wakeword.wake_sound
        if sound:
            with contextlib.suppress(Exception):
                await self.audio.play_wav(sound)
        await self._set_state(LedState.LISTENING)
        with contextlib.suppress(Exception):
            conv.begin_listening()

    async def _close(self) -> None:
        if self.media is not None:
            with contextlib.suppress(Exception):
                await self.media.on_turn_end()
        await self._set_state(LedState.IDLE)
        self.metrics.incr("conversations_closed")
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
        prev = self._state
        now = time.monotonic()
        # wake→listening (warm-up/connect cost) and think→speak (server response cost).
        if state is LedState.LISTENING and prev is LedState.ENGAGING:
            self.metrics.observe("wake_to_listen_s", now - self._state_since.get(prev, now))
        elif state is LedState.SPEAKING and prev is LedState.THINKING:
            self.metrics.observe("think_to_speak_s", now - self._state_since.get(prev, now))
        self._state = state
        self._state_since[state] = now
        self.metrics.gauge("state", state.value)
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
        acquire = release = None
        if self._conn_manager is not None:
            mgr = self._conn_manager

            async def acquire() -> Any:
                return await mgr.acquire(_WARM_ACQUIRE_TIMEOUT_S)

            release = mgr.release
        return RealtimeSession(
            rt,
            self.audio,
            capture_rate=self.settings.audio.capture_rate,
            playback_rate=self.settings.audio.playback_rate,
            on_event=on_event,
            preroll=preroll,
            acquire=acquire,
            release=release,
        )
