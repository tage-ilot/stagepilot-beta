# Remote deployment acceptance

## Live verification — 2026-09-28 (agent-hub host)

Read-only health/status checks against the currently running managed
installation, plus systemd checks. No credentials were requested, read, or
changed; MIDI devices were enumerated only — no signal was sent.

Planning Center OAuth: `GET /api/v1/planning-center/status` (authenticated
via existing dashboard PIN) returned
`{"connection_status":"disconnected","configured":false,"app_id":null,"service_type_id":null,"planning_center_secret_saved":false,"detail":"Demo service loaded."}`.
This host's installed instance runs in `service_source=demo`/`midi_source=simulated`
mode (see `settings.integration_modes`), not against the live jpl-graphics
Planning Center org — no PCO app ID/secret is configured on this box, so
there is nothing to authenticate. This confirms the *status endpoint itself*
works and reports honestly; it does not confirm a live PCO OAuth session
because none is provisioned here. Live PCO health must be re-run once
`STAGEPILOT_PCO_APP_ID`/`STAGEPILOT_PCO_SECRET` are set for the production
org (see "Production hardening implementation" below for exact variables).

ProPresenter: `GET /api/v1/propresenter` returned
`{"enabled":false,...,"connection_status":"disconnected","detail":"The ProPresenter plugin is disabled."}`.
Same situation — the endpoint reports state truthfully; ProPresenter
integration is not enabled/configured on this installation, so there is no
live connection to verify. Enable `STAGEPILOT_PROPRESENTER_ENABLED=true` and
set host/port to check real connectivity.

MIDI: `GET /api/v1/midi/inputs` returned `{"enabled":false,...,"inputs":[]}`
and `GET /api/v1/midi/network/status` returned
`{"transport_enabled":false,"gateway_available":false,...,"detail":"Network MIDI transport is not configured."}`.
MIDI is running in `midi_source=simulated` mode on this host, so no physical
device list is available to enumerate here. **Open follow-up (not a
blocker):** device enumeration and live signal delivery against real
hardware still need to be run against the production-configured instance
with an operator present; this check only confirms the enumeration
endpoints themselves respond correctly.

Overall app health: `GET /api/v1/health` / `/api/v1/health/ready` returned
`status:"degraded"` — the `lights` plugin reports
`"Lighting MIDI outputs could not be listed."` (expected: lights/MIDI are
simulated/disabled on this box) while the `demo` plugin is `running`. This is
consistent with a demo-mode install, not a new fault.

Security note: the dashboard PIN was still the documented default `1234`
during this check (used only to read status, then logged out). **Rotate it
before/at production go-live** — this is a pre-existing gap, not introduced
by this check.

systemd: `systemd-analyze --user verify stagepilot-beta-managed.service
stagepilot-beta-managed-connector.service` exited `0` (no output — passing
result) for both currently installed units. `loginctl show-user agenthub`
reports `Linger=yes`. `mount | grep -i sony` shows
`/dev/sda1 on /media/SONY type ext4 (rw,relatime)` — mounted and available.
Both installed unit files already use quoted `ExecStart` arguments (the
literal `"..."` tokens are in the unit file on disk) and `WorkingDirectory`
is set to `/home/agenthub/stagepilot-beta/backend` — no quoting problem
found. `systemctl --user status` for both services shows `active (running)`,
continuously up since 2026-09-12 (14+ days uptime), `enabled`.

## Backup and recovery — policy and procedure

**Policy (what must be backed up):** the private Remote state root
(`.tools/remote-managed/` in this deployment, or the private state directory
you configured under `--state-root` in production), specifically:
- `backend/identity.sqlite3` — the operator/session identity database.
- `export/remote.json` — the desired Remote policy/config file.
- `export/connector.token` — the Cloudflare tunnel credential, when present
  (a revoked/disabled installation may have none).
- Application `settings.json` and (Docker path) the private Planning Center
  credential file, plan cache, and lighting cue maps, per `docs/docker.md`
  "Production credentials, recovery, and logs".

All of the above contain live secrets or session material. Treat backup
media as sensitive: encrypt it, restrict filesystem access to the service
user, keep at least one offline/off-host copy, and never commit any of it to
git or attach it to a ticket/doc.

**Procedure (how, today — target deferred by operator):** use the existing
`stagepilot.remote_backup` CLI rather than copying the live SQLite file
directly, since it takes an application-consistent snapshot:

```sh
# Stop nothing for backup — this reads a consistent SQLite snapshot live.
python -m stagepilot.remote_backup backup \
  --state-root /home/agenthub/stagepilot-beta/.tools/remote-managed \
  --destination /absolute/encrypted-backups/stagepilot-remote-YYYYMMDD
```

