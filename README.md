# agent-voice-assistant

A light but capable, **headless** wake-word voice assistant for ARM64 single-board
computers (Raspberry Pi, Banana Pi, Orange Pi, …). It is a **client** of an
OpenAI-Realtime-compatible [speech2speech](https://github.com/winoiknow/speech2speech)
server: all STT / LLM / TTS happen off-device. On the SBC it runs wake-word
detection (openWakeWord), a reSpeaker XVF3800 4-mic array (on-board AEC +
beamforming), and can play and announce media via Music Assistant / Home Assistant.

## Install (on the SBC)

One line — installs system deps, the package, the reSpeaker `xvf_host` binary, a
udev rule, runs a config wizard, and registers a systemd user service:

```bash
curl -fsSL https://raw.githubusercontent.com/winoiknow/agent-voice-assistant/main/install.sh | bash
```

The wizard asks for your speech2speech URL/API key, wake word (defaults to the
bundled **`Belvedere`** model), and optional Music Assistant / Home Assistant
control. Secrets go to `~/.config/voiceagent/secrets.env` (mode 600), never into
`config.yaml`. Manage it with:

```bash
systemctl --user status voiceagent
journalctl --user -u voiceagent -f
voiceagent init --force          # re-run the wizard
```

> ### ⏱️ Time sync is essential
> sendspin's multi-room playback is **clock-driven**: the SBC and your **Music
> Assistant** host **must share the same time source**, or playback sync drifts and
> start/resume buffering gets worse. Install **chrony** (the installer does) and
> point both machines at the **same NTP server**.

> ### Audio routing note
> If the device runs a desktop/PulseAudio session, the XVF3800 is the pulse default
> source/sink — capture via `default` and keep **CH0 only**
> (`audio.capture_channels: 2`, `capture_pick_channel: 0`); the wizard sets this.
> CH0 is the hardware-AEC'd/beamformed channel; capturing the raw stereo downmix
> reintroduces speaker echo (false wakes, barge-ins, STT hallucinations).

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

### Custom wake word

`wakeword.models` accepts built-in names **or paths to a custom-trained `.onnx`
model**:

```yaml
wakeword:
  models: ["/home/orangepi/models/hey_panel.onnx"]   # your own phrase
```

Train one with [openWakeWord](https://github.com/dscripka/openWakeWord) and export
to ONNX (the `.tflite` export won't load on our ONNX inference path). Path entries
are validated at startup; built-in names (`alexa`, `hey_jarvis`, …) still work.

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

## How a turn works (wake → reply)

1. **Wake word** fires → LED ring shows a **pulsing-blue "connecting"** cue and any
   playing music is ducked.
2. The realtime connection is opened and the **instructions prompt is sent first**
   to warm it up. The prompt should end with a line telling the model *not* to reply
   yet, e.g. *"…Do not reply to this prompt; the next instruction is the user request."*
   The **wake word itself is never streamed to the model.**
3. Once the connection is live, the **`acknowledge.wav` earcon plays** and the LED
   turns **green ("speak now")** — only then does the mic start streaming your request.
4. Normal turn-taking follows (listening ⇄ thinking ⇄ speaking), with a follow-up
   window for multi-turn, closing on a closer phrase (e.g. "goodbye") or timeout.

This is controlled by `realtime.warmup_handshake` (default on) and the
`wakeword.wake_sound` earcon (the installer points it at the bundled
`assets/acknowledge.wav`).

## Operation & troubleshooting

**Manage the service**

```bash
systemctl --user status voiceagent
systemctl --user restart voiceagent
systemctl --user stop voiceagent          # stops cleanly in ~2-3 s (see below)
journalctl --user -u voiceagent -f        # follow logs (state= lines track the turn)
```

The state machine logs each transition as `state value=<idle|engaging|listening|
thinking|speaking>`; following the log is the quickest way to see where a turn is.

**A turn hangs in `thinking` (LED stuck pulsing blue).** If the s2s server accepts
the connection but never produces a response, the turn no longer hangs forever: a
**watchdog** (`realtime.turn_watchdog_s`, default 75 s) aborts the turn, plays the
error earcon, and returns to idle (`turn_watchdog_fired` in the log).

The whole stretch from the user finishing to the first audio is **silent on the wire**
— STT, then the Hermes agent's LLM/tool loop (tools run *inside* the agent, so their
latency is invisible to the client), then TTS. The s2s server emits `response.created`
only on the first audio chunk, so a long agent turn looks identical to a dead one. The
watchdog must therefore exceed your backend's *entire* response envelope; tool-free
turns already run ~25–30 s, so keep it generous. The proper fix is **server-side**: have
s2s (or the Hermes adapter) emit an early `response.created`/in-progress or a periodic
keepalive during the gap, so the client can both refresh the watchdog and show honest
"working" feedback — then this can be tightened again.

**The service won't stop / `systemctl stop` hangs.** The app now shuts down
gracefully on `SIGTERM`, interrupting an in-flight (or stalled) turn, so stop
completes in a couple of seconds. The unit also sets `TimeoutStopSec=15` and
`KillMode=mixed` as a backstop. A clean stop logs `orchestrator_stopped → audio_stopped
→ sendspin_stopped → stopped`.

**Audio output flips to HDMI/another card after a reboot.** PulseAudio's
`module-switch-on-connect` grabs whichever sink appears first at boot. The installer
pins the XVF3800 as the default sink/source via `~/.config/pulse/default.pa`
(`# voiceagent-managed`) and unloads that module. To re-pin manually after hardware
changes, re-run `install.sh`, or delete that file to manage PulseAudio yourself.
Verify with `pactl get-default-sink` / `pactl get-default-source`.

**Rapid close→re-wake misbehaves.** After a turn closes, the mic keeps flowing for
`realtime.post_close_grace_s` (default 2.5 s, wake detection suppressed) so the
XVF3800 AEC re-converges after music resume before the next wake is honored.

**Wake→reply feels slow on the first wake after idle.** Opening the s2s connection
costs ~5 s cold. Set `realtime.warm_connection: true` to keep one connection warm
during idle: a wake then reuses it (skipping the connect) and it's recycled on close
and re-opened in the background, so each wake-session still gets a fresh conversation.
If the warm connection isn't ready (a rapid re-wake while it's re-warming) the turn
falls back to an inline connect, so it never regresses. Logs `warm_connection_ready`
/ `realtime_using_warm_connection`. **Single device only** — the s2s server allows
one concurrent session.

## Multi-device wake arbitration

When several units are within earshot, you don't want them all answering one wake
word. Turn on `arbitration.enabled` and give the units the same `community` string:
on wake, each unit broadcasts a tiny UDP message — `{community, device_id, ts,
strength}` where **strength = wake score + the wake's audio energy (RMS)** — collects
peers' announcements for `window_ms`, and the **strongest (closest/loudest) wins**
(ties broken by `device_id`, so every unit agrees). The rest suppress and stay idle.

Periodic presence beacons let a unit know whether any peers exist, so a **solo unit
answers immediately** — the arbitration window only costs latency when peers are
actually present. Correlating "the same wake" across units leans on the shared clock,
so keep chrony/NTP in sync (you already need it for media). It fails *open*: any error
or no peers ⇒ handle the wake. Disabled by default.

Verify two units see each other on the LAN before relying on it:

```bash
voiceagent arbitration-test -s 30      # run on each unit; they should list each
                                       # other as peers and trade synthetic wakes
```

## Development

```bash
ruff check .          # lint
mypy                  # type-check (strict)
pytest                # tests
```

## License

Apache-2.0. © 2026 Eric Alborn, Anteon Group.
