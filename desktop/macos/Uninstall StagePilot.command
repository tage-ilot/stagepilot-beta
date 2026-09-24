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
#   6. Stale StagePilot.app copies in ~/.Trash and Gatekeeper App Translocation
#      images, plus a Launch Services database rebuild so macOS stops resolving
#      launches (Dock/Spotlight/"Open Recent") to any of those old paths
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
  log ""
  log "  NOTE: ${#MOUNTED_VOLUMES[@]} StagePilot disk image(s) are still mounted."
  log "  macOS Launch Services can resolve a Dock/Spotlight/\"Open Recent\" launch"
  log "  to the copy inside one of these images instead of the freshly installed"
  log "  app, which is how an older StagePilot appears to \"come back\". They are"
  log "  ejected below and the Launch Services database is rebuilt."
else
  log "  - Mounted volumes: (none found)"
fi
TRASHED_COPIES=()
while IFS= read -r -d '' trashed; do
  TRASHED_COPIES+=("$trashed")
done < <(find "$HOME/.Trash" -maxdepth 1 -iname 'StagePilot*.app' -print0 2>/dev/null || true)
if ((${#TRASHED_COPIES[@]} > 0)); then
  for trashed in "${TRASHED_COPIES[@]}"; do
    log "  - Trashed copy:    $trashed (will be removed)"
  done
else
  log "  - Trashed copies:  (none found)"
fi
log "  - Launch Services: registrations for StagePilot will be rebuilt"
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

# 7. Remove stale StagePilot.app copies sitting in the Trash, and stale
#    Gatekeeper App Translocation images. Both remain registered with Launch
#    Services after an "uninstall", so a later launch can resolve to one of
#    them and run an old version out of a read-only image.
for trashed in "${TRASHED_COPIES[@]}"; do
  if rm -rf "$trashed" 2>/dev/null; then
    REMOVED+=("Stale copy $trashed")
  else
    SKIPPED+=("Stale copy $trashed (removal failed)")
  fi
done

TRANSLOCATED_KILLED=0
while IFS= read -r pid; do
  [[ -n "$pid" ]] || continue
  kill -TERM "$pid" 2>/dev/null && TRANSLOCATED_KILLED=$((TRANSLOCATED_KILLED + 1)) || true
done < <(pgrep -f '/AppTranslocation/.*StagePilot' 2>/dev/null || true)
if ((TRANSLOCATED_KILLED > 0)); then
  REMOVED+=("$TRANSLOCATED_KILLED translocated StagePilot process(es) stopped")
fi

while IFS= read -r -d '' image; do
  if rm -rf "$image" 2>/dev/null; then
    REMOVED+=("Translocation image $image")
  else
    SKIPPED+=("Translocation image $image (macOS may still hold it; it is temporary and cleared on reboot)")
  fi
done < <(find "${TMPDIR:-/tmp}" -maxdepth 3 -type d -name 'AppTranslocation' -print0 2>/dev/null || true)

# 8. Rebuild the Launch Services database so macOS forgets every stale
#    StagePilot.app registration (translocated images, ejected DMG volumes,
#    trashed copies). This is what stops an old version from "coming back"
#    when the user launches from the Dock, Spotlight, or Finder.
#    STAGEPILOT_LSREGISTER only exists so the automated test harness can point
#    this at a stub; a normal run always uses the real macOS binary.
LSREGISTER="${STAGEPILOT_LSREGISTER:-/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister}"
if [[ -x "$LSREGISTER" ]]; then
  if "$LSREGISTER" -kill -r -domain local -domain system -domain user >/dev/null 2>&1; then
    REMOVED+=("Launch Services registrations for StagePilot (database rebuilt)")
  else
    SKIPPED+=("Launch Services rebuild (lsregister failed; log out and back in if an old StagePilot still launches)")
  fi
else
  SKIPPED+=("Launch Services rebuild (lsregister not found on this macOS version)")
fi

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
