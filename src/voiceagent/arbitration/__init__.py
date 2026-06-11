"""Multi-device wake arbitration (the strongest unit answers a shared wake word)."""

from __future__ import annotations

from voiceagent.arbitration.arbitrator import WakeArbitrator, wake_strength
from voiceagent.arbitration.transport import (
    ArbitrationTransport,
    InMemoryBus,
    InMemoryTransport,
    UdpBroadcastTransport,
)

__all__ = [
    "WakeArbitrator",
    "wake_strength",
    "ArbitrationTransport",
    "UdpBroadcastTransport",
    "InMemoryBus",
    "InMemoryTransport",
]
