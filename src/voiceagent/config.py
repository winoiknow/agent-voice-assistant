"""Configuration model for the voice assistant.

A single ``config.yaml`` drives the whole headless device. Every field is also
overridable by an environment variable using the ``VOICEAGENT_`` prefix and a
``__`` nested delimiter, e.g. ``VOICEAGENT_REALTIME__HOST=10.0.0.5`` or
``VOICEAGENT_LOGGING__LEVEL=DEBUG``.

Precedence (highest first): explicit init kwargs > environment > YAML file >
field defaults. Secrets use ``SecretStr`` so they never appear in logs, ``repr``,
or ``model_dump`` output.

The active YAML path is chosen by :func:`load_config` (CLI ``--config`` flag, then
the ``VA_CONFIG`` env var, then standard locations). It is intentionally *not*
the ``VOICEAGENT_`` prefix so it can never collide with a settings field.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


# Standard locations probed (in order) when no path is given explicitly.
def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


DEFAULT_CONFIG_LOCATIONS: tuple[Path, ...] = (
    Path("config.yaml"),
    _xdg_config_home() / "voiceagent" / "config.yaml",
    Path("/etc/voiceagent/config.yaml"),
)

# Module-level handle read by ``settings_customise_sources``. Set by load_config.
_active_config_path: Path | None = None


class _StrictModel(BaseModel):
    """Base for config sections: reject unknown keys so typos surface loudly."""

    model_config = ConfigDict(extra="forbid")


class DeviceConfig(_StrictModel):
    """Identity of this physical device."""

    name: str = "voice-assistant"
    room: str | None = None


class RealtimeConfig(_StrictModel):
    """Connection to the speech2speech OpenAI-Realtime server."""

    host: str = "127.0.0.1"
    port: int = 8765
    base_url: str | None = None  # overrides host/port for HTTP, e.g. http://h:8765/v1
    ws_base_url: str | None = None  # overrides host/port for WS, e.g. ws://h:8765/v1
    model: str = "local"
    api_key: SecretStr | None = None

    # Audio format on the wire. Default declares the OpenAI standard 24 kHz; the
    # s2s server resamples to/from its internal 16 kHz pipeline. native_16k omits
    # the format field for the zero-resample fast path (valid only against s2s).
    native_16k: bool = False

    # Sent in session.update.
    instructions: str | None = None
    voice: str | None = None
    server_vad: bool = True
    interrupt_response: bool = True

    # Warm-up handshake: on wake, open the connection and send the instructions
    # prompt first (the prompt should end telling the model not to reply yet);
    # the mic is streamed only after the acknowledge earcon, so the wake word
    # itself is never sent to the model. False = stream immediately (legacy).
    warmup_handshake: bool = True

    # Conversation lifecycle.
    follow_up_window_s: float = 8.0
    # Abort a turn if the s2s server sends no progress (audio/transcript/response)
    # for this long while we await it, instead of hanging in THINKING forever —
    # recovers to a fail-safe earcon + IDLE. This must exceed the *whole* silent
    # gap between the user finishing and the first audio: STT + the Hermes agent's
    # LLM/tool loop (tools run in the agent, invisible to us) + TTS. Observed
    # tool-free turns already take ~25-30 s, and tool calls add a round-trip, so
    # the default is generous. 0 disables the watchdog. (A server-side progress/
    # heartbeat event during the gap is the proper fix — see README.)
    turn_watchdog_s: float = 75.0
    # After a conversation closes, ignore wake detection for this long while the
    # mic keeps flowing — lets the XVF3800 AEC re-converge after music resumes so
    # the resume transient doesn't false-trigger the wake word.
    post_close_grace_s: float = 2.5
    # Spoken phrases that end the session (matched as substrings, label-stripped).
    closer_phrases: list[str] = Field(
        default_factory=lambda: [
            "goodbye",
            "good bye",
            "that's all",
            "that is all",
            "that will be all",
            "never mind",
            "go to sleep",
            "stop listening",
        ]
    )

    # Keep one s2s connection open and ready during IDLE so a wake skips the
    # connect latency (~5 s cold). The connection is recycled on close and
    # re-opened in the background, so each wake-session still gets a fresh
    # conversation. Falls back to an inline connect when no warm connection is
    # ready. Single-device only — the s2s server allows one concurrent session.
    warm_connection: bool = False
    # Recycle an idle warm connection older than this so it's replaced before the
    # server/proxy drops a long-idle socket (which would burn the next wake's turn).
    # Keep it under the upstream idle timeout (commonly 60-120 s).
    warm_refresh_s: float = 45.0
    # Wait this long after a conversation closes before re-warming, so we don't
    # reconnect 1-2 s after disconnect — the rapid session churn the single-session
    # s2s server stalls on. Still well under a typical gap between wakes.
    warm_rewarm_delay_s: float = 3.0

    # Resilience.
    connect_timeout_s: float = 10.0
    reconnect_initial_backoff_s: float = 0.5
    reconnect_max_backoff_s: float = 30.0


class AudioConfig(_StrictModel):
    """Local capture/playback and ducking.

    ``backend: mock`` selects a dependency-free simulated backend for development
    and CI on machines without the audio hardware. ``pipewire``/``alsa`` use the
    real ``sounddevice`` backend (install the ``audio`` extra).
    """

    backend: Literal["pipewire", "alsa", "mock"] = "pipewire"
    # Mic capture is fixed at 16 kHz: required by openWakeWord and what the
    # XVF3800 presents. The realtime client upsamples to 24 kHz on send.
    capture_rate: int = 16000
    capture_frame_ms: int = Field(default=32, gt=0)  # 32 ms = 512 samples @ 16 kHz
    capture_device: str | None = None
    # The XVF3800 presents 2 channels: CH0 = processed (AEC + beamformed), CH1 =
    # raw/reference. When capturing via a source that would downmix them (e.g.
    # PulseAudio), open this many channels and keep only capture_pick_channel so
    # the raw echo in CH1 isn't blended back into the clean CH0. Default 1 = the
    # device already gives clean mono (e.g. direct ALSA hw on CH0).
    capture_channels: int = Field(default=1, ge=1)
    capture_pick_channel: int = Field(default=0, ge=0)
    playback_device: str | None = None
    playback_rate: int = 24000
    # Music volume (0..1) during a voice turn, and the fade applied when ducking.
    duck_level: float = Field(default=0.2, ge=0.0, le=1.0)
    duck_fade_ms: int = Field(default=80, ge=0)
    # Optional PipeWire/Pulse target (node name/id) whose volume is ducked. The
    # concrete music stream is wired in Phase 7; unset = duck is a logged no-op.
    music_target: str | None = None


class RespeakerConfig(_StrictModel):
    """reSpeaker XVF3800 control via the xvf_host binary."""

    enabled: bool = True
    # simulate selects an in-memory MockXvfHost for dev/CI without the hardware.
    simulate: bool = False
    xvf_host_path: str = "xvf_host"
    transport: Literal["usb", "i2c"] = "usb"
    # Raw xvf_host parameter name -> argument values, applied at startup.
    # e.g. {"AUDIO_MGR_MIC_GAIN": [10], "PP_AGCGAIN": [1]}
    tuning: dict[str, list[float]] = Field(default_factory=dict)
    save_to_flash: bool = False


class LedConfig(_StrictModel):
    """LED-ring cue colors (RGB 0..255). See ARCHITECTURE.md §3.8.

    Firmware effects are a fixed set (off/breath/rainbow/single/doa); chase/flash
    patterns are an open item, so the feedback controller maps states to the
    nearest available primitive using these colors.
    """

    enabled: bool = True
    brightness: int = Field(default=40, ge=0, le=255)
    listen_color: tuple[int, int, int] = (0, 255, 0)
    think_color: tuple[int, int, int] = (0, 0, 255)
    speak_color: tuple[int, int, int] = (0, 0, 255)


class WakewordConfig(_StrictModel):
    """openWakeWord detection and the wake confirmation sound.

    ``engine: mock`` uses a dependency-free RMS-triggered detector for dev/CI;
    ``openwakeword`` is the real engine (install the ``wakeword`` extra).
    """

    enabled: bool = True
    engine: Literal["openwakeword", "mock"] = "openwakeword"
    models: list[str] = Field(default_factory=lambda: ["alexa"])
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    vad_threshold: float = Field(default=0.0, ge=0.0, le=1.0)  # 0 disables the VAD gate
    cooldown_s: float = Field(default=2.0, ge=0.0)
    preroll_s: float = Field(default=0.5, ge=0.0)
    # User-supplied wake confirmation .wav, played once on detection.
    wake_sound: str | None = None
    # mock engine: fire when a frame's RMS exceeds this (int16 scale, 0..32767).
    mock_trigger_rms: float = Field(default=1500.0, ge=0.0)


class SendspinConfig(_StrictModel):
    """Managed sendspin player sidecar (device is the player).

    Run with no ``server_url`` so the daemon advertises via mDNS
    (``_sendspin._tcp.local.``) and Music Assistant auto-discovers it; MA then
    mirrors it into Home Assistant as a media_player entity.
    """

    enabled: bool = False
    binary: str = "sendspin"
    name: str | None = None  # defaults to device.name when unset
    server_url: str | None = None  # ws://...; None => mDNS auto-discovery (preferred)
    audio_device: str | None = None  # index / name prefix / ALSA / 'pulse'|'pipewire'
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # False = software (per-stream) volume so ducking only attenuates the music,
    # not the shared output device (which would also duck the assistant's TTS).
    # True would control the system/hardware volume of the whole sink.
    hardware_volume: bool = False
    extra_args: list[str] = Field(default_factory=list)


class HomeAssistantConfig(_StrictModel):
    """Home Assistant / Music Assistant control for pause/resume/announce."""

    enabled: bool = False
    base_url: str | None = None  # http://homeassistant.local:8123
    token: SecretStr | None = None
    media_player_entity: str | None = None  # entity for this device's player


class MediaConfig(_StrictModel):
    sendspin: SendspinConfig = Field(default_factory=SendspinConfig)
    home_assistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig)
    # What to do to the HA player on a voice turn:
    #   duck  = lower its volume, keep the stream flowing (instant resume, keeps
    #           the XVF3800 AEC converged) — recommended.
    #   pause = true pause (frees the device, but resume re-buffers and the AEC
    #           re-converges, needing realtime.post_close_grace_s).
    on_turn: Literal["duck", "pause"] = "duck"
    # Volume (0..1) the player is ducked to during a turn (on_turn: duck).
    duck_level: float = Field(default=0.25, ge=0.0, le=1.0)


class FeedbackConfig(_StrictModel):
    error_sound: str | None = None
    led: LedConfig = Field(default_factory=LedConfig)


class LoggingConfig(_StrictModel):
    level: str = "INFO"
    format: Literal["console", "json"] = "console"

    @field_validator("level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        up = v.upper()
        if up not in valid:
            raise ValueError(f"invalid log level {v!r}; choose one of {sorted(valid)}")
        return up


class Settings(BaseSettings):
    """Top-level configuration aggregating every subsystem."""

    model_config = SettingsConfigDict(
        env_prefix="VOICEAGENT_",
        env_nested_delimiter="__",
        extra="forbid",
        case_sensitive=False,
    )

    device: DeviceConfig = Field(default_factory=DeviceConfig)
    realtime: RealtimeConfig = Field(default_factory=RealtimeConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    respeaker: RespeakerConfig = Field(default_factory=RespeakerConfig)
    wakeword: WakewordConfig = Field(default_factory=WakewordConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # init > env > yaml > defaults.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if _active_config_path is not None:
            sources.append(
                YamlConfigSettingsSource(settings_cls, yaml_file=_active_config_path)
            )
        return tuple(sources)


def resolve_config_path(path: str | os.PathLike[str] | None = None) -> Path | None:
    """Resolve which config file to use: explicit arg, then VA_CONFIG, then defaults.

    Returns the first existing path, or None if nothing is found (defaults only).
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    elif env_path := os.environ.get("VA_CONFIG"):
        candidates.append(Path(env_path))
    else:
        candidates.extend(DEFAULT_CONFIG_LOCATIONS)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # An explicitly requested path that does not exist is an error the caller
    # should see, rather than silently falling back to defaults.
    if path is not None or os.environ.get("VA_CONFIG"):
        raise FileNotFoundError(f"config file not found: {candidates[0]}")
    return None


def load_config(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings, layering YAML (if found) under environment overrides."""
    global _active_config_path
    _active_config_path = resolve_config_path(path)
    try:
        return Settings()
    finally:
        _active_config_path = None
