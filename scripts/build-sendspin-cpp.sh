#!/usr/bin/env bash
# Build the sendspin-cpp `basic_client` reference player from source.
#
# This is a standalone helper for evaluating the `cpp` sendspin provider — it is
# intentionally NOT wired into install.sh yet (distribution path is still TBD).
# It clones sendspin-cpp, installs the Linux build deps, compiles basic_client,
# and prints (or installs) the resulting binary so you can point
# media.sendspin.binary at it.
#
# Usage:
#   scripts/build-sendspin-cpp.sh [--ref <git-ref>] [--no-deps] [--install]
#
#   --ref <ref>   git tag/branch/sha to build (default: v0.6.1)
#   --no-deps     skip the apt dependency install (deps already present)
#   --install     symlink the binary to ~/.local/bin/sendspin-cpp (on PATH)
set -euo pipefail

REF="v0.6.1"
INSTALL_DEPS=1
INSTALL_BIN=0
REPO="https://github.com/Sendspin/sendspin-cpp"
SRC_DIR="${SENDSPIN_CPP_SRC:-$HOME/.cache/sendspin-cpp}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --no-deps) INSTALL_DEPS=0; shift ;;
    --install) INSTALL_BIN=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ "$INSTALL_DEPS" == 1 ]]; then
  echo ">> installing build deps (sudo apt)…"
  sudo apt-get update -qq
  # cmake/toolchain + PortAudio (audio out) + Avahi compat (mDNS advertise)
  sudo apt-get install -y \
    git cmake build-essential \
    portaudio19-dev \
    libavahi-compat-libdnssd-dev
fi

echo ">> fetching sendspin-cpp @ $REF -> $SRC_DIR"
if [[ -d "$SRC_DIR/.git" ]]; then
  git -C "$SRC_DIR" fetch --tags --quiet origin
else
  git clone --quiet "$REPO" "$SRC_DIR"
fi
git -C "$SRC_DIR" checkout --quiet "$REF"
# Discard any prior applied patch so re-runs start clean, then re-apply.
git -C "$SRC_DIR" checkout --quiet -- examples/basic_client/main.cpp 2>/dev/null || true

# Apply our patches (idempotent: skip any already present). The key one gives
# basic_client a stable, name-derived client_id so each device is a distinct MA
# player instead of the example's hardcoded shared id.
PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/patches/sendspin-cpp"
if [[ -d "$PATCH_DIR" ]]; then
  for p in "$PATCH_DIR"/*.patch; do
    [[ -e "$p" ]] || continue
    if git -C "$SRC_DIR" apply --check "$p" 2>/dev/null; then
      git -C "$SRC_DIR" apply "$p" && echo ">> applied patch: $(basename "$p")"
    elif git -C "$SRC_DIR" apply --reverse --check "$p" 2>/dev/null; then
      echo ">> patch already applied: $(basename "$p")"
    else
      echo "!! patch failed to apply: $(basename "$p") (does it match $REF?)" >&2
      exit 1
    fi
  done
fi

echo ">> configuring + building (this is slow on an SBC)…"
cmake -S "$SRC_DIR" -B "$SRC_DIR/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$SRC_DIR/build" --target basic_client -j "$(nproc)"

BIN="$SRC_DIR/build/examples/basic_client/basic_client"
if [[ ! -x "$BIN" ]]; then
  echo "!! build finished but binary not found at $BIN" >&2
  exit 1
fi

echo ""
echo ">> built: $BIN"
"$BIN" -h 2>&1 | head -5 || true

if [[ "$INSTALL_BIN" == 1 ]]; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$BIN" "$HOME/.local/bin/sendspin-cpp"
  echo ">> linked: $HOME/.local/bin/sendspin-cpp -> $BIN"
  echo "   (set media.sendspin.provider: cpp; binary can stay null if ~/.local/bin is on PATH)"
else
  echo ">> set in config.yaml:  media.sendspin.binary: $BIN"
fi
