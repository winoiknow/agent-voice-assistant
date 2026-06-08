from __future__ import annotations

import asyncio

from voiceagent.app import App
from voiceagent.config import load_config


async def test_app_runs_and_shuts_down() -> None:
    settings = load_config(None)
    app = App(settings)

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        app.request_shutdown(reason="test")

    # run() must return promptly once shutdown is requested.
    await asyncio.wait_for(asyncio.gather(app.run(), _stop_soon()), timeout=2.0)


async def test_request_shutdown_is_idempotent() -> None:
    app = App(load_config(None))
    app.request_shutdown()
    app.request_shutdown()  # second call must not raise
    assert app._shutdown.is_set()


def test_run_via_load() -> None:
    # Smoke: constructing App from default config does not raise.
    App(load_config(None))
