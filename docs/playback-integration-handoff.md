# Playback backend integration handoff

## Consolidated implementation

Isolated clone: `backend-work` in the root task workspace. Branch:
`feat/playback-api-source`; origin verified `huntrw6/stagepilot-beta`.
Base: `4872a5e4fc6b29080ecb1625381bd4ece025d503`.
Sibling slices are consolidated: transport `b7c0087`, settings cherry-pick
`a6bb923` (original `9cf877c`), mapping cherry-pick `0acc15c` (original `c9bf0d1`).
The integration commit follows these; use the exact commit in the card handoff.

- `plugins/playback_api/plugin.py`: selected-input lifecycle supervising unchanged
  MidiPlaybackPlugin or PlaybackClient, bounded dispatch queue/monitor, action
  serialization, stopped-only explicit discovery with complete-result persistence.
- `api/playback.py`: typed status/events/settings/find and confirmed discovery.
  No play/pause/select/seek or manual-epoch confirmation endpoints.
- `main.py` and `core/runtime.py`: instance-owned plugin/client factory injection;
  the plugin manager owns shutdown. MIDI remains the existing implementation and
  controller surface, chosen dynamically by routes rather than stale runtime refs.
- Generic settings writes serialize with discovery and reject manufactured order
  fields. Input-source-only updates are live without restart. Existing MIDI filter
  updates do not reopen the selected port. Only initial persisted load and completed
  discovery install mapping trust; unrelated settings do not silently rearm it.
- Mapping adds `discard_pending()` to discard old queue envelopes on disconnect or
  source shutdown without invalidating saved order. Reconnect revalidates its first
  snapshot; version/unknown-ID invalidation stays sticky until discovery.
- Docs: `playback-api.md` is the frontend API/operator contract; configuration,
  architecture and Unreleased changelog updated. Follow Hunter's amendment, NOT
  the withdrawn passive/manual-epoch policy from older root comments.

## API decisions frontend must consume

Prefix `/api/v1/playback-api`: GET status, GET events, PUT settings, POST find,
POST discover-song-order. Full bodies/status fields are in `playback-api.md` and
OpenAPI. Settings includes only enabled/host/port/auto_scan; discovery alone owns
order/version/time. Manual host remains authoritative even for Find.

Find starts a new receive/address search and returns initial status; poll status
for the result. Discovery requires literal JSON `{confirm: true}` (otherwise
400), rejects playing/disconnected/running with 409, remains open while discovery
runs, and returns status `done|failed` at completion (200). Poll status concurrently
for `progress` (commands issued, not percent). A 200 failed run is not success.
Settings/Find conflict while discovery is running. Navigation limit is 200 TOTAL
commands, including start traversal/restoration, five seconds per step. Continuous
fresh unchanged heartbeats distinguish nonwrapping ends from silence. Failures
preserve previous persisted result, release suppression, and tell the operator to
check Playback's selection. No select command is used for restoration.

Events are a 100-entry oldest-first monitor, including monitor-only observations
and discovery-tagged events. Poll and replace snapshots; event timestamps are
monotonic, not wall-clock. No app control commands are sent by ordinary receive,
Find, reconfiguration, startup, or reconnect.

The existing application `midi_status` readiness slot represents the selected live
input. Use dedicated Playback status for actual input/source/configuration fields.
The stable lifecycle plugin name is midi_playback for initial real MIDI and
playback_api otherwise; dormant source supervision is still RUNNING (not an
external connected claim). Two existing MIDI tests now include that additive
lifecycle entry; all original hardware-free/simulation safety assertions remain.

## Actual final verification

Clean clone, existing frozen backend lock/dependencies:

- `uv sync --project backend --frozen --extra dev`: passed.
- In backend: `uv run --frozen pytest -q`: **550 passed, 1 skipped**, 1 existing
  Starlette/httpx deprecation warning, 41.30 seconds. Skip is Windows st_mode
  synthesis on Linux.
- `uv run --frozen mypy`: **137 source files**, strict, no issues.
- `uv run --frozen ruff check .`: all checks passed.
- `uv run --frozen ruff format --check .`: **137 files** already formatted.
- Integration coverage: **22 tests** collected, included in the full green run;
  nonwrapping fake-loopback discovery from every original selection, restoration,
  confirmations/conflicts, tagged start/pause/stop suppression, disconnect/silence/
  version/wrapping/step-limit failures, complete-only persistence and save failure,
  migrated installation/default/error status, known position/restart/stale/unknown
  mapping, monitor capacity, same-version reconnect, stale envelopes, source
  switching, native MIDI alternative and late closed-listener callbacks, shutdown.
- Transport/mapping/settings suites are included in the same full run (local-first,
  attached-LAN/manual-exclusive search, wire normalization, all monitor-only rows,
  same-batch invalidation and existing MIDI/network-MIDI regressions).
- Node **22.23.3**, existing frontend lock restored with `npm ci --no-audit --no-fund`:
  `npx --no-install tsc --noEmit` passed; `npx --no-install vitest run`:
  **34 files / 268 tests passed**; `npm run build` passed (Vite 6.4.3).
- `git -c core.whitespace=cr-at-eol diff --check`: passed.
- Diff against base for plugins/midi_playback, network_midi.py, backend manifest
  and lock: empty. No new dependency, frontend source, updater/auth/release change.

Node 26 was the system default; Node 22 was explicitly used from the local npm
cache for frontend gates. An initial nested npm-exec invocation inherited `-c`
and failed usage validation; direct Node-22 PATH plus local npx resolved it. After
frontend build, absent POST control routes return 405 through the static GET-only
catch-all rather than 404; tests assert they are absent from OpenAPI and reject
both serving modes. These were harness/environment issues, not fabricated passes.

## Recovery/publication boundary

Use the verified local integration bundle supplied as a task attachment; it has
base commit 4872a5e4 as prerequisite and the feature branch tip. Root supervisor
can reuse this isolated clone or fetch that bundle into another clone at the base.
Do not cherry-pick the mapper original without its transport base, or separately
reapply already-consolidated settings/mapping slices.

No real Playback/device was probed and no live navigation was attempted. An
application-test autouse fixture disables only the default Playback finder so
legacy tests never scan the production LAN; explicit transport/integration tests
use injected fake loopback sockets. No push, PR, merge, deployment, tag, release,
or shared working-copy modification was performed by integration. The root's
pre-created acceptance checkpoint and frontend lane must run before publication.
