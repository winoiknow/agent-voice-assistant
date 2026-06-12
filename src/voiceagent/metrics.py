"""Lightweight in-process runtime metrics + a periodic heartbeat reporter.

The orchestrator records counters (wakes, turns, failures, watchdog fires…),
gauges (current state), and latencies (wake→listening, think→speak). The reporter
emits the snapshot as a structured ``heartbeat`` log event on an interval — so a
soak run is measurable from the logs alone — and optionally writes it to a JSON
file for scraping without opening a port.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from voiceagent.config import ObservabilityConfig
from voiceagent.logging_setup import get_logger

log = get_logger("metrics")


class _Latency:
    __slots__ = ("count", "last", "total", "min", "max")

    def __init__(self) -> None:
        self.count = 0
        self.last: float | None = None
        self.total = 0.0
        self.min: float | None = None
        self.max: float | None = None

    def observe(self, seconds: float) -> None:
        self.count += 1
        self.last = seconds
        self.total += seconds
        self.min = seconds if self.min is None else min(self.min, seconds)
        self.max = seconds if self.max is None else max(self.max, seconds)

    def snapshot(self) -> dict[str, float | int]:
        if not self.count or self.last is None or self.min is None or self.max is None:
            return {"count": 0}
        return {
            "count": self.count,
            "last_s": round(self.last, 3),
            "avg_s": round(self.total / self.count, 3),
            "min_s": round(self.min, 3),
            "max_s": round(self.max, 3),
        }


class Metrics:
    """Cheap, dependency-free counters/gauges/latencies. Single-threaded (asyncio)."""

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, object] = {}
        self._latencies: dict[str, _Latency] = defaultdict(_Latency)

    def incr(self, name: str, n: int = 1) -> None:
        self._counters[name] += n

    def gauge(self, name: str, value: object) -> None:
        self._gauges[name] = value

    def observe(self, name: str, seconds: float) -> None:
        self._latencies[name].observe(seconds)

    def snapshot(self) -> dict[str, object]:
        return {
            "uptime_s": round(time.monotonic() - self._start, 1),
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "latency": {k: v.snapshot() for k, v in self._latencies.items()},
        }


class MetricsReporter:
    """Owns the heartbeat loop: log the snapshot on an interval and (optionally)
    write it to a JSON file."""

    def __init__(self, metrics: Metrics, cfg: ObservabilityConfig) -> None:
        self.metrics = metrics
        self.cfg = cfg
        self._task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self.cfg.heartbeat_interval_s > 0 or bool(self.cfg.metrics_file)

    async def start(self) -> None:
        if self.active and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.emit()  # one final snapshot on the way out

    async def _loop(self) -> None:
        interval = self.cfg.heartbeat_interval_s or 60.0
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            self.emit()

    def emit(self) -> None:
        snap = self.metrics.snapshot()
        if self.cfg.heartbeat_interval_s > 0:
            log.info("heartbeat", **snap)
        if self.cfg.metrics_file:
            self._write_file(snap)

    def _write_file(self, snap: dict[str, object]) -> None:
        path = Path(self.cfg.metrics_file or "").expanduser()
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(snap, indent=2, sort_keys=True))
            os.replace(tmp, path)  # atomic swap so a scraper never reads a partial file
        except OSError as exc:  # pragma: no cover - host-dependent
            log.warning("metrics_file_write_failed", error=str(exc))


__all__ = ["Metrics", "MetricsReporter"]
