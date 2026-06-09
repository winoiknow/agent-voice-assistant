#!/usr/bin/env bash
# agent-voice-assistant installer.
#   curl -fsSL https://raw.githubusercontent.com/winoiknow/agent-voice-assistant/main/install.sh | bash
#
# Installs system deps, the package + on-device extras, the reSpeaker xvf_host
# control binary, a udev rule, runs the config wizard, and registers a systemd
# user service. Idempotent — safe to re-run to update.
set -euo pipefail

REPO_URL="${VOICEAGENT_REPO:-https://github.com/winoiknow/agent-voice-assistant.git}"
BRANCH="${VOICEAGENT_BRANCH:-main}"
INSTALL_DIR="${VOICEAGENT_DIR:-$HOME/agent-voice-assistant}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/voiceagent"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m WARN:\033[0m %s\n' "$*"; }

arch="$(uname -m)"
[ "$arch" = "aarch64" ] || warn "architecture is '$arch'; the vendored xvf_host is aarch64 only."

# 1. System packages (chrony: sendspin's playback sync is clock-driven).
say "Installing system packages (sudo may prompt)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git python3-venv python3-pip libportaudio2 alsa-utils curl chrony

# 2. Clone or update the repo.
if [ -d "$INSTALL_DIR/.git" ]; then
  say "Updating $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only --quiet
else
  say "Cloning into $INSTALL_DIR"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# 3. venv + package (+ on-device extras). openWakeWord is --no-deps because it
#    hard-requires tflite-runtime on Linux (no aarch64/py3.12 wheel); we use ONNX.
say "Creating virtualenv and installing the package"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[audio,wakeword,realtime,media]"
pip install --quiet --no-deps openwakeword
pip install --quiet sendspin

# 4. Vendor the reSpeaker xvf_host control binary (aarch64 build).
if [ "$arch" = "aarch64" ] && [ ! -x "vendor/xvf_host/xvf_host" ]; then
  say "Fetching xvf_host (reSpeaker rpi_64bit)"
  tmp="$(mktemp -d)"
  git clone --quiet --depth 1 --filter=blob:none --sparse \
    https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git "$tmp"
  git -C "$tmp" sparse-checkout set host_control/rpi_64bit >/dev/null
  mkdir -p vendor/xvf_host
  cp -f "$tmp"/host_control/rpi_64bit/* vendor/xvf_host/
  chmod +x vendor/xvf_host/xvf_host vendor/xvf_host/xvf_i2c_dfu
  rm -rf "$tmp"
fi
XVF_HOST="$INSTALL_DIR/vendor/xvf_host/xvf_host"
[ -x "$XVF_HOST" ] || XVF_HOST="xvf_host"

# 5. udev rule so xvf_host reaches the XVF3800 without root.
if [ -f packaging/udev/99-respeaker-xvf3800.rules ]; then
  say "Installing udev rule for the XVF3800"
  sudo install -m0644 packaging/udev/99-respeaker-xvf3800.rules /etc/udev/rules.d/
  sudo udevadm control --reload-rules
  sudo udevadm trigger --attr-match=idVendor=2886 || true
fi

# 6. Configuration wizard (skipped if a config already exists — re-run
#    `voiceagent init --force` to redo).
if [ -f "$CONFIG_DIR/config.yaml" ] && [ -z "${VOICEAGENT_FORCE_INIT:-}" ]; then
  say "Config exists at $CONFIG_DIR/config.yaml — keeping it (skipping wizard)"
else
  say "Configuration"
  DEFAULT_MODEL="$INSTALL_DIR/models/wakeword/Belvedere.onnx"
  [ -f "$DEFAULT_MODEL" ] || DEFAULT_MODEL="alexa"
  voiceagent init \
    --config "$CONFIG_DIR/config.yaml" \
    --secrets "$CONFIG_DIR/secrets.env" \
    --xvf-host-path "$XVF_HOST" \
    --default-model "$DEFAULT_MODEL" < /dev/tty
fi

# 7. systemd user service (runs in the graphical session so it shares PulseAudio).
say "Installing systemd user service"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/voiceagent.service" <<EOF
[Unit]
Description=agent-voice-assistant (headless voice assistant)
After=network-online.target pipewire.service pulseaudio.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/.venv/bin/voiceagent run --config $CONFIG_DIR/config.yaml
EnvironmentFile=-$CONFIG_DIR/secrets.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now voiceagent.service || true

cat <<EOF

$(say "Installed.")
  Service : systemctl --user status voiceagent
  Logs    : journalctl --user -u voiceagent -f
  Config  : $CONFIG_DIR/config.yaml   (re-run: voiceagent init --force)

Note: the service runs in your graphical login session (for PulseAudio access).
For unattended boot, enable desktop auto-login, or 'loginctl enable-linger $USER'
once PulseAudio/PipeWire is confirmed to start under a lingering session.
EOF