This produces a new mode-0700 directory containing the copied private files
plus a manifest recording whether a revocable connector token was present.
**The actual backup destination (where encrypted-backups/ lives — e.g. an
external drive, off-host target, or cloud target) is explicitly deferred by
the operator ("we will later")**; this procedure is ready to run against
any absolute destination path once one is chosen. No working backup
destination has been built or is claimed here.

**Restore procedure** (maintenance window; stop both services first):

```sh
systemctl --user stop stagepilot-beta-managed-connector.service \
  stagepilot-beta-managed.service

python -m stagepilot.remote_backup restore \
  --source /absolute/encrypted-backups/stagepilot-remote-YYYYMMDD \
  --state-root /home/agenthub/stagepilot-beta/.tools/remote-managed

systemctl --user start stagepilot-beta-managed.service
systemctl --user start stagepilot-beta-managed-connector.service
curl --fail http://127.0.0.1:18765/api/v1/health/ready
curl --fail http://127.0.0.1:18767/ready
```

Restore validates the manifest, private file permissions, Remote policy, and
the SQLite database, and removes a stale destination token when the backup
had none. After restore, confirm ownership/mode on the restored files,
verify both readiness endpoints return 200, and confirm existing Remote
sessions behave as expected (old sessions may need to re-authenticate). If
the backed-up installation's tunnel token was ever revoked at Cloudflare, do
not reuse it — leave Remote disabled and provision a fresh generation
instead of restoring a dead token.

**Cadence recommendation (policy, to activate once a destination exists):**
run the backup command on a schedule shorter than any planned production
change window (e.g. daily, before each release, and before any host
maintenance/reboot), and do one real restore-into-an-isolated-directory
drill before relying on it operationally.

## Milestone B packaged-desktop implementation

Settings → Remote Access exposes the packaged friend flow: strict one-time
bundle import, first local Operator setup, enable/reconnecting/connected states,
the stable HTTPS URL, and permanent Disable. Bundle replay IDs remain persisted
after revocation, while only origin, installation, hostname, port, schema/version,
and replay metadata are stored locally. Installation credentials cross an
ephemeral authenticated loopback channel to native Tauri code and live only in
Windows Credential Manager or macOS Keychain. Tunnel run tokens are transient
launch material, are never written to `connector.token` by the packaged desktop,
and are reacquired through `/reconcile` after restart. Import accepts only a
timezone-qualified issue time no older than seven days (with at most five minutes
of future clock skew) and authenticates the enrollment against the trusted live
control plane before native storage.

Disable closes the local listener first, permanently revokes the installation at
the control plane, removes the OS credential, clears transient token state, and
leaves LAN/Tauri and production integrations running. Re-enrollment requires a
new admin-created installation and new friend bundle; consumed bundles remain
invalid. The import UI asks the user to delete transferred source copies after
success but does not claim secure source-file erasure.

Automated tests cover schema/binding/replay rejection, offline retry, permanent
revocation, one-child reconnect, transient token-file cleanup, UI transitions,
native target compilation, and packaging configuration. They are not evidence
of a fresh-machine install, native credential-store round trip, live provider
lifecycle, or computer-restart recovery; those remain native acceptance gates.

## Production hardening implementation — 2026-09-12

The existing managed deployment now has a production configuration boundary without introducing
a new installation method. `stagepilot.remote_deploy --environment-file /absolute/private.env`
references, but never embeds, an existing service-user-owned mode-`0600` environment file. The
generated user units require the repository/state mounts, retain `UMask=0077`, use read-only home
and system protection with only the private state root writable, and send redacted structured logs
to journald instead of discarding them. `systemd-analyze --user verify` remains a required gate.
The currently installed beta units predate these generated-unit changes and must not be replaced
until a real production environment file and maintenance window are approved.

Required production variables are the existing settings interface: set
`STAGEPILOT_SERVICE_SOURCE=planning_center`, `STAGEPILOT_PCO_APP_ID`,
`STAGEPILOT_PCO_SECRET`, `STAGEPILOT_PCO_SERVICE_TYPE_ID`,
`STAGEPILOT_MIDI_SOURCE=real`, `STAGEPILOT_MIDI_ENABLED=true`, the exact
`STAGEPILOT_MIDI_INPUT_NAME`, `STAGEPILOT_PROPRESENTER_ENABLED=true`, and the exact
`STAGEPILOT_PROPRESENTER_HOST`, port, timer, and optional look. Use a dedicated non-root service
user; its private environment/state directories are `0700`, files are `0600`, and neither the
Cloudflare account token nor control journal belongs in the installation export. Planning Center
PATs have no separate product-scope selector and inherit the creating user's permissions. Use a
dedicated least-privileged user that can read the selected Services service type, plans, times,
items, and songs; no People, Giving, Check-Ins, Calendar, billing, or organization-administration
permission is needed where Planning Center's role model permits that separation.

