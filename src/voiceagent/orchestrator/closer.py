"""Detect a spoken phrase that should end the conversation."""

from __future__ import annotations

import re
from collections.abc import Sequence

# s2s diarization prefixes transcripts with a speaker label like "[Eric]".
_LABEL = re.compile(r"^\s*\[[^\]]*\]\s*")


def strip_label(text: str) -> str:
    return _LABEL.sub("", text)


def is_closer(text: str, phrases: Sequence[str]) -> bool:
    cleaned = strip_label(text).lower().strip().strip(".!?,")
    return any(phrase in cleaned for phrase in phrases)
