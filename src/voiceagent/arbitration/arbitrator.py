"""Multi-device wake arbitration.

When several units in earshot hear one wake word, only the strongest answers.
Each unit broadcasts a tiny announcement on wake — ``{community, device_id, ts,
strength, model}`` where *strength* = wake score + the wake's audio energy (RMS).
A unit collects peers' announcements for a short window, and the highest strength
wins (ties broken by ``device_id`` so every unit agrees); the rest suppress.

Periodic presence beacons let a unit know whether any peers exist at all, so a
**solo** unit (no peers) skips the window and answers immediately — arbitration adds
latency only when peers are actually present. Correlation of "the same wake" across
units leans on the shared (chrony-synced) clock: a peer announcement whose ``ts`` is
within the window of ours is treated as competing for the same utterance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass

from voiceagent.arbitration.transport import ArbitrationTransport
from voiceagent.config import ArbitrationConfig
from voiceagent.logging_setup import get_logger
from voiceagent.wakeword.base import WakeEvent, frame_rms

log = get_logger("arbitration")

_PROTOCOL_VERSION = 1
_RMS_FULL_SCALE = 32767.0


def wake_strength(event: WakeEvent) -> float:
    """Combine detection confidence and loudness (proximity) into one comparable
    scalar: score (0..1) + normalized RMS energy (0..1)."""
    energy = min(frame_rms(event.preroll) / _RMS_FULL_SCALE, 1.0)
    return round(event.score + energy, 6)


@dataclass(frozen=True)
class _PeerWake:
    device_id: str
    strength: float
    ts: float


class WakeArbitrator:
    """Decides whether this unit should handle a wake, or defer to a louder peer."""

    def __init__(self, cfg: ArbitrationConfig, transport: ArbitrationTransport, *,
                 device_id: str) -> None:
        self.cfg = cfg
        self.transport = transport
        self.device_id = cfg.device_id or device_id
        self.community = cfg.community
        self._known_peers: dict[str, float] = {}  # device_id -> last_seen monotonic
        self._recent_wakes: list[_PeerWake] = []   # peers' recent wake announcements
        self._beacon_task: asyncio.Task[None] | None = None
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────
    async def start(self) -> None:
        await self.transport.start(self._on_message)
        self._running = True
        self._beacon_task = asyncio.create_task(self._beacon_loop())
        log.info("arbitration_started", device_id=self.device_id, community=self.community)

    async def stop(self) -> None:
        self._running = False
        if self._beacon_task is not None:
            self._beacon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._beacon_task
            self._beacon_task = None
        await self.transport.stop()

    # ── the decision ─────────────────────────────────────────────
    async def should_handle(self, event: WakeEvent) -> bool:
        """True if this unit should take the turn; False to defer to a louder peer."""
        my_ts = time.time()
        my_strength = wake_strength(event)
        await self._send({
            "v": _PROTOCOL_VERSION, "t": "wake", "community": self.community,
            "device_id": self.device_id, "ts": my_ts, "strength": my_strength,
            "model": event.model,
        })

        if not self._peers_present():
            log.info("arbitration_solo_handle", strength=my_strength)
            return True

        window_s = self.cfg.window_ms / 1000.0
        await asyncio.sleep(window_s)

        competing = [
            w for w in self._recent_wakes if abs(w.ts - my_ts) <= window_s
        ]
        winner_id, winner_strength = self._pick_winner(my_strength, competing)
        won = winner_id == self.device_id
        log.info("arbitration_decided", won=won, winner=winner_id,
                 my_strength=my_strength, winner_strength=winner_strength,
                 peers=len(competing))
        return won

    def _pick_winner(self, my_strength: float, competing: list[_PeerWake]) -> tuple[str, float]:
        best_id, best_strength = self.device_id, my_strength
        for w in competing:
            # Higher strength wins; on a tie the lexicographically smaller device_id
            # wins so every unit reaches the same verdict from the same candidates.
            if w.strength > best_strength or (
                w.strength == best_strength and w.device_id < best_id
            ):
                best_id, best_strength = w.device_id, w.strength
        return best_id, best_strength

    # ── peer tracking ────────────────────────────────────────────
    def peers(self) -> list[str]:
        """Currently-known peer device ids (pruned of stale ones)."""
        self._prune()
        return sorted(self._known_peers)

    def _peers_present(self) -> bool:
        self._prune()
        return bool(self._known_peers)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.cfg.peer_timeout_s
        self._known_peers = {d: t for d, t in self._known_peers.items() if t >= cutoff}
        wake_cutoff = time.time() - max(2.0, 2 * self.cfg.window_ms / 1000.0)
        self._recent_wakes = [w for w in self._recent_wakes if w.ts >= wake_cutoff]

    def _on_message(self, data: bytes) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict) or msg.get("v") != _PROTOCOL_VERSION:
            return
        if msg.get("community") != self.community:
            return  # different community — ignore
        device_id = msg.get("device_id")
        if not isinstance(device_id, str) or device_id == self.device_id:
            return  # ignore our own broadcast (loopback)
        self._known_peers[device_id] = time.monotonic()
        if msg.get("t") == "wake":
            try:
                self._recent_wakes.append(
                    _PeerWake(device_id, float(msg["strength"]), float(msg["ts"]))
                )
            except (KeyError, TypeError, ValueError):
                return

    # ── presence beacon ──────────────────────────────────────────
    async def _beacon_loop(self) -> None:
        while self._running:
            await self._send({
                "v": _PROTOCOL_VERSION, "t": "presence",
                "community": self.community, "device_id": self.device_id,
                "ts": time.time(),
            })
            try:
                await asyncio.sleep(self.cfg.presence_interval_s)
            except asyncio.CancelledError:
                break
            self._prune()

    async def _send(self, msg: dict[str, object]) -> None:
        try:
            await self.transport.send(json.dumps(msg).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - never let arbitration break a turn
            log.warning("arbitration_send_failed", error=str(exc))


__all__ = ["WakeArbitrator", "wake_strength"]