Operational checks:

```sh
loginctl show-user "$USER" -p Linger
findmnt /media/SONY
systemd-analyze --user verify stagepilot-beta-managed.service \
  stagepilot-beta-managed-connector.service
journalctl --user -u stagepilot-beta-managed.service \
  -u stagepilot-beta-managed-connector.service --since today
python -m stagepilot.remote_health \
  --status-file /absolute/private/connector/status.json --max-age-seconds 10
curl --fail http://127.0.0.1:18765/api/v1/health/ready
curl --fail http://127.0.0.1:18767/ready
```

The freshness command exits nonzero for a missing, invalid, future-dated, stopped, or stale
heartbeat and prints only sanitized state/age/detail JSON. Run it from the site's existing monitor
at an interval shorter than its alert deadline. A forced stale test must alert before production
acceptance. Port `18767` is cloudflared's loopback-only connector `/ready` endpoint and exists only
while an enabled named connector process is running; it reports edge connectivity, not StagePilot
application readiness. `/api/v1/health/ready` now gives per-application, service-plan, and plugin
diagnostics while retaining HTTP 503 for degraded readiness.

Use the online SQLite-safe recovery utility rather than copying a live identity database:

```sh
python -m stagepilot.remote_backup backup \
  --state-root /absolute/private/remote-state \
  --destination /absolute/encrypted-backups/stagepilot-remote-YYYYMMDD

# Restore only in a maintenance window with backend and connector stopped.
python -m stagepilot.remote_backup restore \
  --source /absolute/encrypted-backups/stagepilot-remote-YYYYMMDD \
  --state-root /absolute/private/remote-state
```

The new backup directory and every file are private; the manifest records whether the revocable
connector credential was present. Restore validates the manifest, private permissions, Remote
policy, and SQLite database, and removes a stale destination token when the backup had none. After
restore, verify ownership/mode, start the backend before the connector, check both readiness
endpoints, and confirm old Remote sessions behave as intended. If a backed-up tunnel was revoked,
do not reuse its token: keep Remote disabled and provision a new generation.

Code-level acceptance at `2026-09-12T14:38:31Z`: focused backend deployment, backup/restore,
heartbeat, redaction, and readiness tests passed; focused Remote UI tests cover explicit password
replacement confirmation and last-Operator protection. Live production readiness is still gated
on operator-supplied Planning Center credentials, reachable ProPresenter, selected physical MIDI
devices, installation of the regenerated hardened units, a real stale-heartbeat alert, an
encrypted off-host backup/restore drill, and another actual target reboot. Do not claim those
external gates from the isolated demo deployment.

## Final product-level named Remote acceptance — passed 2026-09-12

The managed StagePilot UI and external `stagepilot.remote_control` completed two
full stable-hostname generations. Settings → Remote Access → Enable reached
Enabling and then Connected at exactly
`https://stagepilot-beta.illuminary.studio`. First tunnel
`53c24554-0987-42ca-9b6b-2aa8b28faa41` passed public HTTPS, authenticated WSS and
reconnect, anonymous/LAN-PIN rejection, secure cookies, Viewer/Operator policy,
CSRF, and logout revocation. UI Disable plus CLI disable removed DNS, tunnel,
credential, connector process, and Remote listener while local health stayed 200.

Clean UI re-enable and CLI reprovision created distinct tunnel
`851f2faf-a9e6-468a-8f38-679af3797cd9`, retained the same installation and stable
URL, and repeated the complete browser pass after bounded Cloudflare wildcard
certificate convergence. Final UI/CLI disable was read back from both the product
and provider: UI Off, control disabled with empty tunnel ID, zero matching Remote
DNS records, zero active matching tunnels, no installation credential, desired
exposure disabled, port 18766 closed, zero cloudflared, both managed services
active, and local `/api/v1/health/live` 200. Evidence is in
`.tools/remote-product-final/`.

One real product defect was fixed minimally: named connector state now feeds the
existing product intent/status path, a non-secret stable-origin marker prevents a
configured named installation from falling into Quick-Tunnel mode, and the UI
identifies the URL as stable. Provider credentials remain outside the product
runtime and installation export. The HTTP rejection proof was updated to accept
either 426 or a secure Cloudflare redirect; HTTPS/WSS requirements were unchanged.
Targeted results: 19 backend tests, Ruff, mypy, five panel tests, affected ESLint,
frontend typecheck, and production build all passed. No full suite, architecture
redesign, new infrastructure, commit, push, tag, or release occurred. **Final
product-level stable named Remote acceptance is complete.**

