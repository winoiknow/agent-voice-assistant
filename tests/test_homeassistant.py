from __future__ import annotations

import json
from typing import Any

import httpx

from voiceagent.media.homeassistant import HomeAssistantClient


def _client(handler: Any) -> HomeAssistantClient:
    return HomeAssistantClient(
        "http://ha.local:8123/", "tok-123", transport=httpx.MockTransport(handler)
    )


async def test_media_pause_and_play_post_correct_service() -> None:
    seen: list[tuple[str, dict[str, Any], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.url.path, body, request.headers.get("authorization", "")))
        return httpx.Response(200, json=[])

    async with _client(handler) as ha:
        await ha.media_pause("media_player.ha_panel_voice")
        await ha.media_play("media_player.ha_panel_voice")

    assert seen[0][0] == "/api/services/media_player/media_pause"
    assert seen[0][1] == {"entity_id": "media_player.ha_panel_voice"}
    assert seen[0][2] == "Bearer tok-123"
    assert seen[1][0] == "/api/services/media_player/media_play"


async def test_is_playing_reads_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states/media_player.ha_panel_voice"
        return httpx.Response(200, json={"state": "playing"})

    async with _client(handler) as ha:
        assert await ha.is_playing("media_player.ha_panel_voice") is True


async def test_is_playing_false_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client(handler) as ha:
        assert await ha.is_playing("media_player.x") is False


async def test_announce_sets_announce_flag() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        captured["path"] = request.url.path
        return httpx.Response(200, json=[])

    async with _client(handler) as ha:
        await ha.announce("media_player.ha_panel_voice", "Dinner is ready")

    assert captured["path"] == "/api/services/media_player/play_media"
    assert captured["announce"] is True
    assert captured["media_content_id"] == "Dinner is ready"
