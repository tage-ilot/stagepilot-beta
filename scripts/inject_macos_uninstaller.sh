#!/usr/bin/env bash
# Repacks a Tauri-built StagePilot DMG to add the macOS uninstaller
# ("Uninstall StagePilot.command" + README) alongside StagePilot.app, so it
# is visible in Finder the moment a user opens the DMG.
#
# Usage: inject_macos_uninstaller.sh <path-to-StagePilot.dmg>
#
# Requires macOS (hdiutil). Run this after `tauri build` produces the DMG
# and before the DMG is collected as a release asset.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <path-to-StagePilot.dmg>" >&2
  exit 2
fi

DMG_PATH="$1"
[[ -s "$DMG_PATH" ]] || { echo "DMG not found: $DMG_PATH" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNINSTALLER_SRC="$SCRIPT_DIR/../desktop/macos/Uninstall StagePilot.command"
README_SRC="$SCRIPT_DIR/../desktop/macos/README - Uninstall.txt"
[[ -s "$UNINSTALLER_SRC" ]] || { echo "Missing uninstaller script: $UNINSTALLER_SRC" >&2; exit 1; }
[[ -s "$README_SRC" ]] || { echo "Missing uninstaller README: $README_SRC" >&2; exit 1; }

TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/stagepilot-dmg-inject.XXXXXX")"
MOUNT_POINT="$TEMP_ROOT/mount"
CONTENTS_DIR="$TEMP_ROOT/contents"
mkdir -p "$MOUNT_POINT" "$CONTENTS_DIR"

cleanup() {
  hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT

hdiutil attach "$DMG_PATH" -readonly -nobrowse -mountpoint "$MOUNT_POINT" -quiet
VOLNAME="$(basename "$(hdiutil info | awk -v mp="$MOUNT_POINT" '$0 ~ mp {print}' | head -1 || true)")"
[[ -n "$VOLNAME" ]] || VOLNAME="StagePilot"

# Copy the DMG's existing contents (StagePilot.app, the /Applications
# symlink, any background image) verbatim.
cp -R "$MOUNT_POINT/." "$CONTENTS_DIR/"
hdiutil detach "$MOUNT_POINT" -quiet
MOUNT_POINT=""

cp "$UNINSTALLER_SRC" "$CONTENTS_DIR/Uninstall StagePilot.command"
cp "$README_SRC" "$CONTENTS_DIR/README - Uninstall.txt"
chmod +x "$CONTENTS_DIR/Uninstall StagePilot.command"

# Ship the DMG payload free of extended attributes.
#
# macOS adds `com.apple.quarantine` to the *downloaded DMG* itself, and
# propagates it to whatever the user drags out — that part is inherent to an
# ad-hoc-signed, non-notarized app and cannot be prevented here. What we can
# prevent is baking any *additional* xattrs (a stale quarantine flag from the
# build machine, resource forks from `cp -R`) into the bundle we ship, which
# would make Gatekeeper App Translocation stick even after the user clears the
# flag on /Applications/StagePilot.app. `xattr -cr` on the staging folder is
# purely subtractive: it never touches code signatures, only xattrs.
if command -v xattr >/dev/null 2>&1; then
  xattr -cr "$CONTENTS_DIR" || echo "warning: could not clear extended attributes on the DMG payload" >&2
  if REMAINING="$(xattr -lr "$CONTENTS_DIR" 2>/dev/null | grep -c 'com.apple.quarantine' || true)"; then
    [[ "$REMAINING" == "0" ]] || {
      echo "::error::DMG payload still carries com.apple.quarantine on $REMAINING item(s)." >&2
      exit 1
    }
  fi
fi

rm -f "$DMG_PATH"
hdiutil create -volname "StagePilot" -srcfolder "$CONTENTS_DIR" -ov -format UDZO "$DMG_PATH" -quiet

echo "Injected uninstaller into $DMG_PATH"
