"""Media controller: sendspin sidecar + pause/resume around a voice turn.

The sendspin daemon runs as a managed sidecar (so its Music Assistant / Home
Assistant ``media_player`` entity exists only while we're up). On a voice turn we
pause the player via Home Assistant — which also frees the audio device for TTS —
and resume it when the conversation closes. An optional local PipeWire duck runs in
parallel when a ``music_target`` is configured.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from voiceagent.audio.base import AudioIO
from voiceagent.config import HomeAssistantConfig, Settings
from voiceagent.logging_setup import get_logger
from voiceagent.media.homeassistant import HomeAssistantClient
from voiceagent.media.sendspin import SendspinDaemon

log = get_logger("media.controller")

HaClientFactory = Callable[[HomeAssistantConfig], Any]

# How long/often to wait for the player to (re)connect to MA before restoring its
# startup volume. The player advertises at 0.0 on connect; ~60 s covers reconnect.
_STARTUP_POLL_INTERVAL_S = 2.0
_STARTUP_POLL_TRIES = 30


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
        self._startup_task: asyncio.Task[None] | None = None
        # Persist the last-known listening volume next to the log file so we can
        # restore it after the player boots muted (see _restore_startup_volume).
        log_file = settings.logging.file
        state_dir = (
            Path(log_file).expanduser().parent
            if log_file
            else Path.home() / ".local" / "state" / "voiceagent"
        )
        self._volume_state_path = state_dir / "last_volume"

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
            # The player boots at volume 0.0 (silent) on every reconnect; restore a
            # usable level in the background once it comes up.
            self._startup_task = asyncio.create_task(self._restore_startup_volume())

    async def stop(self) -> None:
        if self._startup_task is not None:
            self._startup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._startup_task
            self._startup_task = None
        if self._ha is not None:
            with contextlib.suppress(Exception):
                await self._ha.aclose()
            self._ha = None
        if self._daemon is not None:
            await self._daemon.stop()

    def _load_last_volume(self) -> float | None:
        """Read the persisted last-known listening volume, if any (0..1)."""
        try:
            v = float(self._volume_state_path.read_text().strip())
        except Exception:
            return None
        return v if 0.0 < v <= 1.0 else None

    def _save_last_volume(self, level: float) -> None:
        """Persist a genuine listening level so it survives a restart. Best effort."""
        if not (0.0 < level <= 1.0):
            return
        try:
            self._volume_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._volume_state_path.write_text(f"{level:.4f}")
        except Exception as exc:
            log.debug("last_volume_save_failed", error=str(exc))

    async def _restore_startup_volume(self) -> None:
        """Restore a usable volume after the player (re)connects muted.

        The sendspin-cpp player re-registers at volume 0.0 on every connect, so a
        reboot/restart leaves it silent. Wait (polling) for the player to report a
        volume; if it came up at 0.0, set it to the last-known level — or the
        configured ``startup_volume`` default when there's no record. A player that
        comes up with a real volume is left untouched.
        """
        if self._ha is None or not self._entity:
            return
        try:
            for _ in range(_STARTUP_POLL_TRIES):
                await asyncio.sleep(_STARTUP_POLL_INTERVAL_S)
                try:
                    cur = await self._ha.get_volume(self._entity)
                except Exception:
                    continue  # entity still unavailable / HA hiccup — keep waiting
                if cur is None:
                    continue
                if cur > 0.001:
                    return  # came up with a usable volume; leave it alone
                target = self._load_last_volume() or self._media.startup_volume
                source = "last_known" if self._load_last_volume() else "default"
                if target <= 0.0:
                    return
                await self._ha.set_volume(self._entity, target)
                log.info("startup_volume_restored", entity=self._entity,
                         to=target, source=source)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("startup_volume_restore_failed", error=str(exc))

    async def on_turn_start(self) -> None:
        """Duck or pause the player when a voice turn begins (only if it's playing)."""
        if self.settings.audio.music_target:  # optional instant local PipeWire duck
            with contextlib.suppress(Exception):
                await self.audio.set_music_gain(self.settings.audio.duck_level)
        await self._attenuate()

    async def on_speaking(self) -> None:
        """Re-assert the duck when the agent starts speaking. The agent can start
        music itself mid-turn (Music Assistant tools), so it may not have been
        playing at on_turn_start — this catches that and ducks under the reply."""
        await self._attenuate()

    async def _attenuate(self) -> None:
        """Duck/pause the player for the turn (once). In duck mode we lower the
        volume even when nothing is playing yet, so music the agent starts later in
        the turn (Music Assistant tools) comes in already ducked instead of blasting
        over the reply — which otherwise drowns out the user's next command."""
        if self._ha is None or not self._entity or self._paused or self._ducked:
            return
        try:
            if self._media.on_turn == "pause":
                # Pause only makes sense for a stream that's actually playing.
                if not await self._ha.is_playing(self._entity):
                    return
                await self._ha.media_pause(self._entity)
                self._paused = True
                log.info("music_paused", entity=self._entity)
            else:  # duck: lower the player volume up front; restore it on close.
                saved = await self._ha.get_volume(self._entity)
                # If the player is already at/below the duck level (e.g. muted at
                # 0.0), there's nothing to duck — and "restoring" a ~0 level on
                # close would just keep it silent, a self-sustaining mute trap
                # across turns. Leave it untouched.
                if saved is not None and saved <= self._media.duck_level:
                    log.info("music_duck_skipped", entity=self._entity,
                             was=saved, duck=self._media.duck_level)
                    return
                self._saved_volume = saved
                self._save_last_volume(saved)  # remember the listening level
                await self._ha.set_volume(self._entity, self._media.duck_level)
                self._ducked = True
                log.info("music_ducked", entity=self._entity,
                         to=self._media.duck_level, was=self._saved_volume)
        except Exception as exc:
            log.warning("music_duck_pause_failed", error=str(exc))

    async def _settled_volume(self) -> float | None:
        """Read the player volume, polling briefly until two consecutive reads agree.

        HA's reported ``volume_level`` trails Music Assistant by up to ~1 s, so a read
        taken immediately at turn end can still show our ducked value even when the
        agent set a new volume mid-turn. Polling to a stable reading lets us tell the
        two apart.
        """
        prev: float | None = None
        last: float | None = None
        for _ in range(6):  # ~1.2 s worst case
            try:
                last = await self._ha.get_volume(self._entity)
            except Exception:
                return prev
            if prev is not None and abs(last - prev) <= 0.01:
                return last
            prev = last
            await asyncio.sleep(0.2)
        return last

    async def on_turn_end(self) -> None:
        """Restore the player (resume or un-duck) when the conversation closes."""
        if self._ha is not None and self._entity:
            try:
                if self._paused:
                    await self._ha.media_play(self._entity)
                    log.info("music_resumed", entity=self._entity)
                elif self._ducked:
                    # Restore the pre-turn volume — unless the agent changed it this
                    # turn (e.g. via Music Assistant's volume_set, which lands directly
                    # on the sendspin-cpp player), in which case keep the agent's level
                    # rather than stomping it back. HA's volume state lags MA, so read
                    # until it settles before deciding — a single read can still show
                    # the ducked value.
                    cur = await self._settled_volume()
                    changed = cur is not None and abs(cur - self._media.duck_level) > 0.02
                    restore = cur if changed else self._saved_volume
                    if restore is not None and restore != cur:
                        await self._ha.set_volume(self._entity, restore)
                    if restore is not None:
                        self._save_last_volume(restore)  # remember for next boot
                    log.info(
                        "music_unducked", entity=self._entity, to=restore,
                        settled=cur, agent_changed=changed,
                    )
            except Exception as exc:
                log.warning("music_restore_failed", error=str(exc))
        self._paused = False
        self._ducked = False
        self._saved_volume = None
        if self.settings.audio.music_target:
            with contextlib.suppress(Exception):
                await self.audio.set_music_gain(1.0)
