#!/usr/bin/env bash
# Removes Pocket Relay Bridge from macOS or Linux.
#   ./uninstall.sh           remove the bridge, its start-at-login entry and the Antigravity plugin
#   ./uninstall.sh --purge   also remove saved pairings, settings and conversation history
set -euo pipefail

LABEL="com.antigravity.mobile.bridge"
UNIT="antigravity-mobile-bridge.service"
HOME_DIR="${HOME}/.antigravity-mobile"
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"

if [ "$(uname -s)" = "Darwin" ]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "${HOME}/Library/LaunchAgents/${LABEL}.plist"
else
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now "${UNIT}" 2>/dev/null || true
  fi
  rm -f "${HOME}/.config/systemd/user/${UNIT}"
fi

if command -v agy >/dev/null 2>&1; then
  agy plugin uninstall pocket-relay >/dev/null 2>&1 || true
fi

rm -rf "${HOME_DIR}/runtime"
echo "Removed Pocket Relay Bridge."

if [ "${1:-}" = "--purge" ]; then
  rm -rf "${HOME_DIR}"
  rm -f "${HOME}/Library/Logs/antigravity-mobile-bridge.log" "${HOME}/Library/Logs/antigravity-mobile-bridge.err.log"
  echo "Removed saved pairings, settings, conversation history and logs."
else
  echo "Saved pairings and history are still in ${HOME_DIR}. Run with --purge to remove them."
fi
