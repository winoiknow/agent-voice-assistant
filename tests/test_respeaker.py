from __future__ import annotations

from voiceagent.config import LedConfig, RespeakerConfig
from voiceagent.respeaker import LedState, create_xvf_host
from voiceagent.respeaker.base import LedEffect
from voiceagent.respeaker.led import LedController
from voiceagent.respeaker.mock import MockXvfHost


async def test_apply_tuning_sets_and_saves() -> None:
    host = MockXvfHost()
    await host.apply_tuning({"AUDIO_MGR_MIC_GAIN": [10], "PP_AGCGAIN": [1]}, save=True)
    assert host.params["AUDIO_MGR_MIC_GAIN"] == [10]
    assert host.params["PP_AGCGAIN"] == [1]
    assert host.saved == 1


async def test_apply_tuning_no_save_when_empty() -> None:
    host = MockXvfHost()
    await host.apply_tuning({}, save=True)
    assert host.saved == 0


async def test_get_param_roundtrip() -> None:
    host = MockXvfHost()
    await host.set_param("X", [1.5, 2.0])
    assert await host.get_param("X") == [1.5, 2.0]
    assert await host.get_param("missing") == []


async def test_led_thinking_is_breath_blue() -> None:
    host = MockXvfHost()
    ctrl = LedController(host, LedConfig(think_color=(0, 0, 255)))
    await ctrl.show(LedState.THINKING)
    assert host.led.effect is LedEffect.BREATH
    assert host.led.color == (0, 0, 255)


async def test_led_idle_turns_off() -> None:
    host = MockXvfHost()
    ctrl = LedController(host, LedConfig())
    await ctrl.show(LedState.SPEAKING)
    await ctrl.show(LedState.IDLE)
    assert host.led.effect is LedEffect.OFF


async def test_led_disabled_is_noop() -> None:
    host = MockXvfHost()
    ctrl = LedController(host, LedConfig(enabled=False))
    await ctrl.show(LedState.SPEAKING)
    assert host.commands == []


async def test_led_listening_nearest_primitive_green() -> None:
    host = MockXvfHost()
    ctrl = LedController(host, LedConfig(listen_color=(0, 255, 0)))
    await ctrl.show(LedState.LISTENING)
    # motion not in firmware -> steady single green
    assert host.led.effect is LedEffect.SINGLE
    assert host.led.color == (0, 255, 0)


def test_factory_simulate_returns_mock() -> None:
    assert isinstance(create_xvf_host(RespeakerConfig(simulate=True)), MockXvfHost)


def test_factory_real_when_not_simulated() -> None:
    from voiceagent.respeaker.xvf_host import RealXvfHost

    host = create_xvf_host(RespeakerConfig(simulate=False, transport="i2c"))
    assert isinstance(host, RealXvfHost)
    assert host._argv("LED_EFFECT", 1) == ["xvf_host", "-u", "i2c", "LED_EFFECT", "1"]


async def test_real_led_color_packs_to_uint32(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from voiceagent.respeaker.xvf_host import RealXvfHost

    host = RealXvfHost()
    calls: list[tuple[object, ...]] = []

    async def fake_run(command: str, *args: object) -> str:
        calls.append((command, *args))
        return ""

    monkeypatch.setattr(host, "_run", fake_run)
    await host.led_color((0, 255, 0))  # green -> 0x00FF00
    await host.led_color((255, 0, 0))  # red   -> 0xFF0000
    await host.led_color((0, 0, 255))  # blue  -> 0x0000FF
    assert calls == [
        ("LED_COLOR", 0x00FF00),
        ("LED_COLOR", 0xFF0000),
        ("LED_COLOR", 0x0000FF),
    ]


async def test_real_get_param_ignores_device_banner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from voiceagent.respeaker.xvf_host import RealXvfHost

    host = RealXvfHost()

    async def fake_run(name: str, *args: object) -> str:
        # Banner numbers must NOT pollute the parsed value.
        return (
            "Device (USB)::device_init() -- Found device VID: 10374 PID: 26 interface: 3\n"
            "AUDIO_MGR_MIC_GAIN 10\n"
        )

    monkeypatch.setattr(host, "_run", fake_run)
    assert await host.get_param("AUDIO_MGR_MIC_GAIN") == [10.0]
