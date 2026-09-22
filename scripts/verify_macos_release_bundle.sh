#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 (--app APP | --dmg DMG | --archive APP_TAR_GZ) --arch (x86_64|arm64)" >&2
  exit 2
}

MODE=""
INPUT=""
EXPECTED_ARCH=""
while (($#)); do
  case "$1" in
    --app|--dmg|--archive) MODE="${1#--}"; INPUT="${2:-}"; shift 2 ;;
    --arch) EXPECTED_ARCH="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$MODE" && -n "$INPUT" && -n "$EXPECTED_ARCH" ]] || usage
[[ "$EXPECTED_ARCH" == "x86_64" || "$EXPECTED_ARCH" == "arm64" ]] || usage
[[ -e "$INPUT" ]] || { echo "Missing release artifact: $INPUT" >&2; exit 1; }

TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/stagepilot-release-verify.XXXXXX")"
MOUNT_POINT=""
BACKEND_PID=""
LOG_FILE="$TEMP_ROOT/backend.log"
cleanup() {
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ -n "$MOUNT_POINT" ]]; then
    hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true
  fi
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT

case "$MODE" in
  app)
    APP="$INPUT"
    ;;
  dmg)
    MOUNT_POINT="$TEMP_ROOT/mount"
    mkdir -p "$MOUNT_POINT"
    hdiutil attach "$INPUT" -readonly -nobrowse -mountpoint "$MOUNT_POINT" -quiet
    APP_COUNT="$(find "$MOUNT_POINT" -maxdepth 2 -type d -name 'StagePilot.app' | wc -l | tr -d ' ')"
    [[ "$APP_COUNT" -eq 1 ]] || { echo "Expected exactly one StagePilot.app in $INPUT." >&2; exit 1; }
    SOURCE_APP="$(find "$MOUNT_POINT" -maxdepth 2 -type d -name 'StagePilot.app' -print -quit)"
    cp -R "$SOURCE_APP" "$TEMP_ROOT/StagePilot.app"
    APP="$TEMP_ROOT/StagePilot.app"
    UNINSTALLER="$(find "$MOUNT_POINT" -maxdepth 1 -type f -name 'Uninstall StagePilot.command' -print -quit)"
    [[ -n "$UNINSTALLER" ]] || { echo "DMG is missing the macOS uninstaller (Uninstall StagePilot.command)." >&2; exit 1; }
    [[ -x "$UNINSTALLER" ]] || { echo "Uninstall StagePilot.command in the DMG is not executable." >&2; exit 1; }
    bash -n "$UNINSTALLER" || { echo "Uninstall StagePilot.command has a syntax error." >&2; exit 1; }
    ;;
  archive)
    mkdir -p "$TEMP_ROOT/archive"
    tar -xzf "$INPUT" -C "$TEMP_ROOT/archive"
    APP_COUNT="$(find "$TEMP_ROOT/archive" -type d -name 'StagePilot.app' | wc -l | tr -d ' ')"
    [[ "$APP_COUNT" -eq 1 ]] || { echo "Expected exactly one StagePilot.app in $INPUT." >&2; exit 1; }
    APP="$(find "$TEMP_ROOT/archive" -type d -name 'StagePilot.app' -print -quit)"
    ;;
  *) usage ;;
esac

SIDECAR_COUNT="$(find "$APP/Contents/MacOS" -maxdepth 1 -type f -name 'stagepilot-backend*' | wc -l | tr -d ' ')"
[[ "$SIDECAR_COUNT" -eq 1 ]] || { echo "Expected exactly one packaged backend sidecar." >&2; exit 1; }
SIDECAR="$(find "$APP/Contents/MacOS" -maxdepth 1 -type f -name 'stagepilot-backend*' -print -quit)"
MAIN_NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Contents/Info.plist")"
MAIN="$APP/Contents/MacOS/$MAIN_NAME"
[[ -x "$MAIN" ]] || { echo "Missing StagePilot application executable." >&2; exit 1; }
HELP_FOLDER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleHelpBookFolder' "$APP/Contents/Info.plist")"
HELP_NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleHelpBookName' "$APP/Contents/Info.plist")"
[[ "$HELP_FOLDER" == "StagePilot.help" ]] || { echo "StagePilot Help folder is not registered." >&2; exit 1; }
[[ "$HELP_NAME" == "org.stagepilot.desktop.help" ]] || { echo "StagePilot Help identifier is not registered." >&2; exit 1; }
HELP_ROOT="$APP/Contents/Resources/$HELP_FOLDER/Contents/Resources/English.lproj"
[[ -s "$HELP_ROOT/index.html" ]] || { echo "StagePilot Help landing page is missing." >&2; exit 1; }
[[ -s "$HELP_ROOT/StagePilot.helpindex" ]] || { echo "StagePilot Help search index is missing." >&2; exit 1; }
find "$HELP_ROOT/pages" -type f -name '*.html' -print -quit | grep -q . || {
  echo "StagePilot Help contains no searchable documentation pages." >&2
  exit 1
}

