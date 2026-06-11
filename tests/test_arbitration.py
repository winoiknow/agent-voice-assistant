from __future__ import annotations

import asyncio
from array import array

from voiceagent.arbitration import InMemoryBus, InMemoryTransport, WakeArbitrator, wake_strength
from voiceagent.config import ArbitrationConfig
from voiceagent.wakeword.base import WakeEvent


def _pcm(amp: int, n: int = 512) -> bytes:
    """int16-mono PCM whose RMS is exactly ``amp`` (alternating +amp/-amp)."""
    return array("h", [amp, -amp] * (n // 2)).tobytes()


def _wake(amp: int, score: float = 0.9) -> WakeEvent:
    return WakeEvent("belvedere", score, _pcm(amp), 16000)


def _cfg(**over: object) -> ArbitrationConfig:
    base: dict[str, object] = {
        "enabled": True, "community": "house", "window_ms": 100,
        "presence_interval_s": 10.0, "peer_timeout_s": 30.0,
    }
    base.update(over)
    return ArbitrationConfig(**base)


def _arb(bus: InMemoryBus, device_id: str, **over: object) -> WakeArbitrator:
    cfg = _cfg(**over)
    return WakeArbitrator(cfg, InMemoryTransport(bus), device_id=device_id)


async def _start(*arbs: WakeArbitrator) -> None:
    for a in arbs:
        await a.start()
    await asyncio.sleep(0.02)  # let presence beacons exchange so peers are known


# ── strength metric ─────────────────────────────────────────────
def test_wake_strength_combines_score_and_energy() -> None:
    quiet = wake_strength(_wake(amp=1000, score=0.8))
    loud = wake_strength(_wake(amp=20000, score=0.8))
    assert loud > quiet  # louder (closer) wins on equal confidence
    # score(0.8) + energy(1000/32767 ~= 0.0305)
    assert abs(quiet - (0.8 + 1000 / 32767.0)) < 1e-4


# ── decisions ───────────────────────────────────────────────────
async def test_solo_device_handles_immediately_without_waiting() -> None:
    bus = InMemoryBus()
    solo = _arb(bus, "solo", window_ms=5000)  # huge window it must NOT wait on
    await solo.start()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    handled = await solo.should_handle(_wake(amp=5000))
    assert handled is True
    assert loop.time() - t0 < 0.5  # no peers known -> skipped the window
    await solo.stop()


async def test_louder_peer_wins_quieter_suppresses() -> None:
    bus = InMemoryBus()
    a = _arb(bus, "alpha")
    b = _arb(bus, "bravo")
    await _start(a, b)
    ra, rb = await asyncio.gather(
        a.should_handle(_wake(amp=20000)),  # loud / close
        b.should_handle(_wake(amp=2000)),   # quiet / far
    )
    assert ra is True and rb is False  # only the loud unit answers
    await asyncio.gather(a.stop(), b.stop())


async def test_tie_breaks_on_device_id() -> None:
    bus = InMemoryBus()
    a = _arb(bus, "alpha")
    b = _arb(bus, "bravo")
    await _start(a, b)
    # Identical strength -> the lexicographically smaller id ("alpha") wins.
    ra, rb = await asyncio.gather(
        a.should_handle(_wake(amp=8000)),
        b.should_handle(_wake(amp=8000)),
    )
    assert ra is True and rb is False
    await asyncio.gather(a.stop(), b.stop())


async def test_different_communities_do_not_arbitrate() -> None:
    bus = InMemoryBus()
    a = _arb(bus, "alpha", community="house-1")
    b = _arb(bus, "bravo", community="house-2")
    await _start(a, b)
    # Different communities never see each other -> both are solo -> both handle.
    ra, rb = await asyncio.gather(
        a.should_handle(_wake(amp=20000)),
        b.should_handle(_wake(amp=2000)),
    )
    assert ra is True and rb is True
    await asyncio.gather(a.stop(), b.stop())


async def test_three_devices_strongest_wins() -> None:
    bus = InMemoryBus()
    a, b, c = _arb(bus, "a"), _arb(bus, "b"), _arb(bus, "c")
    await _start(a, b, c)
    ra, rb, rc = await asyncio.gather(
        a.should_handle(_wake(amp=5000)),
        b.should_handle(_wake(amp=25000)),  # loudest
        c.should_handle(_wake(amp=9000)),
    )
    assert (ra, rb, rc) == (False, True, False)
    await asyncio.gather(a.stop(), b.stop(), c.stop())


def test_malformed_messages_are_ignored() -> None:
    bus = InMemoryBus()
    a = _arb(bus, "alpha")
    a._on_message(b"not json at all")
    a._on_message(b'{"v": 99, "community": "house", "device_id": "x"}')  # wrong version
    a._on_message(b'{"v": 1, "community": "other", "device_id": "x", "t": "presence"}')
    assert a._known_peers == {}  # nothing registered as a peer
