"""Real XvfHost backend: drives the prebuilt ``xvf_host`` CLI as a subprocess.

Command form (per the XMOS host-application guide):
    xvf_host [-u <transport>] COMMAND [args...]
USB is the default transport; pass ``-u i2c`` for I2C. Getters print their values
to stdout; setters take the value args after the command name.

LED argument formats (LED_COLOR in particular) are validated on hardware in the
Phase 3 hardware pass; the encoding here (``LED_COLOR r g b``) is the documented
shape and is centralized so it can be adjusted in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence

from voiceagent.logging_setup import get_logger
from voiceagent.respeaker.base import RGB, LedEffect, XvfHost

log = get_logger("respeaker.xvf_host")

# Substrings xvf_host prints when it cannot reach the device (it still exits 0).
_FAILURE_MARKERS = ("Failed to open device", "Could not connect", "No device found")


class XvfHostError(RuntimeError):
    pass


class RealXvfHost(XvfHost):
    def __init__(self, binary: str = "xvf_host", transport: str = "usb") -> None:
        self.binary = binary
        self.transport = transport

    def _argv(self, command: str, *args: object) -> list[str]:
        argv = [self.binary]
        if self.transport and self.transport != "usb":
            argv += ["-u", self.transport]
        argv.append(command)
        argv += [str(a) for a in args]
        return argv

    async def _run(self, command: str, *args: object) -> str:
        argv = self._argv(command, *args)
        log.debug("xvf_host_exec", argv=argv)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise XvfHostError(
                f"xvf_host binary not found: {self.binary!r}. The installer vendors "
                f"the prebuilt binary; set respeaker.simulate: true for development."
            ) from exc
        out, err = await proc.communicate()
        out_s = out.decode(errors="replace")
        err_s = err.decode(errors="replace")
        # xvf_host exits 0 even when it cannot open the USB device, so detect
        # failures by their message markers as well as a non-zero return code.
        if proc.returncode != 0 or any(m in (out_s + err_s) for m in _FAILURE_MARKERS):
            detail = (err_s or out_s).strip().splitlines()
            raise XvfHostError(
                f"xvf_host {command} failed (rc={proc.returncode}): "
                f"{detail[0] if detail else 'unknown error'}"
            )
        return out_s

    async def get_param(self, name: str) -> list[float]:
        # Output carries a device-init banner plus an echoed "<NAME> <values...>"
        # line. Parse the line that starts with the command name, regardless of
        # which stream the banner used.
        out = await self._run(name)
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == name:
                values: list[float] = []
                for tok in parts[1:]:
                    with contextlib.suppress(ValueError):
                        values.append(float(tok))
                return values
        return []

    async def set_param(self, name: str, values: Sequence[float]) -> None:
        await self._run(name, *values)

    async def save_configuration(self) -> None:
        await self._run("SAVE_CONFIGURATION")

    async def led_effect(self, effect: LedEffect) -> None:
        await self._run("LED_EFFECT", int(effect))

    async def led_color(self, rgb: RGB) -> None:
        # Firmware LED_COLOR is a single uint32, 0xRRGGBB (verified on hardware,
        # firmware 2.0.6). Not three r/g/b args.
        r, g, b = rgb
        await self._run("LED_COLOR", (r << 16) | (g << 8) | b)

    async def led_brightness(self, value: int) -> None:
        await self._run("LED_BRIGHTNESS", value)

    async def led_speed(self, value: int) -> None:
        await self._run("LED_SPEED", value)
