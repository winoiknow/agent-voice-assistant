from __future__ import annotations

import asyncio
from typing import Any

import pytest

from voiceagent.audio.mock import MockAudioIO
from voiceagent.audio.types import AudioFormat
from voiceagent.config import RealtimeConfig
from voiceagent.realtime import RealtimeSession
from voiceagent.realtime.connection import WarmConnectionManager


class FakeCM:
    """Stand-in for an entered openai realtime connect() context manager."""

    def __init__(self) -> None:
        self.closed = False

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


class FakeConn:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, event: dict[str, Any]) -> None:
        self.sent.append(event)

    async def recv(self) -> Any:
        await asyncio.Event().wait()  # block; turns drive completion in these tests


def _cfg(**over: Any) -> RealtimeConfig:
    base: dict[str, Any] = {
        "reconnect_initial_backoff_s": 0.01,
        "reconnect_max_backoff_s": 0.05,
        "warm_rewarm_delay_s": 0.0,
        "warm_refresh_s": 100.0,
    }
    base.update(over)
    return RealtimeConfig(**base)


async def test_warm_manager_warms_acquires_and_recycles() -> None:
    opened: list[FakeCM] = []

    async def opener() -> tuple[FakeCM, FakeConn]:
        cm = FakeCM()
        opened.append(cm)
        return cm, FakeConn()

    mgr = WarmConnectionManager(_cfg(), opener=opener)
    await mgr.start()

    conn = await mgr.acquire(timeout=1.0)
    assert conn is not None  # warmed during idle, acquired instantly
    assert len(opened) == 1

    await mgr.release()  # closes the used connection + re-warms in the background
    assert opened[0].closed is True
    conn2 = await mgr.acquire(timeout=1.0)
    assert conn2 is not None  # a fresh one was warmed
    assert len(opened) == 2
    assert conn2 is not conn
    await mgr.stop()
    assert opened[1].closed is True


async def test_warm_manager_refreshes_idle_connection() -> None:
    # A connection idle longer than warm_refresh_s is recycled before an upstream
    # idle timeout can hand us a dead socket.
    opened: list[FakeCM] = []

    async def opener() -> tuple[FakeCM, FakeConn]:
        cm = FakeCM()
        opened.append(cm)
        return cm, FakeConn()

    mgr = WarmConnectionManager(_cfg(warm_refresh_s=0.1), opener=opener)
    await mgr.start()
    await asyncio.sleep(0.05)
    assert len(opened) == 1  # warmed
    await asyncio.sleep(0.4)  # exceed warm_refresh_s -> supervisor recycles
    assert len(opened) >= 2
    assert opened[0].closed is True  # the aged connection was closed
    await mgr.stop()


async def test_warm_manager_delays_rewarm_after_release() -> None:
    opened: list[FakeCM] = []

    async def opener() -> tuple[FakeCM, FakeConn]:
        cm = FakeCM()
        opened.append(cm)
        return cm, FakeConn()

    mgr = WarmConnectionManager(_cfg(warm_rewarm_delay_s=0.4), opener=opener)
    await mgr.start()
    assert await mgr.acquire(timeout=1.0) is not None
    await mgr.release()
    # Re-warm is held off for warm_rewarm_delay_s, so it's not ready immediately.
    assert await mgr.acquire(timeout=0.1) is None
    # ...but it does come back after the delay.
    assert await mgr.acquire(timeout=1.0) is not None
    await mgr.stop()


async def test_warm_manager_acquire_times_out_when_not_ready() -> None:
    started = asyncio.Event()

    async def slow_opener() -> tuple[FakeCM, FakeConn]:
        started.set()
        await asyncio.sleep(5.0)  # never ready within the acquire window
        return FakeCM(), FakeConn()

    mgr = WarmConnectionManager(_cfg(), opener=slow_opener)
    await mgr.start()
    await started.wait()
    # Not ready -> acquire returns None so the caller falls back to inline connect.
    assert await mgr.acquire(timeout=0.05) is None
    await mgr.stop()


async def test_warm_manager_retries_on_open_failure() -> None:
    calls = 0

    async def flaky_opener() -> tuple[FakeCM, FakeConn]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("connection refused")
        return FakeCM(), FakeConn()

    mgr = WarmConnectionManager(_cfg(), opener=flaky_opener)
    await mgr.start()
    conn = await mgr.acquire(timeout=1.0)  # backoff then succeed on the 2nd try
    assert conn is not None
    assert calls == 2
    await mgr.stop()


async def test_session_uses_warm_connection_and_releases() -> None:
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512, max_frames=2)
    conn = FakeConn()
    released = asyncio.Event()

    async def acquire() -> FakeConn:
        return conn

    async def release() -> None:
        released.set()

    sess = RealtimeSession(
        RealtimeConfig(native_16k=True, warmup_handshake=False),
        io, capture_rate=16000, playback_rate=16000,
        acquire=acquire, release=release,
    )
    async with io:
        await asyncio.wait_for(sess.run(duration_s=2.0), timeout=3.0)
    # The injected warm connection was used (session.update sent over it) and released.
    assert conn.sent and conn.sent[0]["type"] == "session.update"
    assert released.is_set()


async def test_session_falls_back_when_no_warm_connection() -> None:
    # acquire() returns None -> run() must fall through to the inline connect path,
    # which (with no openai installed / configured) raises rather than silently no-op.
    io = MockAudioIO(AudioFormat(16000), AudioFormat(16000), frame_samples=512, max_frames=1)
    release_called = False

    async def acquire() -> None:
        return None

    async def release() -> None:
        nonlocal release_called
        release_called = True

    sess = RealtimeSession(
        RealtimeConfig(host="127.0.0.1", port=1), io,
        capture_rate=16000, playback_rate=16000, acquire=acquire, release=release,
    )
    async with io:
        with pytest.raises(Exception):  # noqa: B017 - any inline-connect failure proves fallback
            await asyncio.wait_for(sess.run(duration_s=1.0), timeout=5.0)
    assert release_called is False  # release only runs when a warm conn was used
