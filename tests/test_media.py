from __future__ import annotations

import asyncio

from voiceagent.config import SendspinConfig
from voiceagent.media import SendspinDaemon


def test_argv_mdns_mode_is_default() -> None:
    # positional name, -l level, no url (mDNS discovery), no port flag.
    d = SendspinDaemon(SendspinConfig(name="kitchen"), default_name="dev")
    argv = d.argv()
    assert argv[0] == "sendspin-cpp"
    assert argv[1] == "kitchen"  # positional friendly name
    assert argv[argv.index("-l") + 1] == "info"  # INFO -> cpp's lowercase 'info'
    assert "-p" not in argv  # v0.6.1 basic_client has no port flag (fixed 8928)
    assert "-u" not in argv  # mDNS discovery mode
    assert "-i" not in argv  # no client_id set -> binary derives a slug


def test_argv_pins_client_id_when_set() -> None:
    cfg = SendspinConfig(name="ha-panel-voice", client_id="ha-panel-voice")
    argv = SendspinDaemon(cfg).argv()
    assert argv[argv.index("-i") + 1] == "ha-panel-voice"


def test_argv_cpp_with_url_port_and_extra() -> None:
    cfg = SendspinConfig(
        name="den", server_url="ws://1.2.3.4:8928", port=8930,
        log_level="WARNING", binary="/opt/sendspin/basic_client",
        extra_args=["--foo"],
    )
    argv = SendspinDaemon(cfg).argv()
    assert argv[0] == "/opt/sendspin/basic_client"  # explicit binary honored
    assert "-u" in argv and "ws://1.2.3.4:8928" in argv
    assert argv[argv.index("-p") + 1] == "8930"
    assert argv[argv.index("-l") + 1] == "warn"  # WARNING -> 'warn'
    assert "--foo" in argv


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
