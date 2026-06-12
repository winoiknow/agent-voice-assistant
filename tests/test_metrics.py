from __future__ import annotations

import asyncio
import json
from pathlib import Path

from voiceagent.config import LoggingConfig, ObservabilityConfig
from voiceagent.logging_setup import _REDACTED, configure_logging, get_logger
from voiceagent.metrics import Metrics, MetricsReporter


# ── Metrics ─────────────────────────────────────────────────────
def test_metrics_counters_gauges_and_latency() -> None:
    m = Metrics()
    m.incr("wakes_detected")
    m.incr("wakes_detected")
    m.incr("turns", 3)
    m.gauge("state", "thinking")
    for v in (1.0, 3.0, 2.0):
        m.observe("think_to_speak_s", v)

    snap = m.snapshot()
    assert snap["counters"] == {"wakes_detected": 2, "turns": 3}
    assert snap["gauges"] == {"state": "thinking"}
    lat = snap["latency"]["think_to_speak_s"]
    assert lat == {"count": 3, "last_s": 2.0, "avg_s": 2.0, "min_s": 1.0, "max_s": 3.0}
    assert isinstance(snap["uptime_s"], float)


def test_metrics_empty_latency_snapshot() -> None:
    m = Metrics()
    m.observe("x", 0.0)  # touch it but...
    assert m.snapshot()["latency"]["x"]["count"] == 1
    assert m.snapshot()["latency"].get("never") is None


# ── MetricsReporter ─────────────────────────────────────────────
def test_reporter_inactive_when_unconfigured() -> None:
    r = MetricsReporter(Metrics(), ObservabilityConfig(heartbeat_interval_s=0, metrics_file=None))
    assert r.active is False


async def test_reporter_writes_snapshot_file_atomically(tmp_path: Path) -> None:
    m = Metrics()
    m.incr("wakes_detected", 2)
    m.observe("wake_to_listen_s", 0.6)
    out = tmp_path / "metrics.json"
    r = MetricsReporter(m, ObservabilityConfig(heartbeat_interval_s=0, metrics_file=str(out)))
    await r.start()
    await r.stop()  # final emit writes the file
    data = json.loads(out.read_text())
    assert data["counters"]["wakes_detected"] == 2
    assert data["latency"]["wake_to_listen_s"]["count"] == 1
    assert not out.with_suffix(".json.tmp").exists()  # temp swapped away


async def test_reporter_heartbeat_fires_on_interval(tmp_path: Path) -> None:
    out = tmp_path / "m.json"
    r = MetricsReporter(Metrics(), ObservabilityConfig(heartbeat_interval_s=0.05,
                                                       metrics_file=str(out)))
    await r.start()
    await asyncio.sleep(0.16)  # a few heartbeats
    assert out.exists()  # written without waiting for stop()
    await r.stop()


# ── structured log file ─────────────────────────────────────────
def test_log_file_is_written_and_redacts(tmp_path: Path) -> None:
    logfile = tmp_path / "voiceagent.log"
    configure_logging(LoggingConfig(level="INFO", format="console",
                                    file=str(logfile), file_format="json"))
    try:
        get_logger("test").info("turn_done", latency_s=0.6, api_key="supersecret")
        line = logfile.read_text().strip().splitlines()[-1]
        record = json.loads(line)  # file is JSON even though console is "console"
        assert record["event"] == "turn_done"
        assert record["latency_s"] == 0.6
        assert record["api_key"] == _REDACTED  # secret never hits the file
    finally:
        configure_logging(LoggingConfig())  # reset global logging for other tests
