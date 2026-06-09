"""Minimal Home Assistant REST client for media_player control.

Used to pause/resume (and announce on) the sendspin player that Music Assistant
mirrored into HA. Auth is a long-lived access token. The httpx transport is
injectable so the client is testable without a live HA.
"""

from __future__ import annotations

from typing import Any

from voiceagent.logging_setup import get_logger

log = get_logger("media.homeassistant")


class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 5.0,
        transport: Any = None,
    ) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HomeAssistantClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        resp = await self._client.post(f"/api/services/{domain}/{service}", json=data)
        resp.raise_for_status()
        log.debug("ha_service", domain=domain, service=service, data=data)

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/api/states/{entity_id}")
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def media_pause(self, entity_id: str) -> None:
        await self.call_service("media_player", "media_pause", {"entity_id": entity_id})

    async def media_play(self, entity_id: str) -> None:
        await self.call_service("media_player", "media_play", {"entity_id": entity_id})

    async def set_volume(self, entity_id: str, level: float) -> None:
        await self.call_service(
            "media_player", "volume_set",
            {"entity_id": entity_id, "volume_level": round(max(0.0, min(1.0, level)), 3)},
        )

    async def get_volume(self, entity_id: str) -> float | None:
        try:
            attrs = (await self.get_state(entity_id)).get("attributes", {})
            vol = attrs.get("volume_level")
            return float(vol) if vol is not None else None
        except Exception as exc:
            log.warning("ha_volume_read_error", entity=entity_id, error=str(exc))
            return None

    async def is_playing(self, entity_id: str) -> bool:
        try:
            return (await self.get_state(entity_id)).get("state") == "playing"
        except Exception as exc:  # network/HA hiccup — treat as not playing
            log.warning("ha_state_error", entity=entity_id, error=str(exc))
            return False

    async def announce(self, entity_id: str, message_or_media: str, *, tts: bool = True) -> None:
        """Play an announcement. tts=True speaks ``message_or_media`` via the
        configured TTS engine; otherwise it is treated as a media URL."""
        if tts:
            await self.call_service(
                "media_player", "play_media",
                {"entity_id": entity_id, "media_content_id": message_or_media,
                 "media_content_type": "music", "announce": True},
            )
        else:
            await self.call_service(
                "media_player", "play_media",
                {"entity_id": entity_id, "media_content_id": message_or_media,
                 "media_content_type": "music", "announce": True},
            )
