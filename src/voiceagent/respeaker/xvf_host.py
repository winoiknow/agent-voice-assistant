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
import re
from collections.abc import Sequence

from voiceagent.logging_setup import get_logger
from voiceagent.respeaker.base import RGB, LedEffect, XvfHost

log = get_logger("respeaker.xvf_host")

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


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
        if proc.returncode != 0:
            raise XvfHostError(
                f"xvf_host {command} failed (rc={proc.returncode}): "
                f"{err.decode(errors='replace').strip()}"
            )
        return out.decode(errors="replace")

    async def get_param(self, name: str) -> list[float]:
        out = await self._run(name)
        return [float(m.group()) for m in _FLOAT_RE.finditer(out)]

    async def set_param(self, name: str, values: Sequence[float]) -> None:
        await self._run(name, *values)

    async def save_configuration(self) -> None:
        await self._run("SAVE_CONFIGURATION")

    async def led_effect(self, effect: LedEffect) -> None:
        await self._run("LED_EFFECT", int(effect))

    async def led_color(self, rgb: RGB) -> None:
        r, g, b = rgb
        await self._run("LED_COLOR", r, g, b)

    async def led_brightness(self, value: int) -> None:
        await self._run("LED_BRIGHTNESS", value)

    async def led_speed(self, value: int) -> None:
        await self._run("LED_SPEED", value)
