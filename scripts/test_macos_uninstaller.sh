#!/usr/bin/env bash
# Scripted test for desktop/macos/Uninstall StagePilot.command.
#
# This machine is not macOS, so `security`/`diskutil`/`osascript` are
# stubbed with fakes on PATH that record their invocations and simulate the
# expected state, letting us exercise the script's actual control flow
# (path construction, removal logic, confirmation handling) end to end.
# This is the "scripted test that creates the same directory/keychain-entry
# shape and asserts full removal" fallback described in the task spec,
# used because CI cannot install/mount a real DMG on a non-macOS runner.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNINSTALLER="$SCRIPT_DIR/../desktop/macos/Uninstall StagePilot.command"
[[ -s "$UNINSTALLER" ]] || { echo "Uninstaller script not found: $UNINSTALLER" >&2; exit 1; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/stagepilot-uninstall-test.XXXXXX")"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

export HOME="$WORKDIR/home"
mkdir -p "$HOME"

FAKE_BIN="$WORKDIR/bin"
mkdir -p "$FAKE_BIN"
KEYCHAIN_STATE="$WORKDIR/keychain_present"
CALL_LOG="$WORKDIR/calls.log"
: > "$KEYCHAIN_STATE"  # empty file means "present"; removed file means "absent"

cat > "$FAKE_BIN/security" <<'EOF'
#!/usr/bin/env bash
echo "security $*" >> "$CALL_LOG"
case "$1" in
  find-generic-password)
    [[ -f "$KEYCHAIN_STATE" ]] && exit 0 || exit 44
    ;;
  delete-generic-password)
    rm -f "$KEYCHAIN_STATE"
    exit 0
    ;;
  *) exit 1 ;;
esac
EOF

cat > "$FAKE_BIN/diskutil" <<'EOF'
#!/usr/bin/env bash
echo "diskutil $*" >> "$CALL_LOG"
[[ "$1" == "eject" ]] && { rm -rf "$2"; exit 0; }
exit 1
EOF

cat > "$FAKE_BIN/osascript" <<'EOF'
#!/usr/bin/env bash
echo "osascript $*" >> "$CALL_LOG"
exit 0
EOF

cat > "$FAKE_BIN/pgrep" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF

cat > "$FAKE_BIN/pkill" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

export KEYCHAIN_STATE CALL_LOG
chmod +x "$FAKE_BIN"/*

# --- Build the pre-uninstall state ------------------------------------
APP_PATH="/Applications/StagePilot.app"
mkdir -p "$WORKDIR/fs$APP_PATH/Contents/MacOS"
touch "$WORKDIR/fs$APP_PATH/Contents/MacOS/StagePilot"

APP_DATA_DIR="$HOME/Library/Application Support/org.stagepilot.desktop"
APP_LOG_DIR="$HOME/Library/Logs/org.stagepilot.desktop"
mkdir -p "$APP_DATA_DIR/remote" "$APP_LOG_DIR"
echo "settings" > "$APP_DATA_DIR/settings.json"
echo "log line" > "$APP_LOG_DIR/stagepilot-backend.log"

VOLUME_DIR="$WORKDIR/Volumes/StagePilot-1.1.104-beta.8"
mkdir -p "$VOLUME_DIR"

# The real script hardcodes /Applications and /Volumes; rewrite a working
# copy so the test can point them at a throwaway sandbox without touching
# the real filesystem or requiring root.
SANDBOXED="$WORKDIR/Uninstall-sandboxed.command"
sed \
  -e "s#APP_PATH=\"/Applications/StagePilot.app\"#APP_PATH=\"$WORKDIR/fs$APP_PATH\"#" \
  -e "s#find /Volumes #find \"$WORKDIR/Volumes\" #" \
  "$UNINSTALLER" > "$SANDBOXED"
chmod +x "$SANDBOXED"

# --- Run the uninstaller non-interactively -----------------------------
PATH="$FAKE_BIN:$PATH" STAGEPILOT_UNINSTALL_ASSUME_YES=1 bash "$SANDBOXED" > "$WORKDIR/output.log" 2>&1
STATUS=$?

echo "--- uninstaller output ---"
cat "$WORKDIR/output.log"
echo "--- calls ---"
cat "$CALL_LOG" 2>/dev/null || true

fail() { echo "FAIL: $1" >&2; exit 1; }

[[ "$STATUS" -eq 0 ]] || fail "uninstaller exited with status $STATUS"
[[ ! -e "$WORKDIR/fs$APP_PATH" ]] || fail "app bundle was not removed"
[[ ! -e "$APP_DATA_DIR" ]] || fail "app data directory was not removed"
[[ ! -e "$APP_LOG_DIR" ]] || fail "app log directory was not removed"
[[ ! -f "$KEYCHAIN_STATE" ]] || fail "keychain entry was not removed"
[[ ! -e "$VOLUME_DIR" ]] || fail "mounted DMG volume was not ejected"
grep -q "security delete-generic-password -s StagePilot -a planning-center-secret" "$CALL_LOG" \
  || fail "expected keychain delete call not observed"
grep -q "diskutil eject $VOLUME_DIR" "$CALL_LOG" \
  || fail "expected diskutil eject call not observed"

echo "PASS: uninstaller removed app, data, logs, keychain entry, and ejected the mounted volume."
