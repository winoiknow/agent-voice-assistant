"""Media controller: sendspin sidecar + pause/resume around a voice turn.

The sendspin daemon runs as a managed sidecar (so its Music Assistant / Home
Assistant ``media_player`` entity exists only while we're up). On a voice turn we
pause the player via Home Assistant — which also frees the audio device for TTS —
and resume it when the conversation closes. An optional local PipeWire duck runs in
parallel when a ``music_target`` is configured.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from voiceagent.audio.base import AudioIO
from voiceagent.config import HomeAssistantConfig, Settings
from voiceagent.logging_setup import get_logger
from voiceagent.media.homeassistant import HomeAssistantClient
from voiceagent.media.sendspin import SendspinDaemon

log = get_logger("media.controller")

HaClientFactory = Callable[[HomeAssistantConfig], Any]


class MediaController:
    def __init__(
        self,
        settings: Settings,
        audio_io: AudioIO,
        *,
        ha_client_factory: HaClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.audio = audio_io
        self._media = settings.media
        self._daemon = (
            SendspinDaemon(self._media.sendspin, default_name=settings.device.name)
            if self._media.sendspin.enabled
            else None
        )
        self._ha_factory = ha_client_factory or self._default_ha
        self._ha: Any = None
        self._paused = False
        self._ducked = False
        self._saved_volume: float | None = None

    @staticmethod
    def _default_ha(cfg: HomeAssistantConfig) -> HomeAssistantClient:
        assert cfg.base_url is not None and cfg.token is not None
        return HomeAssistantClient(cfg.base_url, cfg.token.get_secret_value())

    @property
    def _entity(self) -> str | None:
        return self._media.home_assistant.media_player_entity

    def _ha_ready(self) -> bool:
        ha = self._media.home_assistant
        return bool(ha.enabled and ha.base_url and ha.token and ha.media_player_entity)

    async def start(self) -> None:
        if self._daemon is not None:
            await self._daemon.start()
        if self._ha_ready():
            self._ha = self._ha_factory(self._media.home_assistant)
            log.info("media_ha_ready", entity=self._entity)

    async def stop(self) -> None:
        if self._ha is not None:
            with contextlib.suppress(Exception):
                await self._ha.aclose()
            self._ha = None
        if self._daemon is not None:
            await self._daemon.stop()

    async def on_turn_start(self) -> None:
        """Duck or pause the player when a voice turn begins (only if it's playing)."""
        if self.settings.audio.music_target:  # optional instant local PipeWire duck
            with contextlib.suppress(Exception):
                await self.audio.set_music_gain(self.settings.audio.duck_level)
        if self._ha is None or not self._entity:
            return
        try:
            if not await self._ha.is_playing(self._entity):
                return
            if self._media.on_turn == "pause":
                await self._ha.media_pause(self._entity)
                self._paused = True
                log.info("music_paused", entity=self._entity)
            else:  # duck: lower the volume but keep the stream flowing
                self._saved_volume = await self._ha.get_volume(self._entity)
                await self._ha.set_volume(self._entity, self._media.duck_level)
                self._ducked = True
                log.info("music_ducked", entity=self._entity,
                         to=self._media.duck_level, was=self._saved_volume)
        except Exception as exc:
            log.warning("music_duck_pause_failed", error=str(exc))

    async def on_turn_end(self) -> None:
        """Restore the player (resume or un-duck) when the conversation closes."""
        if self._ha is not None and self._entity:
            try:
                if self._paused:
                    await self._ha.media_play(self._entity)
                    log.info("music_resumed", entity=self._entity)
                elif self._ducked:
                    if self._saved_volume is not None:
                        await self._ha.set_volume(self._entity, self._saved_volume)
                    log.info("music_unducked", entity=self._entity, to=self._saved_volume)
            except Exception as exc:
                log.warning("music_restore_failed", error=str(exc))
        self._paused = False
        self._ducked = False
        self._saved_volume = None
        if self.settings.audio.music_target:
            with contextlib.suppress(Exception):
                await self.audio.set_music_gain(1.0)
