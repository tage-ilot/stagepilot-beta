# Transparent beta enrollment and Cloudflare abuse guardrails

## Decision

Possession of the private beta is sufficient authorization to enroll. There is no GitHub verification, OAuth, StagePilot account, invite code, administrator-created bundle, or private bundle delivery. On first local enable, the app creates a random nonce and retains it only while enrollment is incomplete, receives a random installation identity and unique machine credential, then removes the nonce from ordinary local state and stores the credential only in Windows Credential Manager or macOS Keychain. Every later lifecycle call requires that credential.

A public endpoint cannot prove private-repository possession. The binary is not treated as a secret. Abuse is contained through finite server-side ceilings, revocation, aggregate observability, and fail-closed behavior. No Cloudflare, administrator, signing, GitHub, or reusable tunnel credential ships in the app.

The same Worker may broker strictly allowlisted signed beta updater metadata and
immutable updater assets with a server-only GitHub credential. Those public GET
routes are independently rate limited, accept no arbitrary URL, and never relay
Remote application traffic. Details are in `private-beta-release-and-acceptance.md`.

## Always-on lightweight check-in (no Remote Access opt-in)

Every installation now performs the existing anonymous `enroll()` flow and an
authenticated `GET /v1/installations/:id/status` call once at process start and
periodically thereafter, regardless of whether Remote Access has ever been
enabled. This reuses the existing enrollment and status routes unchanged; it
adds no new endpoint and never triggers tunnel/DNS provisioning (that stays
gated behind Remote Access being turned on). The only data this sends is this
installation's existence and its self-reported `deviceName` (see below) -- no
other personal data, no browsing/usage telemetry. This exists solely to give
the fleet admin panel visibility into installations that have never enabled
Remote Access.

## Self-reported deviceName

