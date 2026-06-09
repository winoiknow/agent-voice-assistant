from __future__ import annotations

import asyncio

from voiceagent.app import App
from voiceagent.config import Settings


def _mock_settings() -> Settings:
    return Settings(
        audio={"backend": "mock"},
        wakeword={"engine": "mock"},  # silence won't trigger -> stays idle
        respeaker={"simulate": True},
    )


async def test_app_runs_and_shuts_down() -> None:
    app = App(_mock_settings())

    async def stop_soon() -> None:
        await asyncio.sleep(0.1)  # let run() build the orchestrator
        app.request_shutdown(reason="test")

    # App idles in the wake loop (mock silence never fires) until shutdown.
    await asyncio.wait_for(asyncio.gather(app.run(), stop_soon()), timeout=3.0)


def test_request_shutdown_before_run_is_safe() -> None:
    App(_mock_settings()).request_shutdown()  # orchestrator not built yet; no error
