#!/usr/bin/env bash
# agent-voice-assistant uninstaller.
#   bash uninstall.sh [--purge] [--yes]
#   curl -fsSL https://raw.githubusercontent.com/winoiknow/agent-voice-assistant/main/uninstall.sh | bash -s -- --yes
#
# Removes the systemd user service, the PulseAudio default-sink pin, the udev rule,
# and the install directory (repo + venv). Your config/secrets and runtime state are
# KEPT unless you pass --purge. System apt packages (git, chrony, …) are left alone —
# other things may depend on them.
set -euo pipefail

INSTALL_DIR="${VOICEAGENT_DIR:-$HOME/agent-voice-assistant}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/voiceagent"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/voiceagent"
UNIT="$HOME/.config/systemd/user/voiceagent.service"
PULSE_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/pulse/default.pa"
UDEV_RULE="/etc/udev/rules.d/99-respeaker-xvf3800.rules"
PULSE_MARKER="# voiceagent-managed"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m WARN:\033[0m %s\n' "$*"; }

PURGE=0
ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    --yes | -y) ASSUME_YES=1 ;;
    *) echo "unknown argument: $a (use --purge and/or --yes)"; exit 2 ;;
  esac
done

if [ "$ASSUME_YES" != 1 ]; then
  printf 'Remove the voiceagent service, PulseAudio pin, udev rule, and %s?' "$INSTALL_DIR"
  [ "$PURGE" = 1 ] && printf '\n(--purge: ALSO delete config, secrets, and state)'
  printf ' [y/N] '
  read -r reply < /dev/tty || reply=""
  case "$reply" in
    y | Y | yes | YES) ;;
    *) echo "Aborted."; exit 0 ;;
  esac
fi

# 1. systemd user service.
if [ -f "$UNIT" ] || systemctl --user list-unit-files 2>/dev/null | grep -q '^voiceagent\.service'; then
  say "Stopping and disabling the systemd service"
  systemctl --user stop voiceagent.service 2>/dev/null || true
  systemctl --user disable voiceagent.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
fi

# 2. PulseAudio pin — only remove the file if we wrote it. Reload the module we
#    unloaded so stock auto-switch behavior is restored without waiting for a restart.
if [ -f "$PULSE_CFG" ] && grep -q "$PULSE_MARKER" "$PULSE_CFG"; then
  say "Removing the PulseAudio default-sink pin"
  rm -f "$PULSE_CFG"
  if command -v pactl >/dev/null 2>&1; then
    pactl load-module module-switch-on-connect >/dev/null 2>&1 || true
  fi
fi

# 3. udev rule (needs sudo).
if [ -f "$UDEV_RULE" ]; then
  say "Removing the XVF3800 udev rule (sudo may prompt)"
  sudo rm -f "$UDEV_RULE"
  sudo udevadm control --reload-rules 2>/dev/null || true
fi

# 4. Install directory (repo + venv + vendored binary). cd away first so removing
#    the cwd (if invoked from inside it) is safe.
cd "$HOME" 2>/dev/null || cd /
if [ -d "$INSTALL_DIR" ]; then
  say "Removing $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
fi

# 5. Config / secrets / state — kept by default, removed only with --purge.
if [ "$PURGE" = 1 ]; then
  say "Purging config, secrets, and state"
  rm -rf "$CONFIG_DIR" "$STATE_DIR"
else
  warn "Kept config/secrets at $CONFIG_DIR and state at $STATE_DIR (use --purge to remove)."
fi

cat <<EOF

$(say "Uninstalled.")
Left in place (shared / not ours to remove):
  - apt packages: git, python3-venv, libportaudio2, alsa-utils, chrony, …
  - any 'loginctl enable-linger' you set up for unattended boot
EOF
