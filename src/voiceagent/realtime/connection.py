"""Keep a warm s2s realtime connection ready during idle.

The speech2speech server allows a **single concurrent session** and creates a
fresh per-connection chat on connect. So we keep exactly **one** connection open
and ready while idle; a wake :meth:`acquire`\\ s it (skipping the ~5 s connect),
and on :meth:`release` we recycle it — every wake-session still gets a fresh
conversation, but the connect is paid off the wake's critical path.

A single supervisor loop maintains the invariant "one fresh warm connection while
idle":
- it (re)establishes the connection with backoff;
- it **keeps the connection alive** by sending a WebSocket ping every
  ``warm_ping_interval_s`` and waiting for the pong (``warm_ping_timeout_s``); a
  failed ping means a dead socket, so it drops and reopens. One connection then
  lives for hours with no rebuild churn. (``warm_refresh_s`` can still force a
  periodic recycle, but it is off by default.)
- after a turn it waits ``warm_rewarm_delay_s`` before re-warming, so we don't
  reconnect 1-2 s after disconnect (rapid session churn the server stalls on).

``acquire`` returns ``None`` when no warm connection is ready in time, so the
caller falls back to an inline connect (never a regression).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from voiceagent.config import RealtimeConfig
from voiceagent.logging_setup import get_logger
from voiceagent.realtime.session import RealtimeConnection

log = get_logger("realtime.warm")

# A pure connector: open and enter a realtime connection, returning the entered
# context manager (for later close) and the live connection. Injectable for tests.
Opener = Callable[[], Awaitable[tuple[Any, RealtimeConnection]]]


class WarmConnectionManager:
    """Owns one always-warm s2s connection, recycled per wake-session."""

    def __init__(self, cfg: RealtimeConfig, *, opener: Opener | None = None) -> None:
        self.cfg = cfg
        self._opener = opener or self._default_open
        self._cm: Any = None  # entered connect() context manager for the live conn
        self._conn: RealtimeConnection | None = None
        self._ready = asyncio.Event()
        self._ready_at = 0.0  # monotonic time the current connection became ready
        self._last_ping_at = 0.0  # monotonic time of the last keepalive ping
        self._rewarm_not_before = 0.0  # delay re-warm until this monotonic time
        self._in_use = False
        self._wake = asyncio.Event()  # poke the supervisor to re-evaluate promptly
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    # ── lifecycle ────────────────────────────────────────────────
    async def start(self) -> None:
        self._closing = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._close_cm()

    # ── per-turn ─────────────────────────────────────────────────
    async def acquire(self, timeout: float) -> RealtimeConnection | None:
        """Return a ready warm connection, or None if none becomes ready in time."""
        if self._closing:
            return None
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError:
            log.info("warm_connection_not_ready", waited_s=timeout)
            return None
        conn = self._conn
        if conn is None:  # pragma: no cover - ready set but conn gone (raced stop)
            return None
        # Mark in use so the supervisor leaves it alone; _cm is retained so
        # release() can close it. No re-warm now (one concurrent session only).
        self._in_use = True
        self._ready.clear()
        log.info("warm_connection_acquired")
        return conn

    async def release(self) -> None:
        """Close the used connection; the supervisor re-warms after a short delay."""
        await self._close_cm()
        self._rewarm_not_before = time.monotonic() + self.cfg.warm_rewarm_delay_s
        self._in_use = False
        self._wake.set()  # let the supervisor start the re-warm clock immediately

    # ── supervisor ───────────────────────────────────────────────
    async def _supervise(self) -> None:
        backoff = self.cfg.reconnect_initial_backoff_s
        while not self._closing:
            if self._in_use:
                await self._idle_wait(1.0)
                continue
            if self._conn is None:
                wait = self._rewarm_not_before - time.monotonic()
                if wait > 0:  # let the server settle after a close before re-warming
                    await asyncio.sleep(min(wait, 1.0))
                    continue
                try:
                    self._cm, self._conn = await self._opener()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("warm_connect_failed", error=str(exc),
                                retry_in_s=round(backoff, 1))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.cfg.reconnect_max_backoff_s)
                    continue
                backoff = self.cfg.reconnect_initial_backoff_s
                now = time.monotonic()
                self._ready_at = now
                self._last_ping_at = now
                self._ready.set()
                log.info("warm_connection_ready")
            elif self.cfg.warm_refresh_s > 0 and (
                time.monotonic() - self._ready_at >= self.cfg.warm_refresh_s
            ):
                # Optional periodic recycle (off by default): replace the connection
                # on a fixed age regardless of health.
                log.info("warm_connection_refresh",
                         age_s=round(time.monotonic() - self._ready_at))
                await self._close_cm()
            elif self.cfg.warm_ping_interval_s > 0 and (
                time.monotonic() - self._last_ping_at >= self.cfg.warm_ping_interval_s
            ):
                # Keepalive: ping the idle connection to keep it alive between the
                # server's pings. A failed ping/pong means the socket is dead, so we
                # drop it and reopen on the next loop.
                if await self._keepalive_ping():
                    self._last_ping_at = time.monotonic()
                else:
                    await self._close_cm()
            else:
                # Sleep until the next ping (or refresh) is due, bounded so acquire/
                # release stay responsive.
                await self._idle_wait(min(1.0, max(0.05, self._next_due())))

    def _next_due(self) -> float:
        """Seconds until the next supervisor action (keepalive ping or recycle)."""
        now = time.monotonic()
        due = float("inf")
        if self.cfg.warm_ping_interval_s > 0:
            due = min(due, self._last_ping_at + self.cfg.warm_ping_interval_s - now)
        if self.cfg.warm_refresh_s > 0:
            due = min(due, self._ready_at + self.cfg.warm_refresh_s - now)
        return due if due != float("inf") else 1.0

    async def _keepalive_ping(self) -> bool:
        """Send a WebSocket ping on the idle warm connection and wait for the pong.

        Returns True if the connection answered (still alive), False if the ping
        failed or the pong didn't arrive in ``warm_ping_timeout_s`` (dead socket —
        the caller drops and reopens it). If the backend doesn't expose a ping we
        keep the connection (nothing we can do), so this never causes a needless
        recycle.
        """
        ws = getattr(self._conn, "_connection", None)
        ping = getattr(ws, "ping", None)
        if ping is None:
            return True
        try:
            pong_waiter = await ping()
            await asyncio.wait_for(pong_waiter, timeout=self.cfg.warm_ping_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("warm_ping_failed", error=str(exc))
            return False
        log.debug("warm_ping_ok")
        return True

    async def _idle_wait(self, timeout: float) -> None:
        """Sleep up to ``timeout``, but wake early if poked (acquire/release)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout)
        self._wake.clear()

    # ── internals ────────────────────────────────────────────────
    async def _close_cm(self) -> None:
        cm, self._cm = self._cm, None
        self._conn = None
        self._ready.clear()
        if cm is not None:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    async def _default_open(self) -> tuple[Any, RealtimeConnection]:
        from openai import AsyncOpenAI

        cfg = self.cfg
        base_url = cfg.base_url or f"http://{cfg.host}:{cfg.port}/v1"
        ws_base_url = cfg.ws_base_url or f"ws://{cfg.host}:{cfg.port}/v1"
        client = AsyncOpenAI(
            api_key=cfg.api_key.get_secret_value() if cfg.api_key else "none",
            base_url=base_url,
            websocket_base_url=ws_base_url,
        )
        cm = client.realtime.connect(model=cfg.model)
        conn = await cm.__aenter__()
        return cm, conn


__all__ = ["WarmConnectionManager"]
