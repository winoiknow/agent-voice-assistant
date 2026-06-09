from __future__ import annotations

from typing import Any

from voiceagent.audio.mock import MockAudioIO
from voiceagent.audio.types import AudioFormat
from voiceagent.config import Settings
from voiceagent.media import MediaController


class FakeHA:
    def __init__(self, playing: bool = True) -> None:
        self.playing = playing
        self.calls: list[tuple[str, str]] = []

    async def is_playing(self, entity: str) -> bool:
        return self.playing

    async def media_pause(self, entity: str) -> None:
        self.calls.append(("pause", entity))
        self.playing = False

    async def media_play(self, entity: str) -> None:
        self.calls.append(("play", entity))
        self.playing = True

    async def aclose(self) -> None:
        pass


def _settings(**ha: Any) -> Settings:
    base = {
        "enabled": True,
        "base_url": "https://ha.local",
        "token": "tok",
        "media_player_entity": "media_player.ha_panel_voice",
    }
    base.update(ha)
    return Settings(
        audio={"backend": "mock"},
        media={"sendspin": {"enabled": False}, "home_assistant": base},
    )


def _controller(settings: Settings, ha: FakeHA) -> MediaController:
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000))
    return MediaController(settings, io, ha_client_factory=lambda _cfg: ha)


async def test_pause_on_turn_start_resume_on_end() -> None:
    ha = FakeHA(playing=True)
    mc = _controller(_settings(), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.calls[-1] == ("pause", "media_player.ha_panel_voice")
    await mc.on_turn_end()
    assert ha.calls[-1] == ("play", "media_player.ha_panel_voice")
    await mc.stop()


async def test_no_pause_when_not_playing() -> None:
    ha = FakeHA(playing=False)
    mc = _controller(_settings(), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.calls == []  # nothing playing -> nothing to pause
    await mc.on_turn_end()
    assert ha.calls == []  # we never paused -> don't resume
    await mc.stop()


async def test_ha_disabled_is_noop() -> None:
    ha = FakeHA(playing=True)
    mc = _controller(_settings(enabled=False), ha)
    await mc.start()
    await mc.on_turn_start()
    await mc.on_turn_end()
    assert ha.calls == []
    await mc.stop()


async def test_resume_failure_is_swallowed() -> None:
    class BrokenHA(FakeHA):
        async def media_play(self, entity: str) -> None:
            raise RuntimeError("ha down")

    ha = BrokenHA(playing=True)
    mc = _controller(_settings(), ha)
    await mc.start()
    await mc.on_turn_start()
    await mc.on_turn_end()  # must not raise even if resume fails
    await mc.stop()
