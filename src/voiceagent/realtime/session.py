"""Realtime session against the speech2speech (OpenAI Realtime) server.

``build_session_update`` is a pure function (unit-tested). ``RealtimeSession`` runs
the bidirectional loop: capture → resample → ``input_audio_buffer.append`` on one
side, and inbound events (audio deltas → streaming playback, transcripts, tool
calls, barge-in) on the other. The connection is injected via the
:class:`RealtimeConnection` protocol so the loop is testable with a fake.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any, Protocol

from voiceagent.audio.base import AudioIO
from voiceagent.audio.types import AudioFormat
from voiceagent.config import RealtimeConfig
from voiceagent.logging_setup import get_logger
from voiceagent.realtime.resample import resample_pcm16

log = get_logger("realtime.session")

STANDARD_WIRE_RATE = 24000  # OpenAI Realtime standard
EventCallback = Callable[[dict[str, Any]], None]


def wire_rate(cfg: RealtimeConfig) -> int:
    return 16000 if cfg.native_16k else STANDARD_WIRE_RATE


def build_session_update(cfg: RealtimeConfig) -> dict[str, Any]:
    """Build the session.update sent on connect.

    Declares the 24 kHz OpenAI-standard audio format unless native_16k is set, in
    which case the format is omitted (the s2s server then defaults to 16 kHz).
    """
    input_cfg: dict[str, Any] = {}
    output_cfg: dict[str, Any] = {}
    if cfg.server_vad:
        input_cfg["turn_detection"] = {
            "type": "server_vad",
            "interrupt_response": cfg.interrupt_response,
        }
    if not cfg.native_16k:
        input_cfg["format"] = {"type": "audio/pcm", "rate": STANDARD_WIRE_RATE}
        output_cfg["format"] = {"type": "audio/pcm", "rate": STANDARD_WIRE_RATE}
    if cfg.voice:
        output_cfg["voice"] = cfg.voice

    session: dict[str, Any] = {
        "type": "realtime",
        "audio": {"input": input_cfg, "output": output_cfg},
    }
    if cfg.instructions:
        session["instructions"] = cfg.instructions
    return {"type": "session.update", "session": session}


class RealtimeConnection(Protocol):
    async def send(self, event: dict[str, Any]) -> None: ...
    async def recv(self) -> Any: ...


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


class RealtimeSession:
    """Drives one realtime conversation over a connection."""

    def __init__(
        self,
        cfg: RealtimeConfig,
        audio_io: AudioIO,
        *,
        capture_rate: int,
        playback_rate: int,
        on_event: EventCallback | None = None,
        preroll: bytes = b"",
    ) -> None:
        self.cfg = cfg
        self.audio = audio_io
        self.capture_rate = capture_rate
        self.playback_rate = playback_rate
        self.wire_rate = wire_rate(cfg)
        self._on_event = on_event
        self.preroll = preroll
        self._stop = asyncio.Event()
        # Closed during the warm-up handshake: the send loop holds mic streaming
        # until begin_listening() opens it (after the acknowledge earcon), so the
        # wake word still in the capture pipeline is never sent to the model.
        self._listen_gate = asyncio.Event()
        self._connected = False  # emit "connected" once, on the first session event

    def _emit(self, kind: str, **data: Any) -> None:
        if self._on_event is not None:
            self._on_event({"kind": kind, **data})

    def begin_listening(self) -> None:
        """Release the warm-up gate: start streaming the user's request."""
        self._listen_gate.set()

    def stop(self) -> None:
        self._stop.set()
        self._listen_gate.set()  # unblock a send loop still waiting on warm-up

    # ── public entry: open the openai connection and run ─────────
    async def run(self, *, duration_s: float | None = None) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - exercised only on-device
            raise RuntimeError(
                "the 'openai' package is required for the realtime client; install "
                "the 'realtime' extra (pip install '.[realtime]')"
            ) from exc

        cfg = self.cfg
        base_url = cfg.base_url or f"http://{cfg.host}:{cfg.port}/v1"
        ws_base_url = cfg.ws_base_url or f"ws://{cfg.host}:{cfg.port}/v1"
        client = AsyncOpenAI(
            api_key=cfg.api_key.get_secret_value() if cfg.api_key else "none",
            base_url=base_url,
            websocket_base_url=ws_base_url,
        )
        log.info("realtime_connecting", ws_base_url=ws_base_url, model=cfg.model,
                 wire_rate=self.wire_rate)
        async with client.realtime.connect(model=cfg.model) as conn:
            await self.run_with_connection(conn, duration_s=duration_s)

    # ── testable core: run loops over an injected connection ─────
    async def run_with_connection(
        self, conn: RealtimeConnection, *, duration_s: float | None = None
    ) -> None:
        self._stop.clear()
        self._connected = False
        if not self.cfg.warmup_handshake:
            self._listen_gate.set()  # legacy path: stream immediately
        else:
            self._listen_gate.clear()
        # Send the instructions / session config first. With warm-up enabled this
        # is the only thing on the wire until begin_listening() — it "fires up" the
        # connection while the recv loop waits for session.created.
        await conn.send(build_session_update(self.cfg))

        send_task = asyncio.create_task(self._send_loop(conn))
        recv_task = asyncio.create_task(self._recv_loop(conn))
        tasks = [send_task, recv_task]
        try:
            # Returns on the duration timeout, when the connection ends (recv
            # breaks), or when stop() makes the send loop exit.
            await asyncio.wait(tasks, timeout=duration_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._stop.set()
            self._listen_gate.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.audio.play_stream_stop()

    async def _send_loop(self, conn: RealtimeConnection) -> None:
        # Warm-up gate: hold until the orchestrator has played the acknowledge
        # earcon and called begin_listening(). The wake word still sitting in the
        # capture buffer is thereby never streamed to the model.
        await self._listen_gate.wait()
        if self._stop.is_set():
            return
        await self.audio.play_stream_start(AudioFormat(self.playback_rate, 1))
        # Discard everything captured during connect + the earcon so the turn
        # starts on the user's first fresh word.
        self.audio.drain_capture()
        # Optional pre-roll seed (normally empty: we no longer send the wake word).
        if self.preroll:
            pcm = resample_pcm16(self.preroll, self.capture_rate, self.wire_rate)
            await conn.send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }
            )
        async for frame in self.audio.capture_stream():
            if self._stop.is_set():
                break
            pcm = resample_pcm16(frame, self.capture_rate, self.wire_rate)
            await conn.send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }
            )

    async def _recv_loop(self, conn: RealtimeConnection) -> None:
        while not self._stop.is_set():
            try:
                event = await conn.recv()
            except (StopAsyncIteration, asyncio.CancelledError):
                break
            except Exception as exc:  # connection closed/dropped
                log.warning("recv_error", error=str(exc))
                break
            if event is None:
                break
            self._handle_event(event)

    def _handle_event(self, event: Any) -> None:
        etype = _attr(event, "type", "")
        if etype in ("session.created", "session.updated"):
            # Either marks the connection as live/ready; emit once so the
            # orchestrator can release the warm-up gate regardless of which the
            # s2s server sends first.
            if not self._connected:
                self._connected = True
                self._emit("connected")
        elif etype == "input_audio_buffer.speech_started":
            # Barge-in: cut local playback immediately.
            self.audio.play_stream_clear()
            self._emit("barge_in")
        elif etype == "input_audio_buffer.speech_stopped":
            self._emit("speech_stopped")
        elif etype == "conversation.item.input_audio_transcription.delta":
            self._emit("user_transcript", text=_attr(event, "delta", ""), final=False)
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._emit("user_transcript", text=_attr(event, "transcript", ""), final=True)
        elif etype == "response.created":
            self._emit("response_started")
        elif etype == "response.output_audio.delta":
            pcm = base64.b64decode(_attr(event, "delta", ""))
            pcm = resample_pcm16(pcm, self.wire_rate, self.playback_rate)
            self.audio.play_stream_write(pcm)
            self._emit("audio", bytes=len(pcm))
        elif etype == "response.output_audio_transcript.done":
            self._emit("assistant_transcript", text=_attr(event, "transcript", ""))
        elif etype == "response.function_call_arguments.done":
            self._emit(
                "tool_call",
                name=_attr(event, "name", ""),
                call_id=_attr(event, "call_id", ""),
                arguments=_attr(event, "arguments", ""),
            )
        elif etype == "response.done":
            resp = _attr(event, "response")
            self._emit("response_done", status=_attr(resp, "status", "completed"))
        elif etype == "error":
            err = _attr(event, "error")
            self._emit("error", type=_attr(err, "type", ""), message=_attr(err, "message", ""))
            log.warning("realtime_error", type=_attr(err, "type", ""),
                        message=_attr(err, "message", ""))


__all__ = ["RealtimeSession", "RealtimeConnection", "build_session_update", "wire_rate"]
