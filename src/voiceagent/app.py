"""Application entrypoint: build the subsystems and run the orchestrator.

Owns the async lifecycle (graceful shutdown on SIGINT/SIGTERM) and wires the
configured backends — audio, wake detector, reSpeaker LED — into the orchestrator
state machine. Backends are mock or real per config, so `voiceagent run` works on
a laptop (mock) and on the SBC (hardware) unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from functools import partial

from voiceagent.audio import create_audio_io
from voiceagent.config import Settings
from voiceagent.logging_setup import get_logger
from voiceagent.orchestrator import Orchestrator
from voiceagent.respeaker import LedController, create_xvf_host
from voiceagent.wakeword import create_wake_detector

log = get_logger("app")


class App:
    """Owns the runtime lifecycle of the voice assistant."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._orchestrator: Orchestrator | None = None

    def request_shutdown(self, *, reason: str = "signal") -> None:
        log.info("shutdown_requested", reason=reason)
        if self._orchestrator is not None:
            self._orchestrator.request_shutdown()

    def _build_orchestrator(self) -> Orchestrator:
        audio = create_audio_io(self.settings.audio)
        wake = create_wake_detector(self.settings.wakeword, self.settings.audio.capture_rate)
        led = LedController(create_xvf_host(self.settings.respeaker), self.settings.feedback.led)
        return Orchestrator(self.settings, audio, wake, led)

    async def run(self) -> None:
        """Build subsystems and run the orchestrator until shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Signal handlers are unavailable on some platforms / non-main threads.
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, partial(self.request_shutdown, reason=sig.name))

        log.info(
            "starting",
            device=self.settings.device.name,
            wire_format=("16k-native" if self.settings.realtime.native_16k else "24k-openai"),
        )
        self._orchestrator = self._build_orchestrator()
        await self._orchestrator.run()
        log.info("stopped")


async def run_app(settings: Settings) -> None:
    await App(settings).run()
