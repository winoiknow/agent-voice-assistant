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
        """Pause music (and/or duck) when a voice turn begins."""
        if self.settings.audio.music_target:
            with contextlib.suppress(Exception):
                await self.audio.set_music_gain(self.settings.audio.duck_level)
        if self._ha is not None and self._media.pause_via_ha and self._entity:
            try:
                if await self._ha.is_playing(self._entity):
                    await self._ha.media_pause(self._entity)
                    self._paused = True
                    log.info("music_paused", entity=self._entity)
            except Exception as exc:
                log.warning("music_pause_failed", error=str(exc))

    async def on_turn_end(self) -> None:
        """Resume music (and/or unduck) when the conversation closes."""
        if self._paused and self._ha is not None and self._entity:
            try:
                await self._ha.media_play(self._entity)
                log.info("music_resumed", entity=self._entity)
            except Exception as exc:
                log.warning("music_resume_failed", error=str(exc))
            self._paused = False
        if self.settings.audio.music_target:
            with contextlib.suppress(Exception):
                await self.audio.set_music_gain(1.0)
