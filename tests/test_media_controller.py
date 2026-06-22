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


async def test_duck_preducks_when_idle_so_mid_turn_music_is_ducked() -> None:
    # Agent is a DJ: nothing playing at engage. Duck mode pre-ducks anyway, so when
    # the agent starts music mid-reply it comes in already ducked (not full blast,
    # which would drown out the user's next command).
    ha = FakeHA(playing=False, volume=0.9)
    mc = _controller(_settings(media_over={"duck_level": 0.25}), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.calls[-1] == ("volume", 0.25)  # pre-ducked even though idle
    ha.playing = True  # the agent's play_media tool starts music -> at 0.25
    await mc.on_speaking()  # idempotent: already ducked this turn
    assert ha.calls.count(("volume", 0.25)) == 1
    await mc.on_turn_end()
    assert ha.calls[-1] == ("volume", 0.9)  # restored on close
    await mc.stop()


async def test_unduck_honors_agent_volume_change_mid_turn() -> None:
    # User asks the agent to change the volume during the turn. The agent sets it via
    # Music Assistant while we're ducked. On close we must NOT stomp it back to the
    # pre-turn level — we honor the user's requested volume.
    ha = FakeHA(playing=True, volume=0.8)
    mc = _controller(_settings(media_over={"duck_level": 0.25}), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.volume == 0.25  # ducked, saved=0.8
    await ha.set_volume("e", 0.5)  # agent changes volume mid-turn
    await mc.on_turn_end()
    assert ha.volume == 0.5  # honored, not restored to 0.8
    await mc.stop()


async def test_unduck_restores_when_agent_left_volume_alone() -> None:
    ha = FakeHA(playing=True, volume=0.8)
    mc = _controller(_settings(media_over={"duck_level": 0.25}), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.volume == 0.25
    await mc.on_turn_end()
    assert ha.volume == 0.8  # untouched by agent -> restore pre-turn level
    await mc.stop()


async def test_duck_skipped_when_already_at_or_below_duck_level() -> None:
    # Player already muted (0.0). Ducking would set 0.25 then "restore" to the
    # saved 0.0 on close, re-cementing the mute every turn. Guard: leave it alone.
    ha = FakeHA(playing=True, volume=0.0)
    mc = _controller(_settings(media_over={"duck_level": 0.25}), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.calls == []  # never touched the volume
    assert ha.volume == 0.0
    await mc.on_turn_end()
    assert ha.calls == []  # nothing restored -> no mute trap
    assert ha.volume == 0.0
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


async def test_pause_mode_no_action_when_not_playing() -> None:
    ha = FakeHA(playing=False)
    mc = _controller(_settings(media_over={"on_turn": "pause"}), ha)
    await mc.start()
    await mc.on_turn_start()
    assert ha.calls == []  # pause only acts on a stream that's actually playing
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
