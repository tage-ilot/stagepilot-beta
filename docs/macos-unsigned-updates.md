# Unsigned and ad-hoc-signed macOS updates

StagePilot is not notarized with an Apple Developer account. macOS application
bundles are ad-hoc signed with identity `-`, while Tauri updater archives are
independently signed with the long-term StagePilot updater key.

Ad-hoc signing does not establish an Apple-verified developer identity. The
mandatory Tauri signature proves an update was produced with StagePilot's
private updater key, but it is not Apple notarization.

The first updater-enabled release must be downloaded manually, moved to
`/Applications/StagePilot.app`, and approved through Privacy & Security if
macOS blocks it. Later releases are intended to be downloaded inside
StagePilot, avoiding the usual browser-download path that adds quarantine
metadata. Apple does not guarantee prompt-free updating for unsigned apps on
every macOS version.

Manually downloading a replacement may add `com.apple.quarantine` again.
StagePilot never disables Gatekeeper, invokes AppleScript, or accepts an
updater artifact whose Tauri signature fails.

## App Translocation (why in-app updates could get stuck)

Gatekeeper runs a quarantined, non-notarized `.app` from a randomized,
**read-only** image instead of its real location:

```text
/private/var/folders/<…>/T/AppTranslocation/<uuid>/d/StagePilot.app
```

Finder still shows the app as if it were in `/Applications`. Because the running
bundle path is read-only, any in-place update install fails — after the download
has completed and the managed backend has already been stopped for the install,
which surfaced as a confusing "UPDATE INTERRUPTED … you are still on the old
version". Translocation happens only while the bundle carries
`com.apple.quarantine`; it is inherent to an ad-hoc-signed app that cannot be
notarized.

StagePilot therefore:

1. Detects translocation at runtime (`install_environment` command, backed by
   `desktop/src-tauri/src/macos_install.rs`) by checking whether the resolved
   executable path contains `/AppTranslocation/`. When it does, the updater
   refuses to start an install that cannot land and shows the exact fix instead
   of a generic error. `prepare_for_update` enforces the same guard server-side,
   so the backend is never stopped for a doomed install.
2. Clears `com.apple.quarantine` **on its own bundle only**, at startup, when it
   is not translocated and the bundle is writable by the current user (no
   elevation needed for a personal `/Applications`). This is the narrowest
   possible scope: it removes the trigger for the *next* launch, it never
   touches other applications, never disables Gatekeeper, and never weakens
   updater signature verification — the app has already been admitted by
   Gatekeeper at that point, so no security check is bypassed. This supersedes
   the earlier blanket "never clears quarantine" stance, which predated the
   discovery that quarantine-driven translocation silently breaks in-app
   updates.
3. Ships an uninstaller that ejects stale `/Volumes/StagePilot*` DMG mounts,
   removes stale Trash/translocation copies, and rebuilds the Launch Services
   database, so macOS cannot resolve a later launch to an old registered copy.

### Diagnosing a stuck install

```bash
ps aux | grep -i stagepilot            # a /AppTranslocation/ path means translocated
xattr -p com.apple.quarantine /Applications/StagePilot.app
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -dump | grep -i stagepilot.app       # stale registered paths
```

Manual recovery (what the in-app guidance tells the user, minus the Finder
path): quit StagePilot, `xattr -cr /Applications/StagePilot.app`, eject every
mounted `StagePilot*` volume, then relaunch from `/Applications`.

## Diagnostics

Before and after an in-app update, record:

```bash
xattr -l /Applications/StagePilot.app
codesign --display --verbose=4 /Applications/StagePilot.app
codesign --verify --deep --strict --verbose=2 /Applications/StagePilot.app
```

Do not remove quarantine during validation. Record the macOS version,
architecture, updater archive type, signing result, download path, and any
Gatekeeper prompt. A prompt must be documented rather than hidden.
