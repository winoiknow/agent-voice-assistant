"""Supervise the sendspin player daemon as a sidecar.

With no ``--url`` the daemon advertises via mDNS and Music Assistant auto-discovers
it (which then mirrors it into Home Assistant as a media_player entity). This class
just owns the subprocess lifecycle (start/stop, log forwarding); pause/resume and
ducking are handled via Home Assistant + local PipeWire in later Phase-7 steps.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
import sys
from pathlib import Path

from voiceagent.config import SendspinConfig
from voiceagent.logging_setup import get_logger

log = get_logger("media.sendspin")


_DEFAULT_BINARY = {"cli": "sendspin", "cpp": "sendspin-cpp"}

# sendspin-cpp's basic_client uses its own log-level vocabulary (-l), unlike the
# cli's uppercase Python levels. Map ours onto it.
_CPP_LOG_LEVEL = {
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "warn",
    "ERROR": "error",
    "CRITICAL": "error",
}


def _resolve_binary(binary: str) -> str:
    """Resolve a bare binary name to the venv's copy if it isn't on PATH."""
    if "/" in binary or shutil.which(binary):
        return binary
    candidate = Path(sys.executable).parent / binary
    return str(candidate) if candidate.exists() else binary


class SendspinDaemon:
    def __init__(self, cfg: SendspinConfig, *, default_name: str = "voice-assistant") -> None:
        self.cfg = cfg
        self.name = cfg.name or default_name
        self._proc: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task[None] | None = None

    def argv(self) -> list[str]:
        binary = _resolve_binary(self.cfg.binary or _DEFAULT_BINARY[self.cfg.provider])
        if self.cfg.provider == "cpp":
            return self._argv_cpp(binary)
        return self._argv_cli(binary)

    def _argv_cli(self, binary: str) -> list[str]:
        argv = [binary, "daemon", "--name", self.name, "--log-level", self.cfg.log_level]
        argv += ["--hardware-volume", "true" if self.cfg.hardware_volume else "false"]
        if self.cfg.server_url:
            argv += ["--url", self.cfg.server_url]
        if self.cfg.audio_device:
            argv += ["--audio-device", self.cfg.audio_device]
        argv += self.cfg.extra_args
        return argv

    def _argv_cpp(self, binary: str) -> list[str]:
        # basic_client: positional name, -p port, -u url, -l level. No
        # audio-device/hardware-volume flags (PortAudio default sink, music-only
        # software volume wired through the protocol's volume role).
        argv = [binary, self.name, "-l", _CPP_LOG_LEVEL[self.cfg.log_level]]
        if self.cfg.port is not None:  # v0.6.1 basic_client has no -p (fixed 8928)
            argv += ["-p", str(self.cfg.port)]
        if self.cfg.server_url:
            argv += ["-u", self.cfg.server_url]
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
            hint = (
                "build sendspin-cpp (scripts/build-sendspin-cpp.sh) and set "
                "media.sendspin.binary to the basic_client path"
                if self.cfg.provider == "cpp"
                else "install it (pip install sendspin)"
            )
            raise RuntimeError(
                f"sendspin binary not found: {argv[0]!r} (provider="
                f"{self.cfg.provider!r}). {hint}, or set media.sendspin.binary / "
                f"disable it."
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
