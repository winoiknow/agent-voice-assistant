"""Hardware bring-up routines behind the Phase 3 CLI test commands.

Each routine works against the configured backend, so they run end-to-end with the
mock backends (``audio.backend: mock``, ``respeaker.simulate: true``) on a laptop and
against real hardware on the SBC, unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from voiceagent.audio import create_audio_io
from voiceagent.config import Settings
from voiceagent.logging_setup import get_logger
from voiceagent.media import SendspinDaemon
from voiceagent.realtime import RealtimeSession
from voiceagent.respeaker import LedController, LedState, create_xvf_host
from voiceagent.wakeword import create_wake_detector

log = get_logger("diagnostics")


async def run_audio_test(settings: Settings, *, duration_s: float = 2.0) -> dict[str, object]:
    """Capture for a moment, play it back, play the wake cue, and demo ducking."""
    io = create_audio_io(settings.audio)
    target_samples = int(settings.audio.capture_rate * duration_s)
    captured = 0
    frames: list[bytes] = []

    async with io:
        devices = [d.name for d in io.list_devices()]
        async for frame in io.capture_stream():
            frames.append(frame)
            captured += len(frame) // 2  # int16 mono
            if captured >= target_samples:
                break

        pcm = b"".join(frames)
        log.info("captured", samples=captured)
        await io.play_pcm(pcm, io.capture_format)

        wake = settings.wakeword.wake_sound
        if wake:
            log.info("playing_wake_cue", path=wake)
            await io.play_wav(wake)

        log.info("duck_demo", level=settings.audio.duck_level)
        await io.set_music_gain(settings.audio.duck_level)
        await io.set_music_gain(1.0)

    return {
        "backend": settings.audio.backend,
        "devices": devices,
        "captured_samples": captured,
        "played_bytes": len(pcm),
    }


async def run_led_test(settings: Settings, state: str | None = None) -> dict[str, object]:
    """Drive one LED state, or cycle through all of them."""
    host = create_xvf_host(settings.respeaker)
    controller = LedController(host, settings.feedback.led)

    states = [LedState(state)] if state and state != "all" else list(LedState)

    for s in states:
        log.info("led_state", state=s.value)
        await controller.show(s)

    return {"host": type(host).__name__, "shown": [s.value for s in states]}


async def run_wake_test(settings: Settings, *, seconds: float = 20.0) -> dict[str, object]:
    """Listen for the wake word for ``seconds``; play the cue + log each detection."""
    io = create_audio_io(settings.audio)
    detector = create_wake_detector(settings.wakeword, settings.audio.capture_rate)
    detections: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()

    async with io:
        log.info("wake_listening", engine=settings.wakeword.engine, seconds=seconds)
        start = loop.time()
        async for frame in io.capture_stream():
            event = detector.process(frame)
            if event is not None:
                detections.append(
                    {
                        "model": event.model,
                        "score": round(event.score, 3),
                        "preroll_ms": event.preroll_ms,
                        "at_s": round(loop.time() - start, 2),
                    }
                )
                if settings.wakeword.wake_sound:
                    await io.play_wav(settings.wakeword.wake_sound)
            if loop.time() - start >= seconds:
                break

    return {
        "engine": settings.wakeword.engine,
        "seconds": seconds,
        "count": len(detections),
        "detections": detections,
    }


async def run_realtime_test(settings: Settings, *, seconds: float = 30.0) -> dict[str, object]:
    """Open a realtime session for ``seconds``: speak, hear the reply, barge in."""
    io = create_audio_io(settings.audio)
    summary: dict[str, Any] = {
        "connected": False,
        "user_transcripts": [],
        "assistant_transcripts": [],
        "audio_bytes": 0,
        "responses": 0,
        "response_statuses": [],
        "barge_ins": 0,
        "tool_calls": [],
        "errors": [],
    }

    def on_event(ev: dict[str, Any]) -> None:
        kind = ev["kind"]
        if kind == "connected":
            summary["connected"] = True
            log.info("connected")
        elif kind == "user_transcript" and ev.get("final"):
            summary["user_transcripts"].append(ev["text"])
            log.info("user", text=ev["text"])
        elif kind == "assistant_transcript":
            summary["assistant_transcripts"].append(ev["text"])
            log.info("assistant", text=ev["text"])
        elif kind == "audio":
            summary["audio_bytes"] += ev["bytes"]
        elif kind == "response_done":
            summary["responses"] += 1
            summary["response_statuses"].append(ev.get("status", ""))
            log.info("response_done", status=ev.get("status", ""))
        elif kind == "barge_in":
            summary["barge_ins"] += 1
            log.info("barge_in")
        elif kind == "tool_call":
            summary["tool_calls"].append({"name": ev["name"], "arguments": ev["arguments"]})
        elif kind == "error":
            summary["errors"].append({"type": ev["type"], "message": ev["message"]})

    session = RealtimeSession(
        settings.realtime,
        io,
        capture_rate=settings.audio.capture_rate,
        playback_rate=settings.audio.playback_rate,
        on_event=on_event,
    )
    async with io:
        await session.run(duration_s=seconds)
    return summary


async def run_media_test(settings: Settings, *, seconds: float = 30.0) -> dict[str, object]:
    """Start the sendspin daemon for a while so MA can auto-discover the player."""
    daemon = SendspinDaemon(settings.media.sendspin, default_name=settings.device.name)
    log.info("media_test", name=daemon.name, hint="check Music Assistant for this player")
    await daemon.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        running = daemon.is_running()
        await daemon.stop()
    return {"name": daemon.name, "argv": daemon.argv(), "ran": running, "seconds": seconds}


async def run_respeaker_tune(settings: Settings) -> dict[str, object]:
    """Apply the configured DSP tuning and read the params back."""
    host = create_xvf_host(settings.respeaker)
    tuning = settings.respeaker.tuning
    await host.apply_tuning(tuning, save=settings.respeaker.save_to_flash)
    readback = {name: await host.get_param(name) for name in tuning}
    return {
        "host": type(host).__name__,
        "applied": list(tuning),
        "readback": readback,
        "saved": settings.respeaker.save_to_flash and bool(tuning),
    }


async def run_arbitration_test(settings: Settings, *, seconds: float = 20.0) -> dict[str, object]:
    """Exercise multi-device wake arbitration over the real UDP broadcast wire.

    Forces the arbitrator on (regardless of ``arbitration.enabled``) so two devices
    on a LAN can confirm they see each other: it joins the configured community,
    prints peers it discovers, and every few seconds fires a synthetic wake of
    random loudness and reports whether this unit would win it.
    """
    import random
    from array import array

    from voiceagent.arbitration import UdpBroadcastTransport, WakeArbitrator, wake_strength
    from voiceagent.wakeword.base import WakeEvent

    arb = settings.arbitration
    transport = UdpBroadcastTransport(arb.port, arb.broadcast_address)
    arbitrator = WakeArbitrator(arb, transport, device_id=settings.device.name)
    await arbitrator.start()
    log.info("arbitration_test_started", device_id=arbitrator.device_id,
             community=arb.community, port=arb.port)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    probes = wins = 0
    try:
        while loop.time() < deadline:
            await asyncio.sleep(min(3.0, max(0.0, deadline - loop.time())))
            if loop.time() >= deadline:
                break
            amp = random.randint(2000, 30000)
            pcm = array("h", [amp, -amp] * 400).tobytes()
            event = WakeEvent("diag", 0.9, pcm, settings.audio.capture_rate)
            won = await arbitrator.should_handle(event)
            probes += 1
            wins += int(won)
            log.info("arbitration_probe", strength=round(wake_strength(event), 3),
                     won=won, peers=arbitrator.peers())
    finally:
        peers = arbitrator.peers()
        await arbitrator.stop()
    return {
        "device_id": arbitrator.device_id,
        "community": arb.community,
        "probes": probes,
        "wins": wins,
        "peers": peers,
    }
