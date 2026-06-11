"""Transport for arbitration announcements.

A tiny datagram interface — ``send`` broadcasts bytes to peers, and incoming
datagrams are handed to a callback — so the arbitrator's logic is testable against
an in-memory bus and runs over UDP broadcast on a real LAN.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Callable
from typing import Any, Protocol

from voiceagent.logging_setup import get_logger

log = get_logger("arbitration.transport")

# Called for every inbound datagram with its raw payload.
OnMessage = Callable[[bytes], None]


class ArbitrationTransport(Protocol):
    async def start(self, on_message: OnMessage) -> None: ...
    async def send(self, data: bytes) -> None: ...
    async def stop(self) -> None: ...


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_message: OnMessage) -> None:
        self._on_message = on_message

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self._on_message(data)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover - host-dependent
        log.debug("arbitration_socket_error", error=str(exc))


class UdpBroadcastTransport:
    """UDP broadcast on a fixed port. SO_REUSEADDR/REUSEPORT let several instances
    share the port (e.g. two on one host for local testing); broadcast loopback
    means a unit also hears its own announcement (the arbitrator filters itself)."""

    def __init__(self, port: int, broadcast_address: str = "255.255.255.255") -> None:
        self.port = port
        self.broadcast_address = broadcast_address
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self, on_message: OnMessage) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT is absent on some platforms; ignore if unsupported.
        with contextlib.suppress(AttributeError, OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        sock.bind(("", self.port))
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DatagramProtocol(on_message), sock=sock
        )
        self._transport = transport
        log.info("arbitration_udp_bound", port=self.port)

    async def send(self, data: bytes) -> None:
        if self._transport is not None:
            self._transport.sendto(data, (self.broadcast_address, self.port))

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class InMemoryBus:
    """Connects several :class:`InMemoryTransport`\\ s so a send fans out to all of
    them (including the sender, mirroring UDP broadcast loopback). For tests."""

    def __init__(self) -> None:
        self._members: list[InMemoryTransport] = []

    def register(self, member: InMemoryTransport) -> None:
        self._members.append(member)

    def unregister(self, member: InMemoryTransport) -> None:
        if member in self._members:
            self._members.remove(member)

    def broadcast(self, data: bytes) -> None:
        for m in list(self._members):
            m.deliver(data)


class InMemoryTransport:
    def __init__(self, bus: InMemoryBus) -> None:
        self._bus = bus
        self._on_message: OnMessage | None = None

    async def start(self, on_message: OnMessage) -> None:
        self._on_message = on_message
        self._bus.register(self)

    async def send(self, data: bytes) -> None:
        self._bus.broadcast(data)

    def deliver(self, data: bytes) -> None:
        if self._on_message is not None:
            self._on_message(data)

    async def stop(self) -> None:
        self._bus.unregister(self)


__all__ = [
    "ArbitrationTransport",
    "UdpBroadcastTransport",
    "InMemoryBus",
    "InMemoryTransport",
    "OnMessage",
]