## Cloudflare Registrar migration — complete; DNSSEC stabilization pending 2026-09-12

The registrar transfer completed successfully. AWS operation
`16593253-59ca-4621-ae57-44da2f947237` is `TRANSFER_OUT_DOMAIN` / `SUCCESSFUL`,
and Route 53 Domains no longer owns the domain. Cloudflare Registrar reports
`illuminary.studio` active with no pending transfer, expiration
`2028-05-13T17:13:54.846Z`, auto-renew and privacy enabled, and the lock restored
(`clientTransferProhibited,transferPeriod`). Delegation remains
`lynn.ns.cloudflare.com` / `melody.ns.cloudflare.com`. The private EPP artifact
was deleted after use without disclosure.

The Cloudflare Free zone remains Active with eight expected records. 1.1.1.1,
8.8.8.8, and 9.9.9.9 returned `NOERROR` and the expected answers for apex and
wildcard A, two ACM CNAMEs, DMARC, Resend DKIM, SES MX, and SES SPF; no tested
record returned `SERVFAIL`. The retained Route 53 hosted zone still contains all
ten records, including NS/SOA, and remains the rollback source.

DNSSEC signing is enabled at both Cloudflare authoritative nameservers: matching
DNSKEY/RRSIG data is present (algorithm 13, KSK tag 2371). End-to-end DNSSEC is
not yet accepted: Cloudflare status is `pending`, the registrar exposes no DS,
and all six `.studio` parent authorities still return no DS. Public validators
therefore return healthy unsigned answers without AD, and `delv` reports an
unsigned—not bogus—chain. Keep Route 53 intact. Final DNSSEC gate is DS presence
at every `.studio` parent, Cloudflare status `active`, AD-flagged `NOERROR` from
multiple validators, no `SERVFAIL`, and successful resolution of all eight
records. The registrar migration is complete; only DNSSEC registry propagation
and stable end-to-end validation remain.

## Final unattended control-plane proof — passed 2026-09-12

After narrowing/updating the private Cloudflare control token to Account
Cloudflare Tunnel Edit plus DNS Edit only for `illuminary.studio`, the complete
stable-hostname lifecycle passed using `stagepilot.remote_control` alone. MCP was
not used for lifecycle operations. CLI tunnel
`8b0db3b8-efcf-4227-979a-3afbff950fdc` passed HTTPS/WSS and isolated connector
restart with unchanged backend PID; CLI revocation removed credential, DNS and
tunnel. Clean CLI reprovision created distinct tunnel
`d8d3de5f-45b0-4c1c-8d70-630a4cec1097` and passed HTTPS/WSS again after transient
Cloudflare edge-certificate convergence. Evidence:
`.tools/remote-cli-final-browser1.json`,
`.tools/remote-cli-final-browser-restart.json`, and
`.tools/remote-cli-final-browser-reprovision.json`.

Final CLI disable/read-back at `2026-09-12T10:47:48Z` confirmed Remote disabled,
credential absent, exact DNS/tunnel absent, Remote ports closed, zero cloudflared,
managed services healthy, local health 200, and reboot checks passed. The final
public 525 is the DNS-only wildcard falling through to the pre-existing web origin
with broken TLS SNI, not StagePilot exposure. **Unattended named-tunnel
provisioning is fully proven.** See `remote-provisioning.md` for exact ordering.

## Cloudflare DNS and stable named hostname — passed 2026-09-12

AWS nameserver operation `fca7a0cb-d381-4b4e-8300-7b04c2daf8bd` succeeded;
AWS detail and all six `.studio` parent servers confirmed delegation to
`lynn.ns.cloudflare.com` and `melody.ns.cloudflare.com`. A single post-delegation
activation check made the Cloudflare Free zone Active. Both Cloudflare
authoritatives plus 1.1.1.1 and 8.8.8.8 returned all eight expected records with
no omissions/differences. Route 53 retains its ten-record hosted zone, DNSSEC is
off, and the registrar transfer lock remains enabled. Existing DNS-only web origin
TLS fails SNI for apex/`www`; fix the origin independently.

