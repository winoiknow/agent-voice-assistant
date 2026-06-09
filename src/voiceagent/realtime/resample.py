"""PCM16 resampling for the realtime wire format.

The device captures/plays at 16 kHz (openWakeWord + XVF3800), while the OpenAI
Realtime standard is 24 kHz. We resample at the edge so the client stays
spec-compliant. ``numpy``/``scipy`` are imported lazily so the ``native_16k`` path
(no resampling) needs neither.
"""

from __future__ import annotations

from math import gcd


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample int16-mono PCM from ``src_rate`` to ``dst_rate`` (polyphase)."""
    if src_rate == dst_rate or not data:
        return data
    import numpy as np
    from scipy.signal import resample_poly

    x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    g = gcd(src_rate, dst_rate)
    up = dst_rate // g
    down = src_rate // g
    y = resample_poly(x, up, down)
    y = np.clip(np.round(y), -32768, 32767).astype(np.int16)
    return bytes(y.tobytes())
