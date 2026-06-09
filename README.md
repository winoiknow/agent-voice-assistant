# agent-voice-assistant

A light but capable, **headless** wake-word voice assistant for ARM64 single-board
computers (Raspberry Pi, Banana Pi, Orange Pi, …). It is a **client** of an
OpenAI-Realtime-compatible [speech2speech](https://github.com/winoiknow/speech2speech)
server: all STT / LLM / TTS happen off-device. On the SBC it runs wake-word
detection (openWakeWord), a reSpeaker XVF3800 4-mic array (on-board AEC +
beamforming), and can play and announce media via Music Assistant / Home Assistant.

> **Status:** early development. **Phase 2 complete**; **Phase 3 mock-complete** —
> audio I/O and reSpeaker `xvf_host` control are built behind mock + real backends
> and fully testable without hardware. A hardware validation pass is pending.

## Develop without hardware

The audio and reSpeaker subsystems have dependency-free mock backends, so the
bring-up commands run on any machine:

```bash
# point at a dev config, or just use env overrides:
VOICEAGENT_AUDIO__BACKEND=mock VOICEAGENT_RESPEAKER__SIMULATE=true \
  voiceagent audio-test          # capture → playback → cue → duck demo
VOICEAGENT_AUDIO__BACKEND=mock VOICEAGENT_RESPEAKER__SIMULATE=true \
  voiceagent led-test            # cycle the LED-ring cues (or: led-test thinking)
VOICEAGENT_RESPEAKER__SIMULATE=true \
  voiceagent respeaker-tune      # apply DSP tuning and read it back
```

On the SBC, set `audio.backend: pipewire`/`alsa` and `respeaker.simulate: false`,
and install the on-device extras:

```bash
pip install -e ".[audio,wakeword]"
pip install --no-deps openwakeword     # see note below
```

> **openWakeWord install note:** openWakeWord hard-requires `tflite-runtime` on
> Linux, which has no aarch64/Python-3.12 wheel. We use its **ONNX** inference path,
> so it's installed `--no-deps`; the `wakeword` extra provides the deps it actually
> needs (`onnxruntime`, `numpy`, `scipy`, `scikit-learn`, `tqdm`, `requests`).
> Models download automatically on first run.

## Quick start (development)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp config.example.yaml config.yaml      # edit for your network
voiceagent check-config --config config.yaml   # validate + print resolved config
voiceagent run --config config.yaml             # runs until Ctrl-C / SIGTERM
```

## Configuration

A single `config.yaml` drives everything (see `config.example.yaml` for every
documented option). Any value can be overridden by an environment variable using
the `VOICEAGENT_` prefix and `__` between levels:

```bash
VOICEAGENT_REALTIME__HOST=10.0.0.5 VOICEAGENT_LOGGING__LEVEL=DEBUG voiceagent run
```

Precedence: **env > YAML file > defaults**. Secrets (`realtime.api_key`,
`media.home_assistant.token`) use `SecretStr` and are never printed or logged.

The config file path is chosen by `--config PATH`, else `VA_CONFIG`, else
`./config.yaml`, else `/etc/voiceagent/config.yaml`.

## Development

```bash
ruff check .          # lint
mypy                  # type-check (strict)
pytest                # tests
```

## License

Apache-2.0. © 2026 Eric Alborn, Anteon Group.