Stable named-tunnel lifecycle **passed** for
`stagepilot-beta.illuminary.studio`: exact loopback `127.0.0.1:18766` ingress,
deny-all catch-all, no WARP routing, private installation-only token, HTTPS and
authenticated WSS, connector-only restart with unchanged backend PID, full
revocation/fail-closed behavior, and clean new-generation reprovisioning. Browser
evidence is `.tools/remote-named-browser.json`,
`.tools/remote-named-restart-browser.json`, and
`.tools/remote-named-reprovision-browser.json`. The first and second tunnel IDs
were `9d164eec-7595-4077-b3fa-a50c86eb08f1` and
`44005b5e-f358-49a7-bbc0-e666dcfe34cf`; both were deleted after acceptance.

Final read-back: Remote disabled, public endpoint 530, Remote DNS absent, no
active installation tunnel, token absent, Remote ports closed, zero cloudflared,
managed services healthy, local health 200, reboot checks passed. The existing
private API token lacks Tunnel Write/config/token permissions, so this acceptance
used authenticated Cloudflare MCP for control-plane operations. Replace it with a
correctly scoped control-plane token before claiming unattended CLI provisioning;
never place that token in the installation export. Full details and exact ordering
are in `remote-provisioning.md`.

Follow-up 2026-09-11T21:16:00Z: inspected old logs/state first. Connector log was
empty and cleanup had overwritten the error heartbeat; source discards raw
connector output and does not persist the in-memory 429 flag. The prior attempt
cannot be classified reliably as external failure versus StagePilot defect.
One subsequent managed UI Enable after a 60-second pause again reached generic
error with no URL. No successful Connected/HTTPS/WSS/browser Disable is claimed.
Evidence is `.tools/remote-remaining-gate/results.json`; cleanup Disable returned
200, local health 200, managed recovery checks pass, Remote is disabled and no
cloudflared process remains. No further retries or full suites. See the dated
investigation in `remote-provisioning.md` for the diagnostic limitation.

Outstanding-gate follow-up, 2026-09-11T21:11:32Z: **fresh-store browser bootstrap
passed** through Settings → Remote Access → Enable → Create first Operator.
Exactly one enabled Operator was read back in the isolated store. That same
submission requested connectivity once; product status became error with no URL.
The earlier Cloudflare rate limit is not proven cleared (no raw provider error
was exposed). No second enable/manual tunnel probe was made. The Connected →
HTTPS/WSS → browser Disable gate remains blocked/unverified. Cleanup API disable
returned 200, fresh status read back off, test processes were reaped, managed
health stayed 200, Remote ports are closed and cloudflared count is zero.
See the dated subsection in `remote-provisioning.md` and private
`.tools/remote-fresh-acceptance/results.json`. No architecture changes/full suites.

Latest continuation: see the **Productization continuation** section in
`remote-provisioning.md` for the implemented Settings/API flow and validation.
The new product integration's live connected-UI gate is blocked by Cloudflare
Quick Tunnel HTTP 429/error 1015, directly observed 2026-09-11T21:01:19Z.
Previous successful manual Quick Tunnel rounds below remain historical evidence,
not a substitute for completing that new gate. Rate-limited automatic provisioning
retries now halt with a sanitized product error. Final native read-back confirms
Remote disabled, no cloudflared processes, local health 200, and passing genuine
reboot recovery checks. Named lifecycle remains unverified; no account token used.

Continuation of [Milestone 2](remote-provisioning.md). Acceptance began
2026-09-11 on `agent-hub`, user `agenthub`, an LXC guest. Repository HEAD remains
`aa752f1`; existing unpublished changes are preserved. This record separates
current observed results from prior validation and outstanding gates.

## Quick Tunnel live acceptance — passed 2026-09-11

At the user's direction, named provisioning was replaced for this acceptance by
temporary Quick Tunnels. No API token was read or used in this continuation.
Existing `.tools/cloudflared` version 2026.9.0 was reused. No Docker, installation
method, backend architecture, or production configuration was introduced.

The existing managed single runtime dynamically opened `127.0.0.1:8766`.
Direct HTTP reached that dedicated listener and returned **426**, as required by
its HTTPS-provenance gate; this is reachability, not an unauthenticated bypass.
Each connector ran independently using:

```sh
.tools/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8766
```

The exact generated origin was applied to the existing managed desired policy
after allocation. No LAN listener, MIDI, SSH, UDP or arbitrary TCP was tunneled.

### Successful live rounds

1. `https://unwrap-honest-fred-plastic.trycloudflare.com`
2. `https://commitment-midlands-implemented-employer.trycloudflare.com`

Both browser runs exited **0** and proved:

- Real Chromium HTTPS dashboard loading and email/password login for Viewer and
  Operator, with Secure/HttpOnly/SameSite=Strict session cookies.
- Anonymous state access 401; remote LAN-PIN login 403.
- Viewer state access allowed, settings/actions denied; Operator settings and
  CSRF-authorized action allowed. Missing CSRF denied with 403.
