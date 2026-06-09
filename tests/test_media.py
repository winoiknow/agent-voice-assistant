from __future__ import annotations

import asyncio

from voiceagent.config import SendspinConfig
from voiceagent.media import SendspinDaemon


def test_argv_mdns_mode() -> None:
    d = SendspinDaemon(SendspinConfig(name="kitchen"), default_name="dev")
    argv = d.argv()
    assert argv[:2] == ["sendspin", "daemon"]
    assert "--name" in argv and "kitchen" in argv
    assert "--url" not in argv  # mDNS discovery mode


def test_argv_with_url_and_device_and_extra() -> None:
    cfg = SendspinConfig(
        name="den", server_url="ws://1.2.3.4:8928", audio_device="pipewire",
        extra_args=["--disable-mpris"],
    )
    argv = SendspinDaemon(cfg).argv()
    assert "--url" in argv and "ws://1.2.3.4:8928" in argv
    assert "--audio-device" in argv and "pipewire" in argv
    assert "--disable-mpris" in argv


def test_name_defaults_to_device_name() -> None:
    d = SendspinDaemon(SendspinConfig(), default_name="ha-panel")
    assert d.name == "ha-panel"


class _FakeDaemon(SendspinDaemon):
    def argv(self) -> list[str]:
        # A harmless long-running process to exercise the lifecycle.
        return ["sleep", "30"]


async def test_start_stop_lifecycle() -> None:
    d = _FakeDaemon(SendspinConfig(), default_name="dev")
    assert not d.is_running()
    await d.start()
    assert d.is_running()
    await d.stop()
    assert not d.is_running()


async def test_start_is_idempotent() -> None:
    d = _FakeDaemon(SendspinConfig(), default_name="dev")
    await d.start()
    proc = d._proc
    await d.start()  # no second process
    assert d._proc is proc
    await d.stop()


async def test_missing_binary_raises() -> None:
    d = SendspinDaemon(SendspinConfig(binary="/no/such/sendspin"), default_name="dev")
    try:
        await asyncio.wait_for(d.start(), timeout=2.0)
        raised = False
    except RuntimeError:
        raised = True
    assert raised