Every enroll/status/provision/reconcile request may optionally include
`deviceName` (the machine's own reported hostname, captured client-side via
Python's `platform.node()`). This value is self-reported and UNTRUSTED: the
control plane never uses it for authentication, installation matching, or any
security decision. It exists purely so the fleet admin panel can show a
human-meaningful hint about which physical machine an installation is -- the
panel also still shows the operator-set label and the generated hostname. An
installation's label auto-fills from `deviceName` the first time one is ever
received for that installation and only while no label has been set; once an
operator sets any label, this never silently overwrites it again.

## Implemented controls and thresholds

- Enrollment: 3 new installations per canonical source IPv4 or IPv6 /64 per 24 hours. The source is HMAC-hashed with server-only key material; raw addresses are not stored. Source records expire after 24 hours and the retained index is capped at 2,000 entries.
- Developer-network enrollment exemption: the optional Worker variable `ENROLLMENT_EXEMPT_SOURCES` is a comma-separated list of already-normalized sources (e.g. `192.0.2.10` or `2001:db8:1:4::/64`, using RFC 5737/3849 documentation ranges here — the deployed variable holds the operator's real values, never committed to this repository) that skip only the per-source `ENROLLMENTS_PER_SOURCE` check above. It is a developer convenience so work on this project is never blocked by the beta abuse limit; it defaults to unset, which exempts nobody, so production behavior is unchanged by default. An exempt source is still subject to `ENROLLMENT_ENABLED`, the global `BETA_INSTALLATION_LIMIT` ceiling, `MAX_SOURCE_QUOTAS` pressure, nonce idempotency, and every downstream status/mutation/provider quota — it is never an unlimited bypass. The comparison uses the same `normalizeSourceAddress` as the quota itself, on the already-normalized configured value, never the raw header, so a differently formatted or malformed configuration entry is not treated as a match. Exempt enrollments still increment `enrollments`/`activeInstallations` so capacity stays observable, and never increment `enrollmentDenied`. This list must contain only trusted developer networks, never a beta user's address.
- Replay: a valid nonce replay returns the same installation and credential before any quota check and does not consume quota.
- Global gate: 500 active installations by default, configurable with `BETA_INSTALLATION_LIMIT`; `ENROLLMENT_ENABLED=false` stops only new enrollment.
- Installation API: 120 status requests and 20 lifecycle mutations per installation per 60 seconds. Status never invokes Cloudflare. Confirmed provision responses are cached in memory for 30 seconds so rapid reconcile/provision replay is provider-free.
- Provider API: all requests are serialized in the singleton Durable Object. The total ceiling is 600 calls per 5 minutes; ordinary provisioning and enabled reconciliation stop at 480, reserving 120 calls for disable, revoke, and recovery. This is below Cloudflare's documented 1,200 calls per 5 minutes and 200 calls per second per IP.
- Client: enrollment and lifecycle requests make at most three attempts for 429/503 responses, honor bounded `Retry-After` values, and add exponential backoff with jitter.
- Edge: exactly one Free-plan `http_ratelimit` rule with ref `stagepilot_remote_beta_rate_limit_v1`, expression `(http.host wildcard "sp-*.illuminary.studio")`, 60 requests per source IP/colo per 10 seconds, and a 10-second block. WSS messages are not counted as HTTP requests; initial upgrades and reconnects are.
- Observability: the administrator metrics route exposes only aggregate active/enrollment and denial counters. It emits no raw IP, keyed source hash, installation ID, hostname, provider body, or credential.

Quota and capacity denials return sanitized 429 or 503 responses plus `Retry-After`. They create no installation identity, tunnel, DNS record, or Cloudflare provider state. Managed DDoS remains in its separate Cloudflare phases, and the WAF deployment helper refuses to replace an unrelated rate-limit rule.

## Operations and rollback

The manual protected deployment workflow supplies `ENROLLMENT_ENABLED` and `BETA_INSTALLATION_LIMIT` and deploys the Worker. An independently authorized zone-WAF operator runs `node control-plane/scripts/waf-rate-limit.mjs apply` and reads back the exact rule; the narrower Worker/tunnel token intentionally cannot edit WAF. Monitor `GET /v1/admin/metrics` with the administrator credential and Cloudflare Security Events. Tune only after measuring ordinary HTTPS polling and WSS upgrade/reconnect rates.

Emergency enrollment rollback is `ENROLLMENT_ENABLED=false` followed by a manual Worker deployment; existing authenticated lifecycle and cleanup continue. WAF rollback deletes only the exact StagePilot rule/ruleset in the zone `http_ratelimit` phase and verifies that managed DDoS and all other phases and hosts remain unchanged. Code rollback must preserve the existing Durable Object class/binding and forward-only migration history.

### Refreshing the developer-network exemption

`ENROLLMENT_EXEMPT_SOURCES` is set on the `stagepilot-control-plane` GitHub
environment as a comma-separated list and deployed by `deploy-control-plane.yml`
(`gh variable set ENROLLMENT_EXEMPT_SOURCES --env stagepilot-control-plane
--repo tage-ilot/stagepilot-beta --body "<ipv4>,<ipv6-/64>"`, then dispatch the
workflow). Both entries are ISP-assigned and can change. To refresh:

1. Re-read the current developer-network sources:
   `curl -s https://cloudflare.com/cdn-cgi/trace | grep ^ip=` for the default
   route (usually IPv6) and `curl -4 -s https://cloudflare.com/cdn-cgi/trace |
   grep ^ip=` for IPv4.
2. Normalize the IPv6 value to its `/64` (first four hextets, then `::/64`) —
   the same shape `normalizeSourceAddress` produces internally.
3. Update the `ENROLLMENT_EXEMPT_SOURCES` variable with both values and
   redeploy.
4. Read back the deployed configuration (`deployment-config.mjs validate` logs
   only an exemption *count*, never the raw values) to confirm the change is
   live.

This list must contain only trusted developer networks, never a beta user's
address.

**Never commit real operator addresses to this repository.** The operator's
actual IPv4 and IPv6 /64 belong only in the deployed `ENROLLMENT_EXEMPT_SOURCES`
Worker variable (configuration, not committed). Code, tests, and docs must use
reserved documentation ranges only (RFC 5737 `192.0.2.0/24` for IPv4 examples,
RFC 3849 `2001:db8::/32` for IPv6 examples).

## Limitations

The private binary can be copied, so enrollment is abuse-contained rather than identity-verified. Cloudflare Free permits one zone rate-limit rule and coarse IP-based characteristics; shared NAT sources may share a quota, while distributed sources may each consume quota. Rate limiting is not an exact request counter because edge enforcement may lag briefly. Final native reboot proof still requires reboot-capable Windows x64 and macOS arm64/x64 hardware; this change does not alter the accepted desktop MIDI or local-production path.