- Authenticated WSS initial snapshot and a new WSS connection after disconnect,
  for both roles. Logout revoked subsequent authenticated access (401).
- Public plain HTTP rejected with 426, including forged forwarded-protocol input.

After **each** connector was terminated and reaped, the public route returned
**HTTP 530**, while local `/api/v1/health/live` remained **200**. Backend PID
remained **281** throughout both rounds. The existing running timer was unchanged
across each tunnel stop: round 1 start `2026-09-11T17:39:09.135832Z`, duration 281;
round 2 start `2026-09-11T17:39:34.591471Z`, duration 336. Both before/after states
were `running`, with no timer error. The browser deliberately invokes the demo
Operator action during each round; it is not claimed that those deliberate
actions leave the timer unchanged between rounds.

The second new connector produced a different hostname and repeated the complete
HTTPS/auth/WSS proof without restarting the backend. This proves temporary
transport recovery, not stable hostname recovery or named credential revocation.
Stopping a connector makes the public transport unavailable; it is not itself
session revocation. Final cleanup additionally disabled the managed listener.

### Failures and recovery

Initial attempt `annie-instrumental-dogs-contacts.trycloudflare.com` registered
a Cloudflare connection, but AgentHub DNS returned name-not-known and readiness
expired. Public DNS-over-HTTPS subsequently resolved it. Another fresh hostname
initially lacked a public A record; bounded DNS readiness retries were added.
The next attempt reached HTTPS, but Node rejected an unquoted preload path with
spaces (`-- is not allowed in NODE_OPTIONS`); quoting the harness path fixed it.
Each failed attempt terminated its child and returned Remote to disabled.

The successful tests used Cloudflare public DNS-over-HTTPS A answers in
**test-process-only** Python/Node lookups and Chromium resolver rules. Original
hostnames, Host headers, SNI and certificate validation were retained; no TLS
bypass, `/etc/hosts`, or system resolver change occurred. Therefore normal
AgentHub resolver propagation is **not** claimed proven by the successful runs.
Cloudflared's ICMP permission warning was irrelevant to HTTPS/WSS; no privileges
were granted to enable ICMP.

### Evidence, rerun, and safe final state

Run from the beta repository:

```sh
.tools/backend-venv/bin/python scripts/remote-quick-acceptance.py
```

The runner requires initially disabled Remote, reuses private proof-only users,
creates only temporary Quick Tunnels, and ends disabled. It uses the existing
browser proof plus `scripts/remote-proof-dns.cjs` for scoped DNS resolution.
Results: `.tools/remote-quick-acceptance/results.json`, `browser-1.json`, and
`browser-2.json`; private tunnel logs remain in that directory. Rerunning replaces
these proof artifacts. No credentials are printed or stored in the report.

Final verification: both managed services active/enabled; Remote disabled;
connector heartbeat fresh/disabled; installation token absent; ports 8766 and
18766 closed; local health 200. The preserved actual-reboot observer still passes.
Both successful public tunnels have been stopped; the listed URLs are evidence,
not a running deployment. Targeted Ruff and Node syntax checks passed. No product
code changed and no completed full suite was repeated.

**Future gates remain unverified:** stable hostname, named-tunnel credential
revocation, named-tunnel reprovisioning, and named transport reboot recovery.
The earlier real guest reboot/disabled-service recovery remains valid. No commit,
push, release, original-project edit, API-token use or named provisioning occurred.

## Earlier acceptance update — 2026-09-11T14:51:25Z

This update supersedes the earlier blockers below. Docker and new installation
methods are explicitly out of scope for this continuation.

### Genuine AgentHub guest reboot: passed

Ran `python3 scripts/remote-boot-acceptance.py verify` against the preserved
preboot checkpoint: exit **0**, `passed=true`, including
`actual_reboot_observed=true` and `preboot_checks_passed=true`.
Both managed units are active and enabled; Remote remains disabled; connector
status is fresh and disabled; the installation token is absent; Remote ports
8766 and 18766 are closed; local health is HTTP 200. The automatically started
`stagepilot-beta-boot-acceptance.service` reports `Result=success` and
`ExecMainStatus=0`. This proves recovery across the actual AgentHub LXC guest
reboot, not a Proxmox host reboot or just a backend restart. Evidence remains in
`.tools/remote-acceptance/after-reboot.json`; the baseline was not replaced.

### Named Cloudflare lifecycle: active token, target discovery blocked

