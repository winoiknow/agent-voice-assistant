"""Select an XvfHost implementation from config."""

from __future__ import annotations

from voiceagent.config import RespeakerConfig
from voiceagent.respeaker.base import XvfHost


def create_xvf_host(cfg: RespeakerConfig) -> XvfHost:
    if cfg.simulate:
        from voiceagent.respeaker.mock import MockXvfHost

        return MockXvfHost()

    from voiceagent.respeaker.xvf_host import RealXvfHost

    return RealXvfHost(binary=cfg.xvf_host_path, transport=cfg.transport)
