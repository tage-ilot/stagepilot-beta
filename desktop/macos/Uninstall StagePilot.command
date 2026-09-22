#!/usr/bin/env bash
# Uninstalls StagePilot and removes all of its user-owned state on macOS.
#
# Removes:
#   1. /Applications/StagePilot.app
#   2. ~/Library/Application Support/org.stagepilot.desktop (app data, incl. "remote" state)
#   3. ~/Library/Logs/org.stagepilot.desktop (backend + app logs)
#   4. The Planning Center PAT stored in the macOS Keychain
#      (service "StagePilot", account "planning-center-secret")
#   5. Any mounted StagePilot DMG volumes left over from installs/updates
#
# No sudo / elevated privileges are required: everything above is owned by
# the current user. If double-clicking this file does nothing (or macOS
# refuses to open it), right-click it in Finder, choose "Open", then
# confirm in the dialog that appears — this is a one-time Gatekeeper
# quarantine step for unsigned scripts. See "README - Uninstall.txt" next
# to this file for details.
set -euo pipefail

APP_PATH="/Applications/StagePilot.app"
APP_DATA_DIR="$HOME/Library/Application Support/org.stagepilot.desktop"
APP_LOG_DIR="$HOME/Library/Logs/org.stagepilot.desktop"
KEYCHAIN_SERVICE="StagePilot"
KEYCHAIN_ACCOUNT="planning-center-secret"
BUNDLE_ID="org.stagepilot.desktop"

# STAGEPILOT_UNINSTALL_ASSUME_YES=1 skips the interactive confirmation.
# Used only by the automated test harness (scripts/test_macos_uninstaller.sh);
# a normal double-clicked run always prompts.
ASSUME_YES="${STAGEPILOT_UNINSTALL_ASSUME_YES:-0}"

log() { printf '%s\n' "$1"; }

log "StagePilot Uninstaller"
log "======================"
log ""
log "This will remove:"
[[ -e "$APP_PATH" ]] && log "  - Application:     $APP_PATH" || log "  - Application:     (not found, skipping)"
[[ -e "$APP_DATA_DIR" ]] && log "  - App data/config: $APP_DATA_DIR" || log "  - App data/config: (not found, skipping)"
[[ -e "$APP_LOG_DIR" ]] && log "  - App logs:        $APP_LOG_DIR" || log "  - App logs:        (not found, skipping)"
log "  - Keychain entry:  service \"$KEYCHAIN_SERVICE\" (Planning Center credential, if present)"
MOUNTED_VOLUMES=()
while IFS= read -r -d '' volume; do
  MOUNTED_VOLUMES+=("$volume")
done < <(find /Volumes -maxdepth 1 -iname 'StagePilot*' -print0 2>/dev/null || true)
if ((${#MOUNTED_VOLUMES[@]} > 0)); then
  for volume in "${MOUNTED_VOLUMES[@]}"; do
    log "  - Mounted volume:  $volume (will be ejected)"
  done
else
  log "  - Mounted volumes: (none found)"
fi
log ""

if [[ "$ASSUME_YES" != "1" ]]; then
  read -r -p "Proceed with removal? [y/N] " REPLY
  case "$REPLY" in
    [yY]|[yY][eE][sS]) ;;
    *) log "Cancelled. Nothing was removed."; exit 0 ;;
  esac
fi

REMOVED=()
SKIPPED=()

# 1. Quit StagePilot if running (graceful terminate, no force-kill unless needed).
if pgrep -x "StagePilot" >/dev/null 2>&1; then
  log "Quitting StagePilot..."
  osascript -e 'tell application "StagePilot" to quit' >/dev/null 2>&1 || true
  for _ in {1..10}; do
    pgrep -x "StagePilot" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if pgrep -x "StagePilot" >/dev/null 2>&1; then
    log "StagePilot did not quit gracefully; sending SIGTERM..."
    pkill -TERM -x "StagePilot" >/dev/null 2>&1 || true
    sleep 1
  fi
  if pgrep -x "StagePilot" >/dev/null 2>&1; then
    log "StagePilot is still running; leaving it in place and skipping app removal."
    SKIPPED+=("$APP_PATH (app still running)")
  fi
fi

# 2. Remove the application bundle.
if [[ -e "$APP_PATH" ]] && ! pgrep -x "StagePilot" >/dev/null 2>&1; then
  rm -rf "$APP_PATH"
  REMOVED+=("$APP_PATH")
elif [[ ! -e "$APP_PATH" ]]; then
  : # already absent, nothing to report beyond the earlier notice
fi

# 3. Remove app data/config directory.
if [[ -e "$APP_DATA_DIR" ]]; then
  rm -rf "$APP_DATA_DIR"
  REMOVED+=("$APP_DATA_DIR")
fi

# 4. Remove app log directory.
if [[ -e "$APP_LOG_DIR" ]]; then
  rm -rf "$APP_LOG_DIR"
  REMOVED+=("$APP_LOG_DIR")
fi

# 5. Remove the Keychain entry for the Planning Center secret.
if security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" >/dev/null 2>&1; then
  if security delete-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" >/dev/null 2>&1; then
    REMOVED+=("Keychain entry \"$KEYCHAIN_SERVICE\"")
  else
    SKIPPED+=("Keychain entry \"$KEYCHAIN_SERVICE\" (removal failed)")
  fi
else
  : # not present, nothing to remove
fi

# 6. Eject any mounted StagePilot DMG volumes.
for volume in "${MOUNTED_VOLUMES[@]}"; do
  if diskutil eject "$volume" >/dev/null 2>&1; then
    REMOVED+=("Mounted volume $volume (ejected)")
  else
    SKIPPED+=("Mounted volume $volume (eject failed; eject manually)")
  fi
done

log ""
log "Done."
if ((${#REMOVED[@]} > 0)); then
  log "Removed:"
  for item in "${REMOVED[@]}"; do
    log "  - $item"
  done
else
  log "Nothing needed to be removed (StagePilot was not installed for this user)."
fi
if ((${#SKIPPED[@]} > 0)); then
  log ""
  log "Skipped:"
  for item in "${SKIPPED[@]}"; do
    log "  - $item"
  done
fi
log ""
log "Bundle identifier reference: $BUNDLE_ID"
