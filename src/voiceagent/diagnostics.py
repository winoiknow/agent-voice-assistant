"""Hardware bring-up routines behind the Phase 3 CLI test commands.

Each routine works against the configured backend, so they run end-to-end with the
mock backends (``audio.backend: mock``, ``respeaker.simulate: true``) on a laptop and
against real hardware on the SBC, unchanged.
"""

from __future__ import annotations

from voiceagent.audio import create_audio_io
from voiceagent.config import Settings
from voiceagent.logging_setup import get_logger
from voiceagent.respeaker import LedController, LedState, create_xvf_host

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