The user supplied `/home/agenthub/.config/stagepilot-remote/cloudflare.env`.
Its mode is 0600; it contains only `CLOUDFLARE_API_TOKEN`. The token was read
privately into an HTTPS client's Authorization header, never sourced as shell
code, printed, copied into installation exports, or put in command arguments.

Real read-only API results:

- `GET /user/tokens/verify`: HTTP 200, success true, status active.
- `GET /zones?per_page=50`: HTTP 200, success true, empty result,
  `count=0`, `total_count=0`, `total_pages=0`.
- `GET /accounts?per_page=50`: HTTP 200, success true, empty result,
  `count=0`, `total_count=0`, `total_pages=0`.

This does **not** prove that the token lacks tunnel/DNS write permission. Resource
listing visibility may differ from permissions on an explicitly identified
resource. The credential directory has no additional configuration. An account
ID, zone ID, and authorized exact hostname cannot be inferred safely. Requested
those non-secret identifiers (or a private configuration path); no response was
received in the clarification window. Do not guess a domain, expand token scopes
unnecessarily, or claim named provisioning succeeded from token verification.

No tunnel, DNS record, connector credential or Remote policy was created/changed
in this continuation. Named HTTPS/WSS, remote LAN-PIN rejection through the named
route, connector restart isolation, live revocation/fail-closed behavior, and
clean re-provisioning remain pending this deployment target information.
Recovery: obtain the three non-secret target identifiers, validate them with the
existing `ControlConfig`, and use the existing control/provider implementation
against the specified resources. Keep the account secret control-plane-side.
Then execute the requested live lifecycle on the existing managed runtime,
without another installation or any Docker work. Post-reboot live Remote
re-enablement is part of that pending gate; do not substitute prior mock proofs.

No product code changed, no full validation was repeated, and no commit, push,
tag, release, original-project edit, privilege change, or Docker action occurred.

## Earlier Gate 1 — named Cloudflare Tunnel: blocked

Credential discovery found no Cloudflare/CF API/tunnel credential environment
keys; no account-key declarations in the accessible Hermes environment or user
service files; no `~/.cloudflared`, `/etc/cloudflared`,
`~/.config/cloudflare`, `~/.config/stagepilot-remote`, or
`~/.local/state/stagepilot-remote-control` directory; and no control configuration
in either existing private Remote proof/managed state directory. Only credential
names/presence were reported, never values. Protected administrator credentials
were not accessed or presumed available.

No scoped account token, account/zone IDs, or authorized stable hostname were
available to this agent. Consequently no real named-provider API lifecycle could
be attempted. No resources were created and no credential revocation is claimed.
Prior Quick Tunnel proof is not substituted for this gate.

Recovery: securely supply a scoped token file and a private control configuration
using `deploy/remote/control.example.json`. Keep account secrets outside the
installation export. Required scopes and existing enable/status/disable commands
are documented in `remote-provisioning.md`. Then exercise named enable, stable
HTTPS/WSS, independent connector restart, disable with provider read-back,
old-credential denial, and clean re-enable on the same hostname. All remain
unverified live.

## Earlier Gate 2 — actual AgentHub reboot: blocked by authorization

Current native managed backend and connector supervisor are active and enabled;
user linger is enabled. The repository is on the currently mounted ext4
`/media/SONY` filesystem. `CanReboot` through logind returned `Access denied`.
The existing local root broker supports only OpenClaw gateway/memory operations,
not reboot. No privilege or authentication changes will be made.

The read-only `scripts/remote-boot-acceptance.py` observer has been exercised.
Its preboot snapshot passed every current-state check: active/enabled units,
Remote disabled, fresh disabled connector heartbeat, no installation token,
both default/proof Remote ports closed, and local health HTTP 200. A same-boot
verification correctly returned exit 1 with `actual_reboot_observed=false`.
This negative control prevents service restarts from being mistaken for reboot.
The script passed targeted Ruff lint and syntax validation. No full suite rerun.

Private checkpoint: `.tools/remote-acceptance/before-reboot.json`.
Post-check report: `.tools/remote-acceptance/after-reboot.json` (currently the
negative-control result, **not** post-reboot evidence). The observer compares
kernel boot identity and LXC PID 1 start ticks; it never changes Remote policy.
`deploy/remote/stagepilot-beta-boot-acceptance.service` supplies the native
post-boot observer, ordered after the two managed services, with bounded readiness
checking. The unit passed `systemd-analyze --user verify`, was enabled by its
absolute repository path, and was read back as enabled with the expected unit
fragment. It is a read-only acceptance observer, not another backend/connector.

