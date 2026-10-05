# Updating StagePilot

The public beta updater is live. `tage-ilot/stagepilot-beta` stays public
indefinitely; its base config and release overlays select anonymous GitHub
`releases/latest/download/latest.json`. No broker or release-download PAT is
in this delivery path. The retained STABLE/BETA settings switch selects the
runtime channel; stable uses the stable repository's public release metadata.
Latest verified for this refresh was `v1.1.104-beta.23`, not a permanent target.
Read latest again when testing. Fresh native update acceptance remains UNPROVEN;
see [the operator checklist](native-completion-runbook.md#step-4--operator-hardware-acceptance-pass).

StagePilot checks after the desktop dashboard is ready and waits about five
seconds so the check never blocks
startup, checks again every six hours, and may check when the app regains focus
after that interval.

This runs only inside a production Tauri desktop build. The browser dashboard,
Vite previews, automated tests, and ordinary development builds do not contact
GitHub. A Tauri development build can opt in with
`VITE_STAGEPILOT_ENABLE_UPDATER=true`.

When the installed version is current—or GitHub cannot be reached—nothing is
shown in the header. When a newer signed release exists, a compact **Update**
button appears immediately to the right of the StagePilot logo. Pressing it
opens a confirmation dialog containing the current version, available version,
and plain-text release notes. No download begins until **Update and Restart** is
pressed.

After confirmation StagePilot records an update marker and safe route, saves
the window state, downloads the platform updater, lets Tauri verify its
signature and install it, and restarts automatically. The relaunch restores,
unminimizes, shows, and focuses the main window. A version-bound marker causes
`StagePilot updated to VERSION` to appear only after a successful update.

If download, signature verification, or installation fails, the installed
version remains open. The dialog offers **Retry** and **Close**. A failed
background check does not make the dashboard unhealthy or show a modal.

## Recovery

An authorized release operator may draft a bad GitHub Release, preserving its
immutable tag/assets. This removes it from eligible public latest releases;
`releases/latest/download/latest.json` follows GitHub's newly selected latest
(or fails if none exists). Read back the selected tag and anonymous manifest;
never assume the previous release is now latest. No broker variable/deploy is
involved. Drafting does not downgrade already-updated clients. Fix forward with
a higher signed version; never reuse a tag or replace a published payload.
If in-app recovery is impossible, install a safe build manually. macOS may
require Privacy & Security approval for the browser-downloaded replacement.
See [rollback](native-completion-runbook.md#step-5--rollback-of-a-live-public-updater).

Tauri updater signatures are not Apple code signatures. Their contents are
embedded in `latest.json` and allow installed StagePilot copies to authenticate
the downloaded updater payload. Removing Hardened Runtime from StagePilot's
ad-hoc macOS application signature does not weaken or disable updater
verification. See [macOS ad-hoc signing](macos-adhoc-signing.md).
