"""Application skeleton.

Phase 2 wires nothing to hardware: it owns the async lifecycle (startup, graceful
shutdown on SIGINT/SIGTERM) and the place where subsystems — audio, wakeword,
realtime client, media controller, feedback — will be started and stopped in later
phases. Keeping this skeleton small and correct now means each subsystem drops into
a proven lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from functools import partial

from voiceagent.config import Settings
from voiceagent.logging_setup import get_logger

log = get_logger("app")


class App:
    """Owns the runtime lifecycle of the voice assistant."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()

    def request_shutdown(self, *, reason: str = "signal") -> None:
        if not self._shutdown.is_set():
            log.info("shutdown_requested", reason=reason)
            self._shutdown.set()

    async def startup(self) -> None:
        # Subsystems are constructed/started here in later phases.
        log.info(
            "starting",
            device=self.settings.device.name,
            realtime_host=self.settings.realtime.host,
            realtime_port=self.settings.realtime.port,
            wire_format=("16k-native" if self.settings.realtime.native_16k else "24k-openai"),
        )

    async def shutdown(self) -> None:
        # Subsystems are stopped here (reverse order) in later phases.
        log.info("stopped")

    async def run(self) -> None:
        """Run until a shutdown signal arrives, then clean up."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Signal handlers are unavailable on some platforms / non-main threads.
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, partial(self.request_shutdown, reason=sig.name))

        await self.startup()
        try:
            log.info("ready")
            await self._shutdown.wait()
        finally:
            await self.shutdown()


async def run_app(settings: Settings) -> None:
    await App(settings).run()
