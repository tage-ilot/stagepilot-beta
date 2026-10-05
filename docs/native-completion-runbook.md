# Native completion runbook and PROVEN/DEFERRED ledger

This is the authoritative historical proof ledger and current operator native
acceptance runbook. Releases are ongoing; the updater is live through public
GitHub `latest.json`, without a release broker. Fresh physical install, reboot,
Gatekeeper/SmartScreen, and update acceptance remain UNPROVEN until recorded on
real Windows x64, macOS arm64, and macOS x64 hardware. Neither hosted builds nor
production-use anecdotes substitute for that matrix.

The PROVEN rows below are preserved historical evidence, including historical
broker/config claims, not fresh native acceptance or current deployed-state
claims. New native proof requires actual operator receipts. See
[`private-beta-release-and-acceptance.md`](private-beta-release-and-acceptance.md)
for the delivery decision and
[`private-beta-enrollment-and-guardrails.md`](private-beta-enrollment-and-guardrails.md)
for the guardrail thresholds.

## Runner inventory

| Label | Machine | Status |
|---|---|---|
| `stagepilot-linux` | `stagepilot-ci` (Linux X64) | Registered; offline at this refresh (self-hosted, stays self-hosted) |
| `windows-latest` | GitHub-hosted | Available — public repo, unlimited free minutes on standard runners |
| `macos-15` | GitHub-hosted | Available — public repo, unlimited free minutes on standard runners |
| `macos-15-intel` | GitHub-hosted | Available — public repo, unlimited free minutes on standard runners |

`tage-ilot/stagepilot-beta` stays **public indefinitely**, by operator decision.
Standard hosted Windows/macOS Actions minutes are free for this public repo;
the public release endpoint also permits anonymous in-app updater downloads.
Runner availability is not hardware acceptance. Do not start a stopped runner
or override an operator drain/maintenance hold to run this checklist.

Linux jobs use exactly `runs-on: [self-hosted, stagepilot-linux]` and must
never move to a hosted `ubuntu-*` runner. Native Windows/macOS jobs use
GitHub-hosted `windows-*`/`macos-*` labels. Enforced by parsed YAML in
`scripts/validate_workflow_runners.py`, which runs in CI and fails if a
Linux job drifts to a hosted `ubuntu-*` label, if a native job's `runs-on`
isn't a hosted `windows-*`/`macos-*` label, or if the native job inventory
changes.

## PROVEN on self-hosted Linux

Each row is backed by a CI run on `stagepilot-ci`. Re-read state with `gh`
rather than trusting this table alone.