echo "Application: $APP"
echo "Backend: $SIDECAR"
file "$MAIN"
file "$SIDECAR"
file "$MAIN" | grep -q "$EXPECTED_ARCH" || { echo "Application is not $EXPECTED_ARCH." >&2; exit 1; }
file "$SIDECAR" | grep -q "$EXPECTED_ARCH" || { echo "Backend is not $EXPECTED_ARCH." >&2; exit 1; }
codesign --verify --deep --strict --verbose=4 "$APP"
SIGNATURE="$(codesign -d --verbose=4 "$SIDECAR" 2>&1)"
printf '%s\n' "$SIGNATURE"
codesign -d --entitlements :- "$SIDECAR" 2>/dev/null || true
grep -q 'flags=.*runtime' <<<"$SIGNATURE" && {
  echo "Packaged backend unexpectedly has the Hardened Runtime flag." >&2
  exit 1
}
grep -Eq '^TeamIdentifier=(.+)$' <<<"$SIGNATURE" && ! grep -q '^TeamIdentifier=not set$' <<<"$SIGNATURE" && {
  echo "Ad-hoc backend unexpectedly has a TeamIdentifier." >&2
  exit 1
}

verify_minos() {
  local binary="$1"
  local label="$2"
  local minos
  minos="$(xcrun vtool -show-build "$binary" 2>/dev/null | awk '/minos / {print $2}' | sort -Vu | tail -1)"
  if [[ -z "$minos" ]]; then
    # PyInstaller's macOS bootloader can use the older
    # LC_VERSION_MIN_MACOSX command rather than LC_BUILD_VERSION.
    minos="$(
      otool -l "$binary" 2>/dev/null |
        awk '
          $1 == "cmd" { version_command = ($2 == "LC_VERSION_MIN_MACOSX") }
          version_command && $1 == "version" {
            print $2
            version_command = 0
          }
        ' |
        sort -Vu |
        tail -1
    )"
  fi
  [[ -n "$minos" ]] || { echo "Could not determine deployment target for $label." >&2; exit 1; }
  python3 - "$minos" "$label" <<'PY'
import sys
value, label = sys.argv[1:]
parts = tuple(int(part) for part in value.split("."))
if parts > (12, 0):
    raise SystemExit(f"{label} requires macOS {value}, newer than supported 12.0")
print(f"{label} minimum macOS: {value}")
PY
}
verify_minos "$MAIN" "StagePilot"
verify_minos "$SIDECAR" "StagePilot backend"

PORT="$(python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
SETTINGS="$TEMP_ROOT/settings.json"
printf '{"service_source":"demo","midi_source":"simulated","timer_output":"simulated","server_port":%s}\n' "$PORT" > "$SETTINGS"
STAGEPILOT_HOST=127.0.0.1 \
STAGEPILOT_PORT="$PORT" \
STAGEPILOT_SETTINGS_PATH="$SETTINGS" \
"$SIDECAR" >"$LOG_FILE" 2>&1 &
BACKEND_PID="$!"

for _ in {1..120}; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "Packaged backend exited before readiness." >&2
    cat "$LOG_FILE" >&2
    exit 1
  fi
  if curl --fail --silent --show-error "http://127.0.0.1:$PORT/api/v1/health" >"$TEMP_ROOT/health.json"; then
    grep -q '"application_status"' "$TEMP_ROOT/health.json" || {
      echo "Health response did not identify StagePilot." >&2
      cat "$TEMP_ROOT/health.json" >&2
      exit 1
    }
    COOKIE_JAR="$TEMP_ROOT/dashboard-cookie.txt"
    curl --fail --silent --show-error \
      --cookie-jar "$COOKIE_JAR" \
      --header "Content-Type: application/json" \
      --data '{"pin":"1234"}' \
      "http://127.0.0.1:$PORT/api/v1/dashboard-auth/login" >/dev/null
    curl --fail --silent --show-error \
      --cookie "$COOKIE_JAR" \
      "http://127.0.0.1:$PORT/api/v1/state" >/dev/null
    echo "Packaged backend health check passed."
    exit 0
  fi
  sleep 0.25
done

echo "Timed out waiting for packaged backend health." >&2
cat "$LOG_FILE" >&2
exit 1
