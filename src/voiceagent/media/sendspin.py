"""Supervise the sendspin player daemon as a sidecar.

With no ``--url`` the daemon advertises via mDNS and Music Assistant auto-discovers
it (which then mirrors it into Home Assistant as a media_player entity). This class
just owns the subprocess lifecycle (start/stop, log forwarding); pause/resume and
ducking are handled via Home Assistant + local PipeWire in later Phase-7 steps.
"""

from __future__ import annotations

import asyncio
import signal

from voiceagent.config import SendspinConfig
from voiceagent.logging_setup import get_logger

log = get_logger("media.sendspin")


class SendspinDaemon:
    def __init__(self, cfg: SendspinConfig, *, default_name: str = "voice-assistant") -> None:
        self.cfg = cfg
        self.name = cfg.name or default_name
        self._proc: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task[None] | None = None

    def argv(self) -> list[str]:
        argv = [self.cfg.binary, "daemon", "--name", self.name, "--log-level", self.cfg.log_level]
        argv += ["--hardware-volume", "true" if self.cfg.hardware_volume else "false"]
        if self.cfg.server_url:
            argv += ["--url", self.cfg.server_url]
        if self.cfg.audio_device:
            argv += ["--audio-device", self.cfg.audio_device]
        argv += self.cfg.extra_args
        return argv

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self.is_running():
            return
        argv = self.argv()
        log.info("sendspin_starting", argv=argv)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"sendspin binary not found: {self.cfg.binary!r}. Install it "
                f"(pip install sendspin) or set media.sendspin.binary / disable it."
            ) from exc
        self._log_task = asyncio.create_task(self._forward_logs())

    async def _forward_logs(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        async for raw in self._proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.debug("sendspin", line=line)

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if self._log_task is not None:
            self._log_task.cancel()
        self._proc = None
        self._log_task = None
        log.info("sendspin_stopped")