| # | Capability | Proof |
|---|---|---|
| P1 | Backend, frontend, MultiTracks CLI, and Linux desktop-shell checks (Cargo fmt/check/test) | `ci.yml` jobs `backend`, `frontend`, `multitracks-cues`, `desktop-linux-checks` |
| P2 | Runner boundary: Linux jobs never move to a hosted `ubuntu-*` runner; native job inventory unchanged | `ci.yml:desktop-linux-checks` → `scripts/validate_workflow_runners.py` |
| P3 | `latest.json` generation and schema/date/version validation | `ci.yml:updater-chain` → `scripts/updater_chain_proof.mjs` |
| P4 | Signature embedding taken from the `.sig` sidecar, never invented | same |
| P5 | Missing artifact, missing signature, and empty signature each abort before publication | same |
| P6 | Tag/version agreement; non-HTTPS and query/fragment endpoints refused | same |
| P7 | Beta-vs-main channel isolation in both directions | same |
| P8 | Path-escape and dropped-platform manifests rejected by the validator | same |
| P9 | Updater public key is configured and is not a placeholder; base config keeps the main endpoint while both release overlays select the broker | same |
| P10 | Release-broker allowlisting (versions and exact filenames), foreign-asset-URL rejection, oversized/truncated asset rejection, redirect-hosting confinement, metadata/download cache headers, and independent per-source download rate limits | `ci.yml:updater-chain` → `npm --prefix control-plane test` (25 tests) |
| P11 | Release workflow ordering: signing secrets required, source audit, exact-tag source match, `latest.json` uploaded last, standalone `.sig` never published, immutable tags never clobbered | `ci.yml:updater-chain` → `npm --prefix desktop run release:test` |
| P12 | Control-plane enrollment, isolation, quota, provider-lane, and fail-closed behaviour at unit level | `ci.yml:updater-chain` → `npm --prefix control-plane test` |
| P13 | Zero Cloudflare residue: no `sp-<id>.<suffix>` DNS record and no `stagepilot-<id>-<generation>` tunnel survives an acceptance run | `sweep-control-plane-residue.yml` (report mode) |
| P14 | Release-1 dry run at `main`/`8ce2da9`: `validate_versions.mjs v1.1.103-beta.2`, `audit_beta_release.mjs source` (428 tracked files, zero leaks), `npm --prefix desktop run release:test` (15/15) pass without tagging or publishing | manual local run on this host, see "Release 1 dry run" below |
| P15 | Live enrollment/guardrail acceptance against the deployed Worker: transparent enrollment, idempotent nonce replay, isolated machine credentials, per-installation status quota with `retry-after`, two-tunnel HTTPS isolation, edge HTTPS/WSS abuse limits actually tripped and an unrelated zone host unaffected, zero disposable residue after cleanup | `prepare-control-plane-live-acceptance.yml` run [`35176046446`](https://github.com/tage-ilot/stagepilot-beta/actions/runs/35176046446) on `stagepilot-ci`, commit `4dadbaa`. The developer-network enrollment exemption (`ENROLLMENT_EXEMPT_SOURCES`, see `docs/private-beta-enrollment-and-guardrails.md`) is what removed the 3-per-24h enrollment-source quota as a scheduling constraint on `stagepilot-ci`'s own address. |
| P16 | Native compile/build proof on GitHub-hosted runners, repo now public: Windows x64 unsigned CI installer builds clean (the previously failing "Run packaged Remote lifecycle on Windows" pytest step now passes — the failure was specific to the retired self-hosted Windows setup, not a code defect) and macOS Apple Silicon + macOS Intel Cargo fmt/check/test lifecycle checks pass | `ci.yml` jobs `desktop`, `desktop-macos-lifecycle` on `main`/`3deba13`, run [`35195305370`](https://github.com/tage-ilot/stagepilot-beta/actions/runs/35195305370): `Desktop installer — Windows x64` on `windows-latest` success, `Desktop lifecycle — macOS Apple Silicon` on `macos-15` success, `Desktop lifecycle — macOS Intel` on `macos-15-intel` success; artifact `stagepilot-windows-installer` (54,340,679 bytes) uploaded |
| P17 | Windows x64 installer builds and publishes **signed** (release 1, real tag) | `release-macos.yml:build` (Windows leg, `runs-on: windows-latest`) and `release-macos.yml:publish` (`runs-on: [self-hosted, stagepilot-linux]`) green at tag `v1.1.103-beta.6`, release run [`35229334636`](https://github.com/tage-ilot/stagepilot-beta/actions/runs/35229334636); published asset `StagePilot_1.1.103-beta.6_x64-setup.exe` plus signed `latest.json` entry |
| P18 | macOS arm64/x64 `.app`, `.dmg`, and `.app.tar.gz` build, sign, and publish (release 1, real tag) | `release-macos.yml:build` macOS legs (`macos-15` for Apple Silicon, `macos-15-intel` for Intel) and `release-macos.yml:publish` (`runs-on: [self-hosted, stagepilot-linux]`) green at tag `v1.1.103-beta.6`, release run [`35229334636`](https://github.com/tage-ilot/stagepilot-beta/actions/runs/35229334636); published assets `StagePilot_1.1.103-beta.6_aarch64.dmg`, `StagePilot_1.1.103-beta.6_aarch64.app.tar.gz`, `StagePilot_1.1.103-beta.6_x64.dmg`, `StagePilot_1.1.103-beta.6_x64.app.tar.gz` plus signed `latest.json` entries |

## UNPROVEN native acceptance — exact evidence still required

Never describe any of these as validated. D1 and D2 (real signed Windows
installer and signed macOS bundles) were cleared by release 1: see P17 and
P18 above, both proven at tag `v1.1.103-beta.6`, release run
[`35229334636`](https://github.com/tage-ilot/stagepilot-beta/actions/runs/35229334636).
D3–D8 remain in scope for the beta but need genuine physical hardware and
cannot be proven by CI.

| # | Item | Blocked by | Evidence required to clear it |
|---|---|---|---|
| D3 | Fresh-machine install on each platform | native hardware | `beta_release_acceptance.py installer` + `check --name local_health` receipts |
| D4 | No-auth transparent enrollment from an installed build | native hardware | `check --name transparent_enrollment` and `first_operator` receipts |
| D5 | Viewer/Operator HTTPS + WSS policy from an installed build | native hardware | `check --name https_wss_roles` receipt |
| D6 | App/connector restart and real machine reboot recovery | native hardware | `check --name restart_recovery`, `reboot_recovery` receipts |
| D7 | Disable, re-enable with a new generation, exact provider cleanup | native hardware | `check --name disable_reenable_provider_cleanup` receipt |
| D8 | Gatekeeper / SmartScreen behaviour on unsigned-publisher builds | native hardware | Recorded operator observation per platform |

### Developer-network enrollment exemption (cleared the former D9 scheduling constraint)

Live acceptance runs enroll installations and are subject to the same
production guardrail they verify: **3 new installations per canonical
source IPv4/IPv6-/64 per 24 hours** (`ENROLLMENTS_PER_SOURCE`). Because
`stagepilot-ci` always presents the same one or two developer-network
source addresses, this used to allow at most one full acceptance run per
24 hours and repeatedly stalled iteration on this runbook (see run
`35122372776`, 2026-09-16, exhausted at `enrollments=12`,
`enrollmentDenied` rising).

That scheduling constraint is now removed by an explicit, narrowly scoped
exemption rather than by weakening the guardrail: the optional Worker
variable `ENROLLMENT_EXEMPT_SOURCES` (see
`docs/private-beta-enrollment-and-guardrails.md` for the full mechanism,
security properties, and refresh procedure) lists this development
network's normalized IPv4 and IPv6-/64 sources. An exempt source skips
only the per-source enrollment quota; `ENROLLMENT_ENABLED`, the global
`BETA_INSTALLATION_LIMIT`, `MAX_SOURCE_QUOTAS` pressure, nonce idempotency,
and every downstream status/mutation/provider quota still apply in full,
and exempt enrollments still count toward `enrollments`/`activeInstallations`
so capacity stays observable. `ENROLLMENTS_PER_SOURCE` itself was not
changed. This must never list a beta user's address — only trusted
developer networks.

If this network's ISP-assigned addresses change, refresh the exemption:
read `https://cloudflare.com/cdn-cgi/trace` and
`curl -4 https://cloudflare.com/cdn-cgi/trace`, normalize the IPv6 address
to its `/64`, then `gh variable set ENROLLMENT_EXEMPT_SOURCES --repo
tage-ilot/stagepilot-beta --env stagepilot-control-plane` with the updated
comma-separated list, and redeploy via `deploy-control-plane.yml`.

## Recovering a stranded disposable installation

The Worker's admin surface is deliberately aggregate-only and exposes no route
that enumerates installations, so a stranded installation ID cannot be listed
after the fact. The acceptance script therefore prints
`CLEANUP_RECEIPTS [...]` and, on failure, `STRANDED_INSTALLATIONS [...]` with
exact IDs.

1. Revoke each exact ID from the run log:

   ```sh
   gh workflow run revoke-control-plane-live-installation.yml \
     --repo tage-ilot/stagepilot-beta --ref main -f 'installation=EXACT_DISPOSABLE_32_HEX_ID'
   ```

   Replace the placeholder only with an ID from this disposable run, never a
   production ID. The workflow rejects the unsubstituted placeholder.

2. Confirm no provider residue remains (this is the residue that costs money,
   holds DNS, or stays reachable):

   ```sh
   gh workflow run sweep-control-plane-residue.yml --repo tage-ilot/stagepilot-beta \
     --ref main -f apply=report
   ```

3. If residue remains, retain the exact disposable IDs and request scoped
   admin recovery. Do not use the sweep's global apply mode for hardware
   cleanup: it has no exact-installation filter. Never target a production
   installation. Require the revocation and report run read-backs before
   recording cleanup as passed; queued workflows are not cleanup proof.

### Known unrecoverable registry entries — historical 2 stranded, zero provider residue

The first acceptance attempt of 2026-09-16 (run `35119782474`, at commit
`33a39fe`, before the `badb3a4` fix) enrolled two installations and then died
on a TLS `unrecognized name` error during hostname activation. The script at
that commit appended to `installations` only *after* its assertions, so the
`finally` block had nothing to revoke and printed no receipts. Their exact IDs
were never emitted and the Worker exposes no enumeration route, so **they
cannot be recovered or revoked**. They are the persistent
`activeInstallations: 2` in the historical readings documented here. This
refresh did not query live installation counters or mutate provider state.

The recorded evidence established a bounded, zero-provider-residue baseline:

- Provider residue is **zero** — confirmed by direct Cloudflare reads at 16:41Z
  (run `35123422067`) and 17:02Z (run `35125995391`), both
  `disposableHostnames: []` and `disposableTunnels: []`. No DNS record is held,
  no tunnel is reachable, nothing bills.
- Both entries are `phase: disabled` with no provisioned generation; a stranded
  entry that never completed provisioning holds no provider object.
- `BETA_INSTALLATION_LIMIT` defaults to 500, so 2 entries do not approach the
  global enrollment ceiling.

`badb3a4` fixed the cause: enrollments are now registered for cleanup the
instant they succeed, revocation is retried, and IDs are printed
unconditionally, so no future run can strand an installation invisibly. Treat
the residual 2 as a permanent, harmless baseline offset — the residue check
asserts `activeInstallations` is **not greater than** its baseline for exactly
this reason, rather than asserting zero.

## Current delivery and remaining update proof

The latest non-draft release verified for this refresh is `v1.1.104-beta.23`.
The live anonymous GitHub manifest reports `1.1.104-beta.23`, all three platform
entries, non-empty signatures, and direct GitHub asset URLs. This is metadata
evidence, not new cryptographic or physical-install proof. Read latest again
at acceptance time; do not treat this snapshot as a permanent target.

Base Tauri config and both release overlays select the public beta endpoint.
The retained STABLE/BETA settings switch selects the channel at runtime; do not
remove it. A release broker or `STAGEPILOT_RELEASE_TOKEN` is not required for
this delivery path. Historical broker unit proofs below/above remain history,
not descriptions of the current deployed updater path.

`updater_discovery`, `updater_install_relaunch`, and two-version verification
are required native acceptance, not operator-deferred features. Use an earlier
compatible signed beta as the source and the current latest as the target.
Clients carrying retired signing keys need a manual bootstrap installer first.

## Bounded packaging smoke-test finding — blocked, do not pursue further

Investigated whether `stagepilot-ci` can run
`npm --prefix desktop run build:sidecar` (frontend build + backend
PyInstaller into `desktop/src-tauri/binaries/`), the chain shared with every
native build, so packaging regressions could surface before native hardware
arrives.

**Blocked.** `npm --prefix desktop run build:sidecar` fails on `stagepilot-ci`
at the PyInstaller step:

```
ERROR: Python shared library ('libpython3.12.so.1.0') was not found! If you
are using system python on Debian/Ubuntu, you might need to install a
separate package by running `apt install libpython3.12`.
```

This is a runner-environment gap, not a StagePilot code defect: `uv run
--isolated ... pyinstaller` builds an isolated Python 3.12 environment per
the project's `--extra packaging` spec, and PyInstaller's `--onefile`
bootloader needs to link the interpreter's shared library at build time. The
isolated `uv` build environment does not expose a linkable
`libpython3.12.so.1.0` to PyInstaller, even though a system Python 3.12 is
present on the runner (confirmed by the earlier `ci.yml` jobs using it
successfully for lint/mypy/pytest). Fixing it durably needs either a runner
package install (e.g. `libpython3-dev`) or reconfiguring `uv`'s managed
Python to link dynamically — both a runner-host change beyond what a normal
CI job does. Per the card's timebox, this was not pursued further: no system
package was installed, no runner host state was changed, and no Linux bundle
target was added.

Per-command results:

| Command | Result |
|---|---|
| `npm --prefix frontend run build` (part of `build:sidecar`) | Passes |
| `uv run --isolated --project backend --extra packaging --locked pyinstaller ...` (rest of `build:sidecar`, invoked by `scripts/build_backend_sidecar.py`) | **Fails**: `libpython3.12.so.1.0` not found in the isolated build env |
| `python scripts/stage_cloudflared.py` (the other half of `build:runtime`) | Exits non-zero by design — `linux x86_64` has no entry in `ASSETS`; StagePilot does not ship a Linux bundle, so this is expected and out of scope |

No CI job was added for this chain because it does not currently pass on
`stagepilot-ci`. Re-attempt once the runner host's `uv`-managed Python 3.12
toolchain includes (or is rebuilt with) a linkable shared library — that is
an operator/runner-provisioning action, not a code change.

---

# Native completion runbook

## Step 1 — Read runner state, do not change policy

Native build jobs are already enabled on hosted Windows/macOS runners. Linux
jobs stay self-hosted. No workflow edit or runner restart is part of acceptance.

```sh
gh api repos/tage-ilot/stagepilot-beta/actions/runners --jq '.runners[] | {name,status}'
uv run --with PyYAML==6.0.3 python scripts/validate_workflow_runners.py
```

If Linux jobs queue, wait for separately authorized runner recovery; do not
bypass a drain or maintenance hold.

## Step 2 — Signing prerequisites

Release signing uses repository Actions secrets `TAURI_SIGNING_PRIVATE_KEY`
and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. Inspect names only with
`gh secret list --repo tage-ilot/stagepilot-beta`; never print values.
No release-download PAT or broker allowlist is needed by the live updater.
Do not rotate keys or publish releases during a hardware acceptance pass.

## Step 3 — Current release and historical release 1

Release 1 (`v1.1.103-beta.6`) remains historical proof, not today's beta:
https://github.com/tage-ilot/stagepilot-beta/releases/tag/v1.1.103-beta.6
from `e7af35a7c2e9e0bd53cec90eacb77c0511699058`, release run
https://github.com/tage-ilot/stagepilot-beta/actions/runs/35229334636 and CI
https://github.com/tage-ilot/stagepilot-beta/actions/runs/35227555657.
The abandoned `v1.1.103-beta.1`–`v1.1.103-beta.5` tags remain unmoved history;
never reuse them. New publication follows `releasing-stagepilot.md`, using a
new version, lockfile validation and exact-head CI, not the old release-1 tag.
The latest release at this refresh is `v1.1.104-beta.23`; dynamically resolve
it again below. The public GitHub manifest is actively consumed by the updater.

## Step 4 — Operator hardware acceptance pass

1. Use disposable, isolated OS accounts/machines with no production StagePilot
   installation, Remote identity, sessions, or provider objects. Run on Windows
   x64 and both Apple Silicon and Intel Macs (macOS 12+). One Mac architecture
   cannot clear the other. Use a reviewed checkout for the receipt script, not
   a development backend as a substitute for the installed application.
   Keep private evidence outside Git; never put secrets or personal data in it.

2. Read current latest and its actual assets. In PowerShell on Windows:

   ```powershell
   $Repo = "tage-ilot/stagepilot-beta"
   $Tag = gh release view --repo $Repo --json tagName -q .tagName
   $Version = $Tag -replace '^v', ''
   $Platform = "windows-x86_64"
   $Asset = "StagePilot_${Version}_x64-setup.exe"
   $Work = Join-Path $env:USERPROFILE "StagePilot-disposable-acceptance"
   New-Item -ItemType Directory -Force $Work | Out-Null
   $Report = Join-Path $Work "native-acceptance.json"
   gh release view $Tag --repo $Repo --json tagName,isDraft,assets
   gh release download $Tag --repo $Repo --pattern $Asset --dir $Work
   python scripts/beta_release_acceptance.py --report $Report installer --platform $Platform --version $Version --file (Join-Path $Work $Asset)
   ```

   On a Mac, use this shell block; `uname -m` selects the correct installer:

   ```sh
   REPO=tage-ilot/stagepilot-beta
   TAG=$(gh release view --repo "$REPO" --json tagName -q .tagName)
   VERSION=${TAG#v}
   case "$(uname -m)" in
     arm64) PLATFORM=darwin-aarch64; ARCH=aarch64 ;;
     x86_64) PLATFORM=darwin-x86_64; ARCH=x64 ;;
     *) printf 'Unsupported Mac architecture\n'; exit 1 ;;
   esac
   ASSET="StagePilot_${VERSION}_${ARCH}.dmg"
   WORK="$HOME/StagePilot-disposable-acceptance"
   mkdir -p "$WORK"; chmod 700 "$WORK"
   REPORT="$WORK/native-acceptance.json"
   gh release view "$TAG" --repo "$REPO" --json tagName,isDraft,assets
   gh release download "$TAG" --repo "$REPO" --pattern "$ASSET" --dir "$WORK"
   python3 scripts/beta_release_acceptance.py --report "$REPORT" installer --platform "$PLATFORM" --version "$VERSION" --file "$WORK/$ASSET"
   ```

   Check non-draft status and exactly these six asset names against the returned
   inventory (VERSION is the dynamically read version):
   `StagePilot_VERSION_x64-setup.exe`, `StagePilot_VERSION_aarch64.dmg`,
   `StagePilot_VERSION_x64.dmg`, `StagePilot_VERSION_aarch64.app.tar.gz`,
   `StagePilot_VERSION_x64.app.tar.gz`, and `latest.json`. DMGs/installers are
   human downloads; `.app.tar.gz` files are macOS updater payloads. No standalone
   `.sig` is published. Read the anonymous manifest with:

   ```sh
   curl -fsSL https://github.com/tage-ilot/stagepilot-beta/releases/latest/download/latest.json
   ```

   Its version, three platform URLs and signatures must match the frozen target.
   If latest changes during acceptance, stop and reconcile the target before
   recording update receipts. Do not silently mix releases in one report.

3. Install the target through the native GUI on the fresh account. Record actual
   Gatekeeper/SmartScreen prompts and approval steps in `$Work`/`$WORK` as a
   private operator observation (D8 has no dedicated harness check). Tauri
   updater signatures do not mean Apple notarization or Windows publisher trust.
   Verify local dashboard/health (D3), transparent no-account enrollment and
   exactly one first Operator (D4), and Viewer/Operator HTTPS/WSS, CSRF/session
   policy (D5). Observe app/connector restart and actual OS reboot recovery
   (D6); restore the same variable values after reboot. Disable and re-enable
   Remote, observe a new generation and read back cleanup of the previous exact
   disposable DNS/tunnel (D7). Do not test against a production installation.

4. Record each observation only after it actually passes. These are receipt
   commands, not automated hardware probes. For each CHECK below, substitute
   a short factual receipt in EVIDENCE (maximum 256 characters, no secrets).

   ```powershell
   $Check = "local_health"
   $Evidence = Read-Host "Short secret-free observed receipt"
   python scripts/beta_release_acceptance.py --report $Report check --platform $Platform --name $Check --evidence $Evidence
   ```

   ```sh
   CHECK=local_health
   printf 'Short secret-free observed receipt: '; read -r EVIDENCE
   python3 scripts/beta_release_acceptance.py --report "$REPORT" check --platform "$PLATFORM" --name "$CHECK" --evidence "$EVIDENCE"
   ```

   Repeat for `transparent_enrollment`, `first_operator`, `https_wss_roles`,
   `restart_recovery`, `reboot_recovery`, and
   `disable_reenable_provider_cleanup`; do not bulk-mark unchecked items.

5. On the same disposable accounts, test a genuine earlier-to-latest update.
   Select an earlier published compatible signed beta as FROM_TAG (not an
   abandoned tag or retired-key build); confirm its release and assets with
   `gh release view FROM_TAG --repo tage-ilot/stagepilot-beta --json tagName,isDraft,assets`.
   Download and record the source installer (enter the reviewed source tag;
   the target variables from step 2 remain unchanged):

   ```powershell
   $FromTag = Read-Host "Reviewed compatible earlier published beta tag"
   $FromVersion = $FromTag -replace '^v', ''
   $FromAsset = "StagePilot_${FromVersion}_x64-setup.exe"
   gh release download $FromTag --repo $Repo --pattern $FromAsset --dir $Work
   python scripts/beta_release_acceptance.py --report $Report installer --platform $Platform --version $FromVersion --file (Join-Path $Work $FromAsset)
   ```

   ```sh
   printf 'Reviewed compatible earlier published beta tag: '; read -r FROM_TAG
   FROM_VERSION=${FROM_TAG#v}
   FROM_ASSET="StagePilot_${FROM_VERSION}_${ARCH}.dmg"
   gh release download "$FROM_TAG" --repo "$REPO" --pattern "$FROM_ASSET" --dir "$WORK"
   python3 scripts/beta_release_acceptance.py --report "$REPORT" installer --platform "$PLATFORM" --version "$FROM_VERSION" --file "$WORK/$FROM_ASSET"
   ```

   Manually install
   the source build in this disposable environment, verify its version, keep
   the settings channel BETA, then observe discovery of the frozen target.
   Cancel once (no download), accept Update and Restart, and observe signature
   verification, install, relaunch, window recovery, version read-back and
   success message. Record `updater_discovery` and `updater_install_relaunch`
   with the commands in step 4. If no compatible newer target exists, these
   checks remain UNPROVEN; reinstalling latest is not an update test.

6. Disable Remote in every disposable app first. Save the exact 32-hex
   installation ID from this test's own receipts privately; never substitute
   a production ID. Revoke each exact disposable ID from an authorized admin
   shell. In PowerShell:

   ```powershell
   $DisposableId = Read-Host "Exact 32-hex ID from this disposable test"
   if ($DisposableId -cnotmatch '^[a-f0-9]{32}$') { throw "Invalid installation ID" }
   gh workflow run revoke-control-plane-live-installation.yml --repo tage-ilot/stagepilot-beta --ref main -f "installation=$DisposableId"
   gh run list --repo tage-ilot/stagepilot-beta --workflow revoke-control-plane-live-installation.yml --limit 5
   ```

   In a POSIX shell:

   ```sh
   printf 'Exact 32-hex ID from this disposable test: '; read -r DISPOSABLE_ID
   printf '%s' "$DISPOSABLE_ID" | python3 -c 'import re,sys; sys.exit(0 if re.fullmatch("[a-f0-9]{32}",sys.stdin.read()) else 2)' &&
   gh workflow run revoke-control-plane-live-installation.yml --repo tage-ilot/stagepilot-beta --ref main -f "installation=$DISPOSABLE_ID"
   gh run list --repo tage-ilot/stagepilot-beta --workflow revoke-control-plane-live-installation.yml --limit 5
   ```

   Identify the exact dispatch run, then `gh run watch RUN_ID --repo
   tage-ilot/stagepilot-beta --exit-status` and `gh run view RUN_ID --repo
   tage-ilot/stagepilot-beta --log`. Require its exact-ID disabled/revoked
   read-back. Dispatch a report-only provider sweep:

   ```sh
   gh workflow run sweep-control-plane-residue.yml --repo tage-ilot/stagepilot-beta --ref main -f apply=report
   gh run list --repo tage-ilot/stagepilot-beta --workflow sweep-control-plane-residue.yml --limit 5
   gh run watch RUN_ID --repo tage-ilot/stagepilot-beta --exit-status
   gh run view RUN_ID --repo tage-ilot/stagepilot-beta --log
   ```

   Replace RUN_ID with the exact new report run. Do not run `apply=apply`:
   it has no exact-ID filter and is not safe generic hardware cleanup. If
   residue remains, retain evidence and request scoped admin recovery, not
   a global sweep. Queued cleanup is not successful cleanup; wait for separately
   authorized runner recovery. Preserve the documented two stranded registry
   entries as a historical limitation, not as proof of today's counters.
   Remove only this disposable account's test sessions/users, local machine
   identity/OS credential-store entries, app and installer files through the
   app/OS uninstall interfaces. Never delete a shared data directory or revoke
   production credentials. Inspect before removing private evidence under policy.
   Record `final_cleanup` only after local cleanup and provider read-back.

7. Reports land at `%USERPROFILE%\StagePilot-disposable-acceptance\native-acceptance.json`
   on Windows and `$HOME/StagePilot-disposable-acceptance/native-acceptance.json`
   on each Mac. They contain per-platform installers and observed checks, not
   automatically verified hardware facts. On a private review machine, combine
   the three `platforms` records into one schema-1 report, preserving exactly
   the same source/target pair and only those two installer versions. Inspect
   for secrets before any sharing. Run the final full-matrix gate:

   ```sh
   python3 scripts/beta_release_acceptance.py --report PRIVATE_COMBINED_REPORT verify --from-version FROM_VERSION --to-version TO_VERSION
   ```

   FROM_VERSION and TO_VERSION omit the leading `v`. `verify` requires both
   installer receipts and all ten checks on all three platforms; it does not
   record D8 or replace its separate observations. Missing hardware, reboot,
   publisher-trust observation, or updater proof keeps acceptance UNPROVEN.

## Step 5 — Rollback of a live public updater

Only an authorized release operator may unpublish a bad release:

```sh
gh release edit BAD_TAG --repo tage-ilot/stagepilot-beta --draft
```

Keep the tag/assets for audit; never move/reuse the tag or replace signed
payloads in place. Drafting removes it from eligible public latest releases,
so `releases/latest/download/latest.json` resolves to the release GitHub now
selects as latest (or fails if none is eligible). It does not downgrade already
updated clients. Read back both `gh release view --repo tage-ilot/stagepilot-beta
--json tagName,isDraft` and the anonymous manifest URL; never assume the
previous release was selected. Fix forward with a higher signed version;
manually install a safe build if in-app recovery is impossible. No broker
variable/deploy is part of this rollback; Remote identities remain untouched.
