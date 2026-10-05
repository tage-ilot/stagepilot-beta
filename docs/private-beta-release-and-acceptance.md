# Private beta release and native acceptance plan

## Current delivery decision

`tage-ilot/stagepilot-beta` is public indefinitely by operator decision.
The updater is live: base Tauri config and both release overlays select
`https://github.com/tage-ilot/stagepilot-beta/releases/latest/download/latest.json`.
The retained STABLE/BETA settings switch selects the runtime channel. No release
broker or GitHub release-download PAT is needed; never embed one in the app.

Fresh remote reads for this refresh returned non-draft `v1.1.104-beta.23`, six
published assets, and an anonymous manifest for `1.1.104-beta.23` with three
platform entries, non-empty signatures and direct GitHub URLs. Resolve latest
again at acceptance time. Native install/reboot/publisher-trust/update acceptance
is still UNPROVEN; metadata, CI and production anecdotes cannot clear it.

The former private-repository broker design remains historical implementation,
not the current delivery route. Public GitHub delivery preserves Tauri signature
verification and does not relay Remote traffic. See the single
[Operator hardware acceptance pass](native-completion-runbook.md#step-4--operator-hardware-acceptance-pass)
for current commands and safe disposable-only cleanup.

## CI runner boundary

Linux jobs in `.github/workflows/*.yml` use exactly
`runs-on: [self-hosted, stagepilot-linux]`. This includes backend, frontend,
MultiTracks, Linux-compatible Cargo/Tauri checks, release source validation,
release manifest/publication logic, control-plane verification/deployment, live
transparent-enrollment acceptance, and revocation. The normal bootstrap actions
`actions/checkout`, `actions/setup-node`, and `astral-sh/setup-uv` remain allowed
on that runner.

Windows x64 packaging and macOS arm64/x64 packaging/lifecycle jobs now run on
GitHub-hosted `windows-latest`, `macos-15`, and `macos-15-intel` runners: the
repository stays public indefinitely, so hosted Actions minutes on standard
runners are free and unlimited, removing the need for native self-hosted
machines. See [`native-completion-runbook.md`](native-completion-runbook.md)
for the current PROVEN/DEFERRED status of the native build/sign/publish path.

Validate this boundary with a YAML parser before enabling repository Actions:

```sh
python3 scripts/validate_workflow_runners.py
```

The initial activation was proven by self-hosted CI run
`35029964844`: all four active Linux jobs completed on `stagepilot-ci`, while
the preserved Windows and macOS jobs skipped without acquiring a runner.

## Deterministic versions and assets

The earlier failed release attempts already occupy immutable tags
`v1.1.103-beta.1` (bootstrap failure), `v1.1.103-beta.2` (failed on a
cross-platform mypy defect and an unusable signing key), `v1.1.103-beta.3`
(failed because `backend/uv.lock` recorded a stale project version, so every
`uv sync --locked` step aborted), `v1.1.103-beta.4` (failed on five
Windows-only backend test failures that only the release job's full pytest run
exercises) and `v1.1.103-beta.5` (all three signed builds succeeded, but the
publish step called the `gh` CLI, which is not installed on the self-hosted
Linux runner). **Historical release 1 was `v1.1.103-beta.6`**; many releases
have followed. Read latest dynamically and never move or reuse published tags.

Each release staging directory must contain exactly:

- `StagePilot_VERSION_aarch64.dmg`
- `StagePilot_VERSION_x64.dmg`
- `StagePilot_VERSION_aarch64.app.tar.gz` and `.sig`
- `StagePilot_VERSION_x64.app.tar.gz` and `.sig`
- `StagePilot_VERSION_x64-setup.exe` and `.sig`
- `latest.json`
- `release-notes.md`

Run `node scripts/audit_beta_release.mjs source` before release and `node scripts/audit_beta_release.mjs assets RELEASE_ASSETS VERSION` after building. Preserve the JSON output as the asset-size/SHA-256 inventory. Standalone `.sig` files are staging inputs and remain omitted from user-facing GitHub assets because their contents are embedded in `latest.json`.

## Release 1 — shipped

**`v1.1.103-beta.6` is published.**

| | |
|---|---|
| Release URL | https://github.com/tage-ilot/stagepilot-beta/releases/tag/v1.1.103-beta.6 |
| Release ID | `390773788` |
| Tag commit | `e7af35a7c2e9e0bd53cec90eacb77c0511699058` |
| Published | 2026-09-17T14:09:10Z |
| Build/publish run | https://github.com/tage-ilot/stagepilot-beta/actions/runs/35229334636 |
| Exact-head CI | https://github.com/tage-ilot/stagepilot-beta/actions/runs/35227555657 (8/8 green) |
| Updater key id | `9DAF99548D6D7D77` |

Published asset inventory, SHA-256 (standalone `.sig` files are intentionally
absent; their contents are embedded in `latest.json`):

| Asset | Bytes | SHA-256 |
|---|---|---|
| `StagePilot_1.1.103-beta.6_x64-setup.exe` | 54,334,610 | `49253fe72f5180a5ddf4072c3dc7396ee04c3ec3f4a42ea4d5a0e6f574a92fc1` |
| `StagePilot_1.1.103-beta.6_aarch64.dmg` | 61,582,282 | `6ada3997ccafce0e16e3379747767363fc16a8679dc5801fd8b79355a4faadba` |
| `StagePilot_1.1.103-beta.6_x64.dmg` | 64,718,555 | `b7ce0ecaf75ceb0fed302a9dcf87a740dac13a3300417d3f6e76dac17d83643a` |
| `StagePilot_1.1.103-beta.6_aarch64.app.tar.gz` | 61,504,698 | `855926c9aa08cb1aea6931defe13966aed76f497fe0964dde1e73b07db45e949` |
| `StagePilot_1.1.103-beta.6_x64.app.tar.gz` | 64,527,503 | `c760790e2e92fd2ce84b187d2b41f1b5953e02bafeb577b669c773fb4c9412e3` |
| `latest.json` | 3,623 | `9d05519d8e5346cba38b1b543ca4f0f57d20fcdd5541a01cc963d8b941585dad` |

Verified after publication by unauthenticated download (no token, exactly as a
tester or an installed client would fetch): every asset returned HTTP 200, and
each of the three updater signatures in `latest.json` verifies against the
public key embedded in `desktop/src-tauri/tauri.conf.json`, while a tampered
artifact is rejected. `tage-ilot/stagepilot` was confirmed unchanged at
`f58f91ee320e13d956ff97473e83df4c23199653`, still on v1.1.102.

## Friend download instructions

No invitation or GitHub account is needed. Resolve the latest published tag
with `gh release view --repo tage-ilot/stagepilot-beta --json tagName -q .tagName`
and send its immutable release URL, not the historical release-1 inventory.
After removing the tag's leading `v`, match VERSION to:

- Windows x64: `StagePilot_VERSION_x64-setup.exe`
- macOS Apple Silicon: `StagePilot_VERSION_aarch64.dmg`
- macOS Intel: `StagePilot_VERSION_x64.dmg`

The `.app.tar.gz` files are updater payloads, not manual installer downloads.
Verify each download against the inventory for that exact release, never a
historical hash. Installed compatible-key builds can use Update and Restart;
retired-key builds need a manual bootstrap. Testers never need credentials
inside StagePilot. Do not send signing material or private machine evidence.

## Signing recovery and rollback

The updater signing key was regenerated **twice** on 2026-09-17, both times
before any release published an asset, so no installed client ever trusted a
retired public key.

1. The original `TAURI_SIGNING_PRIVATE_KEY` was an RSA PEM keypair, not a
   Tauri/minisign key, so `tauri build` could never sign with it (`failed to
   decode base64 secret key`). That material is retained, unused, under
   `~/.tauri/legacy-rsa-unusable/`.
2. Its minisign replacement (key id `ED8D8193966AB83F`) was **exposed and is
   permanently retired**. `tauri signer --help` prints the resolved value of
   every option that has an `env:` binding, so invoking it while
   `TAURI_SIGNING_PRIVATE_KEY` / `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` were
   exported echoed both the encrypted private key and its password into the
   terminal transcript. The material is archived, unused, under
   `~/.tauri/exposed-2026-09-17/`.

The current key (id `9DAF99548D6D7D77`) is a real `rsign`/minisign Ed25519
keypair generated with the password read from disk and never placed in the
environment. Its public half is embedded in
`desktop/src-tauri/tauri.conf.json`, and it was proven to sign a disposable
payload, verify against that embedded public key, and reject a tampered payload
before the repository secrets were rotated to match.

### 2026-09-17: rotated to a shared main/beta signing key (key id `E8F63599150A54AC`)

To let a single installed main-app public key verify both channels, the
operator authorized generating a **new shared** minisign keypair and setting
it as `TAURI_SIGNING_PRIVATE_KEY` / `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` in
both `tage-ilot/stagepilot` and `tage-ilot/stagepilot-beta` Actions secrets
(confirmed by read-back 2026-09-17T20:40Z), replacing the beta-only
`9DAF99548D6D7D77` pair above. `desktop/src-tauri/tauri.conf.json` `pubkey`
was updated to the new shared key
(`RWSsVAoVmTX26LIj3tWtVLLY/iE7DOfeg6owbzgyrNf8du7yJ0Ru3tva`), and a new beta
release (`v1.1.103-beta.7`) was cut so its artifacts are signed with it.

**Consequence:** `v1.1.103-beta.1` through `v1.1.103-beta.6`, and stable
`v1.1.102` and earlier, were signed with the retired keys and will **not**
verify against a client carrying the new shared public key. Those installs
do not auto-update onto anything built after this rotation; affected users
must install the new build manually once. Every release cut after
2026-09-17T20:40Z verifies normally in both channels. No private key
material was generated, read, or committed by this task; the private key
lives only in GitHub Actions secrets.

> **Never run `tauri signer --help` (or any `tauri signer` subcommand with
> `--help`) while the signing environment variables are exported.** The CLI
> renders secret values inline in its help text. Pass the key by path with `-f`
> and the password by `-p "$(cat …)"`, and keep the variables out of the
> environment entirely.

Before each release, verify without printing values that GitHub contains `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, the configured updater public key is not a placeholder, and two independently recoverable encrypted offline copies of the private key/password exist. On an isolated local copy, sign a disposable file with the recovered key and verify it with the configured public key using the pinned Tauri CLI. Delete the disposable file. Never rotate the updater key between beta releases; installed clients trust only the embedded public key.

> **Outstanding operator action:** the current private key and its password live
> only at `~/.tauri/stagepilot-updater.key` and
> `~/.tauri/stagepilot-updater.password` on the release host. Take two
> independently recoverable encrypted offline backups before distributing
> release 1 widely.

If the signing key is lost, no future build can be accepted by an installed
updater-enabled copy. Recovery is then a new key plus a fresh installer
download by every tester — which is why two independently recoverable offline
copies are required before release 1, not after.

## Rollback and native acceptance

The updater actively consumes public GitHub latest metadata. An authorized
operator can draft a bad release while preserving its immutable tag/assets;
this removes it from eligible public latest releases. Read back both GitHub's
selected latest tag and the anonymous manifest, which may select another
eligible release or fail if none exists. This does not downgrade installed
clients: fix forward with a higher signed version or manually recover.
No broker variable or redeploy is involved; Remote identities are unaffected.
See [rollback commands](native-completion-runbook.md#step-5--rollback-of-a-live-public-updater).

Fresh Windows x64, macOS arm64 and macOS x64 acceptance is UNPROVEN, including
real reboot and Gatekeeper/SmartScreen observation. `updater_discovery` and
`updater_install_relaunch` are required, not deferred. Follow the single
[Operator hardware acceptance pass](native-completion-runbook.md#step-4--operator-hardware-acceptance-pass)
for exact installer/check/verify CLI, two-version receipts, report locations,
and exact disposable-install revocation with provider read-back. No CI build,
mock or production-use anecdote replaces physical proof. Historical PROVEN
rows and CI URLs in that runbook remain historical evidence.
