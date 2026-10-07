#!/usr/bin/env bash
# After `tauri build` (ad-hoc), re-sign StagePilot.app with the stable StagePilot identity,
# then rebuild the pieces that embed it: the updater archive (+ fresh minisign signature)
# and the DMG. Never falls back to ad-hoc: a missing secret fails the build.
#
# Env: MACOS_SIGNING_P12_BASE64, MACOS_SIGNING_P12_PASSWORD (required),
#      TAURI_SIGNING_PRIVATE_KEY, TAURI_SIGNING_PRIVATE_KEY_PASSWORD (updater minisign key).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="${1:-$HERE/../desktop/src-tauri/target/release/bundle}"
: "${MACOS_SIGNING_P12_BASE64:?MACOS_SIGNING_P12_BASE64 is not configured; refusing to ship an ad-hoc build.}"
: "${MACOS_SIGNING_P12_PASSWORD:?MACOS_SIGNING_P12_PASSWORD is not configured.}"
: "${TAURI_SIGNING_PRIVATE_KEY:?TAURI_SIGNING_PRIVATE_KEY is not configured.}"

APP="$BUNDLE/macos/StagePilot.app"
[[ -d "$APP" ]] || { echo "Missing $APP" >&2; exit 1; }
WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/sp-resign.XXXXXX")"
KC="$WORK/signing.keychain-db"
KCPASS="$(uuidgen)"
echo "::add-mask::$KCPASS"
ORIG_KEYCHAINS="$(security list-keychains -d user | tr -d '"' | tr '\n' ' ')"
cleanup() {
  # shellcheck disable=SC2086
  security list-keychains -d user -s $ORIG_KEYCHAINS 2>/dev/null || true
  security delete-keychain "$KC" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

printf '%s' "$MACOS_SIGNING_P12_BASE64" | base64 --decode > "$WORK/id.p12"
security create-keychain -p "$KCPASS" "$KC"
security set-keychain-settings -lut 3600 "$KC"
security unlock-keychain -p "$KCPASS" "$KC"
security import "$WORK/id.p12" -k "$KC" -P "$MACOS_SIGNING_P12_PASSWORD" -T /usr/bin/codesign >/dev/null
security set-key-partition-list -S apple-tool:,apple: -s -k "$KCPASS" "$KC" >/dev/null
# shellcheck disable=SC2086
security list-keychains -d user -s "$KC" $ORIG_KEYCHAINS
rm -f "$WORK/id.p12"
IDENTITY="StagePilot Beta Code Signing"
security find-identity -p codesigning "$KC" | grep -q "$IDENTITY" || { echo "Signing identity not found after import." >&2; exit 1; }

bash "$HERE/sign_macos_bundle.sh" --app "$APP" --identity "$IDENTITY" --keychain "$KC"

# Updater archive: same layout Tauri produces (StagePilot.app at the archive root).
ARCHIVE="$(find "$BUNDLE/macos" -maxdepth 1 -type f -name '*.app.tar.gz' -print -quit)"
[[ -n "$ARCHIVE" ]] || { echo "Updater archive not found." >&2; exit 1; }
rm -f "$ARCHIVE" "$ARCHIVE.sig"
COPYFILE_DISABLE=1 tar -czf "$ARCHIVE" -C "$BUNDLE/macos" StagePilot.app
(cd "$HERE/../desktop" && npx tauri signer sign "$ARCHIVE")
[[ -s "$ARCHIVE.sig" ]] || { echo "Updater signature was not regenerated." >&2; exit 1; }

# DMG: repack the signed app together with the uninstaller.
for dmg in "$BUNDLE"/dmg/*.dmg; do
  STAGEPILOT_SIGNED_APP="$APP" bash "$HERE/inject_macos_uninstaller.sh" "$dmg"
done
echo "Re-signed app, updater archive and DMG with the stable StagePilot identity."
