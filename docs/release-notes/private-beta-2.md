# Private beta update acceptance — historical template

The former one-release/disabled-updater decision is obsolete. Releases are
ongoing; the latest non-draft release verified at this refresh is
`v1.1.104-beta.23`. Resolve current latest again rather than treating that
snapshot or historical `v1.1.103-beta.6` as a permanent target.

The repository stays public indefinitely. The live updater consumes anonymous
GitHub `latest.json` directly, with Tauri signature verification, no broker or
release-download PAT. The STABLE/BETA settings switch is retained.

For actual release notes, describe the focused changes for that exact tag and
attach its inventory from `node scripts/audit_beta_release.mjs assets
RELEASE_ASSETS VERSION` (VERSION omits `v`). Never publish another release just
to complete this docs checklist.

Native acceptance is UNPROVEN until observed on Windows x64 and both Mac
architectures. Discovery, signature verification, installation and relaunch
from a compatible earlier signed beta to current latest are required, not
deferred. Use the single [Operator hardware acceptance pass](../native-completion-runbook.md#step-4--operator-hardware-acceptance-pass)
for exact `beta_release_acceptance.py` commands, report locations, D3–D8 and
disposable-only cleanup. Never include passwords, credentials, cookies, tokens,
or private machine evidence in release notes.
