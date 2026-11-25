#!/usr/bin/env bash
# Pocket Relay Bridge installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/bpollak/pocket-relay-bridge/main/install.sh | bash
#
# What it does:
#   1. Checks for the Google Antigravity command-line tool (agy) and installs it with Google's
#      installer if it is missing (skip with POCKET_RELAY_NO_AGY_INSTALL=1).
#   2. Installs uv (a Python package manager) if it is missing.
#   3. Copies Pocket Relay Bridge to ~/.antigravity-mobile/runtime and installs its dependencies.
#   4. Sets the bridge to start when you log in (launchd on macOS, systemd on Linux).
#   5. Adds the Pocket Relay plugin to Antigravity.
#
# It does not need sudo and does not change anything outside your home folder.
#
# Options (environment variables):
#   POCKET_RELAY_PORT=8765          port the bridge listens on
#   POCKET_RELAY_SOURCE=/path       install from a local copy of this repository instead of downloading
#   POCKET_RELAY_INSTALL_DIR=/path  where to put the bridge (default ~/.antigravity-mobile/runtime)
#   POCKET_RELAY_NO_SERVICE=1       do not set the bridge to start at login
#   POCKET_RELAY_NO_PLUGIN=1        do not add the Antigravity plugin
#   POCKET_RELAY_ALLOW_NO_AGY=1     continue even if the Antigravity command-line tool is missing
set -euo pipefail

REPO="bpollak/pocket-relay-bridge"
PORT="${POCKET_RELAY_PORT:-8765}"
INSTALL_DIR="${POCKET_RELAY_INSTALL_DIR:-${HOME}/.antigravity-mobile/runtime}"
LABEL="com.antigravity.mobile.bridge"
UNIT="antigravity-mobile-bridge.service"

say()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mNote:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

OS="$(uname -s)"
case "${OS}" in
  Darwin|Linux) ;;
  *) die "This installer supports macOS and Linux. On Windows, use install.ps1 (see the README)." ;;
esac

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"

# 1. Antigravity command-line tool -------------------------------------------------------------
AGY=""
if command -v agy >/dev/null 2>&1; then
  AGY="$(command -v agy)"
fi
if [ -z "${AGY}" ] && [ "${POCKET_RELAY_NO_AGY_INSTALL:-}" != "1" ]; then
  say "Installing the Google Antigravity command-line tool (agy) with Google's installer"
  if curl -fsSL https://antigravity.google/cli/install.sh | bash; then
    export PATH="${HOME}/.local/bin:${PATH}"
    hash -r 2>/dev/null || true
    if command -v agy >/dev/null 2>&1; then AGY="$(command -v agy)"; fi
  fi
fi
if [ -z "${AGY}" ]; then
  if [ "${POCKET_RELAY_ALLOW_NO_AGY:-}" = "1" ]; then
    warn "The Antigravity command-line tool (agy) was not found. The bridge will install but cannot reach your agent until it is."
  else
    die "The Google Antigravity command-line tool (agy) could not be installed.
Install Google Antigravity from https://antigravity.google/ and sign in, then install its command-line tool:
    curl -fsSL https://antigravity.google/cli/install.sh | bash
Open a new Terminal window and run this installer again. Step-by-step help: https://pocketrelay.app/setup/"
  fi
else
  say "Found the Antigravity command-line tool: ${AGY}"
fi

# 2. uv ---------------------------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python package manager) from astral.sh"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
  command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH. Open a new terminal and run this installer again."
fi

# 3. Get the files ----------------------------------------------------------------------------
WORK=""
cleanup() { if [ -n "${WORK}" ] && [ -d "${WORK}" ]; then rm -rf "${WORK}"; fi; }
trap cleanup EXIT

if [ -n "${POCKET_RELAY_SOURCE:-}" ]; then
  SRC="${POCKET_RELAY_SOURCE}"