The actual authorized command `systemctl --no-ask-password reboot` returned
exit 1: `Call to Reboot failed: Access denied`. **No reboot occurred.** No forced
reboot, arbitrary elevation, privilege grants, or fallback service modifications
were attempted. Automatic recovery after an actual guest reboot, mount ordering,
and post-reboot Remote/connector re-enablement remain unverified.

Recovery requires an administrator using the existing trusted AgentHub/Proxmox
console or an already-authorized reboot facility. Reboot only the AgentHub LXC
guest, not the Proxmox host. No new root permissions are requested. Before that
operator reboot, preserve the checkpoint and confirm the two managed units plus
`stagepilot-beta-boot-acceptance.service` are enabled. After recovery:

```sh
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user show stagepilot-beta-boot-acceptance.service \
  -p Result -p ExecMainStatus -p ActiveState
python3 scripts/remote-boot-acceptance.py verify
```

Inspect `.tools/remote-acceptance/after-reboot.json`: every check must pass,
including `actual_reboot_observed`, and its timestamp must be from the new boot.
A stale/missing report or failed observer is not successful recovery; inspect
unit status and `/media/SONY` mount availability from the trusted console.
Do not replace the preboot snapshot just to obtain a passing result. It is
intentionally protected against accidental overwrite.

After reboot verification, the existing `.tools/backend-venv/bin/python
scripts/remote-managed-proof.py` can exercise local authenticated re-enablement
and supervisor restart, ending disabled. It still simulates HTTPS provenance
and does not supply a real connector credential. Live connector/WSS recovery
requires clearing Gate 1; do not report the local proof as named-provider success.

Observer rollback (preserve evidence and the two actual managed services):
`systemctl --user disable stagepilot-beta-boot-acceptance.service`.
No reboot was scheduled or promised through a disconnected chat channel.

## Earlier Gate 3 — Docker/headless acceptance (now out of scope)

AgentHub has no Docker/Podman executable or Docker socket. Unprivileged
`unshare -Ur true` succeeds and subordinate UID/GID ranges exist, but
`newuidmap`/`newgidmap` helpers and a rootless engine are unavailable locally.
Namespace availability alone does not establish a working rootless Docker setup.

The existing trusted `docker-host` SSH alias connects as `agentnode` to host
`ubuntu`. Docker CLI and Compose v5.4.0 are installed. Docker daemon access fails
with `permission denied while trying to connect to the docker API at
unix:///var/run/docker.sock`. No Docker group, socket permissions, or MFA policy
was changed. The existing remote broker is not readable by this SSH user. Its no-argument
usage discovery via the existing `sudo -n /usr/local/sbin/dockerhost-root-broker`
returned exit 2 and listed only status/MAC/WireGuard/signed-update/email-MFA
operations, not Docker build/run. No signed update, MFA request, enrollment,
root grant, or privileged Docker workaround was attempted.

The actual Compose parser accepted the unchanged configuration (exit 0):

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 docker-host \
  'docker compose -f - config --quiet' < compose.yaml
```

This is schema/configuration acceptance only: source was not transferred and no
image build, container startup, health check, or container lifecycle was proven.
The existing native headless lifecycle proof remains valid historical evidence,
not proof of container deployment or actual reboot.

Recovery: provide an already-authorized Docker daemon execution path (or have the
operator run the build/stack acceptance). Do not add the agent to the Docker
group, weaken the socket, modify the broker, or enable privileged LXC/container
settings to bypass this gate. A rootless installation is not claimed from a
successful namespace probe; required UID-map helpers are unavailable here.

Once authorized, follow `server-midi-agent-handoff.md` and `docs/docker.md`:
build both existing image targets, start an isolated stack, check both health
checks, and exercise harmless network-MIDI input/output. Use private persisted
state and isolated ports; do not overwrite an existing production installation.
For Remote, launch the existing `stagepilot.remote_server --control-file ...`
entry point with the same runtime, a loopback-only Remote port and an independent
connector. Account secrets must not enter image layers/build contexts or the
connector. Existing Compose does not itself opt into managed Remote; a verified
Remote container launch is still outstanding, not silently implied by config
validation. Reuse the implemented CLI rather than adding another control plane.

## Earlier scope and final state (superseded above)

No StagePilot product code, Docker topology, auth, LAN PIN, Tauri, MIDI or
production settings changed during these checks. Only acceptance observer files,
its enabled user-service links, private checkpoint/report files, and this handoff
were added. No completed full suites were repeated. No commit, push, tag, image
publication or release occurred. Real named-provider, actual reboot, and running
container lifecycle acceptance are all **blocked/unverified**, not completed.

The existing tool locale warning caused false write-verification failures and
contaminated a documentation patch. Files were recovered and independently
compared byte-for-byte to intended content; no machine locale was changed.
