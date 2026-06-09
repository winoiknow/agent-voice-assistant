from __future__ import annotations

from typing import Any

from voiceagent.audio.mock import MockAudioIO
from voiceagent.audio.types import AudioFormat
from voiceagent.config import Settings
from voiceagent.media import MediaController


class FakeHA:
    def __init__(self, playing: bool = True, volume: float = 0.8) -> None:
        self.playing = playing
        self.volume = volume
        self.calls: list[tuple[str, Any]] = []

    async def is_playing(self, entity: str) -> bool:
        return self.playing

    async def media_pause(self, entity: str) -> None:
        self.calls.append(("pause", entity))
        self.playing = False

    async def media_play(self, entity: str) -> None:
        self.calls.append(("play", entity))
        self.playing = True

    async def get_volume(self, entity: str) -> float:
        return self.volume

    async def set_volume(self, entity: str, level: float) -> None:
        self.calls.append(("volume", level))
        self.volume = level

    async def aclose(self) -> None:
        pass


def _settings(media_over: dict[str, Any] | None = None, **ha: Any) -> Settings:
    base = {
        "enabled": True,
        "base_url": "https://ha.local",
        "token": "tok",
        "media_player_entity": "media_player.ha_panel_voice",
    }
    base.update(ha)
    media: dict[str, Any] = {"sendspin": {"enabled": False}, "home_assistant": base}
    media.update(media_over or {})
    return Settings(audio={"backend": "mock"}, media=media)


def _controller(settings: Settings, ha: FakeHA) -> MediaController:
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000))
    return MediaController(settings, io, ha_client_factory=lambda _cfg: ha)


async def test_duck_on_turn_start_restore_on_end() -> None:
    # default on_turn = duck
    ha = FakeHA(playing=True, volume=0.8)
    mc = _controller(_settings(media_over={"duck_level": 0.25}), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.calls[-1] == ("volume", 0.25)  # ducked
    assert ha.volume == 0.25
    await mc.on_turn_end()
    assert ha.calls[-1] == ("volume", 0.8)  # restored to the saved level
    assert ha.volume == 0.8
    await mc.stop()


async def test_pause_mode_on_turn_start_resume_on_end() -> None:
    ha = FakeHA(playing=True)
    mc = _controller(_settings(media_over={"on_turn": "pause"}), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.calls[-1] == ("pause", "media_player.ha_panel_voice")
    await mc.on_turn_end()
    assert ha.calls[-1] == ("play", "media_player.ha_panel_voice")
    await mc.stop()


async def test_no_action_when_not_playing() -> None:
    ha = FakeHA(playing=False)
    mc = _controller(_settings(), ha)  # duck mode
    await mc.start()
    await mc.on_turn_start()
    assert ha.calls == []  # nothing playing -> don't touch it
    await mc.on_turn_end()
    assert ha.calls == []
    await mc.stop()


async def test_ha_disabled_is_noop() -> None:
    ha = FakeHA(playing=True)
    mc = _controller(_settings(enabled=False), ha)
    await mc.start()
    await mc.on_turn_start()
    await mc.on_turn_end()
    assert ha.calls == []
    await mc.stop()


async def test_restore_failure_is_swallowed() -> None:
    class BrokenHA(FakeHA):
        async def set_volume(self, entity: str, level: float) -> None:
            if level != 0.25:  # fail only on the restore
                raise RuntimeError("ha down")
            await super().set_volume(entity, level)

    ha = BrokenHA(playing=True, volume=0.9)
    mc = _controller(_settings(media_over={"duck_level": 0.25}), ha)
    await mc.start()
    await mc.on_turn_start()
    await mc.on_turn_end()  # must not raise even if restore fails
    await mc.stop()