elif [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/bridge/pyproject.toml" ]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  WORK="$(mktemp -d)"
  say "Downloading Pocket Relay Bridge"
  curl -fsSL "https://github.com/${REPO}/archive/refs/heads/main.tar.gz" | tar -xz -C "${WORK}"
  SRC="$(find "${WORK}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
fi
[ -f "${SRC}/bridge/pyproject.toml" ] || die "Could not find the bridge files in ${SRC}."

say "Installing to ${INSTALL_DIR}"
rm -rf "${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
cp -R "${SRC}/bridge/antigravity_bridge" "${INSTALL_DIR}/"
cp "${SRC}/bridge/pyproject.toml" "${INSTALL_DIR}/"
[ -f "${SRC}/bridge/uv.lock" ] && cp "${SRC}/bridge/uv.lock" "${INSTALL_DIR}/"
find "${INSTALL_DIR}" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

( cd "${INSTALL_DIR}" && uv sync --python 3.13 --no-dev )
BIN="${INSTALL_DIR}/.venv/bin/antigravity-bridge"
[ -x "${BIN}" ] || die "The bridge did not install correctly (${BIN} is missing)."

# 4. Start at login ---------------------------------------------------------------------------
if [ "${POCKET_RELAY_NO_SERVICE:-}" = "1" ]; then
  warn "Skipping start-at-login. Start the bridge yourself with: ${BIN} --host 0.0.0.0 --port ${PORT}"
elif [ "${OS}" = "Darwin" ]; then
  PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
  mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs"
  cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${BIN}</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>${PORT}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/Library/Logs/antigravity-mobile-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/Library/Logs/antigravity-mobile-bridge.err.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "${PLIST}"
  say "Pocket Relay Bridge will start when you log in (launchd: ${LABEL})"
else
  UNIT_DIR="${HOME}/.config/systemd/user"
  mkdir -p "${UNIT_DIR}"
  cat > "${UNIT_DIR}/${UNIT}" <<EOF
[Unit]
Description=Pocket Relay Bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=${BIN} --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
  if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable --now "${UNIT}"
    say "Pocket Relay Bridge will start when you log in (systemd user service: ${UNIT})"
  else
    warn "systemd user services are not available here. Start the bridge yourself with: ${BIN} --host 0.0.0.0 --port ${PORT}"
  fi
fi

# 5. Antigravity plugin -----------------------------------------------------------------------
if [ "${POCKET_RELAY_NO_PLUGIN:-}" = "1" ]; then
  warn "Skipping the Antigravity plugin."
elif [ -n "${AGY}" ]; then
  say "Adding the Pocket Relay plugin to Antigravity"
  "${AGY}" plugin uninstall pocket-relay >/dev/null 2>&1 || true
  if ! "${AGY}" plugin install "${SRC}/plugin/pocket-relay"; then
    warn "The plugin could not be added. You can still pair from http://127.0.0.1:${PORT}/pair on this computer."
  fi
fi

# 6. Check it is running ----------------------------------------------------------------------
if [ "${POCKET_RELAY_NO_SERVICE:-}" != "1" ]; then
  ok=""
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  if [ -n "${ok}" ]; then
    say "Pocket Relay Bridge is running on port ${PORT}."
    PAIR_URL="http://127.0.0.1:${PORT}/pair"
    if [ "${POCKET_RELAY_NO_OPEN:-}" != "1" ]; then
      if [ "$(uname -s)" = "Darwin" ] && command -v open >/dev/null 2>&1; then
        open "${PAIR_URL}" >/dev/null 2>&1 && say "Opened the pairing QR code in your browser."
      elif command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        xdg-open "${PAIR_URL}" >/dev/null 2>&1 && say "Opened the pairing QR code in your browser."
      fi
    fi
  else
    warn "The bridge did not answer on port ${PORT} yet. Check the log, then run this installer again if needed."
  fi
fi

cat <<EOF

Next: pair your iPhone or iPad
  1. In Antigravity, open the Pocket Relay panel. It shows a QR code.
     (No panel? Open http://127.0.0.1:${PORT}/pair in a browser on this computer.)
  2. In the Pocket Relay app, tap Scan QR Code and point the camera at the code.

Your computer must be on and awake, and your phone on the same Wi-Fi or private network.
To remove everything later, run uninstall.sh from https://github.com/${REPO}
EOF
