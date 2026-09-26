# Changelog

## [1.1.104-beta.15] - 2026-09-26

### Changed

- The Planning Center settings panel now clearly separates settings that work
  with either Planning Center sign-in or a manual connection from settings
  that only apply to a manual API key. The manual-only section is now
  collapsed by default and labeled "Manual API Connection". Several buttons
  ("Load Service Types", "Save Settings", and related actions) previously
  required an Application ID even when signed in with Planning Center; they
  now work correctly for OAuth-connected users with no manual credentials
  entered.
- Service Type now has an "All" option that searches every active service
  type and uses the single nearest upcoming plan across all of them, instead
  of requiring one specific service type to be selected.
- Timezone is now a dropdown with one entry per UTC offset (labeled by its
  most populous city, e.g. "New York (UTC-5)") instead of requiring exact
  IANA timezone syntax. It defaults to your system's timezone and stores the
  real, DST-aware zone underneath the fixed label.

## [1.1.104-beta.14] - 2026-09-25

### Fixed

- Planning Center OAuth `client_id` was resolving empty in release builds due
  to a GitHub Actions environment-secret scoping mismatch (beta.13
  regression) — sign-in should now actually work. Release builds now fail
  loudly instead of silently shipping broken sign-in if this ever regresses
  again.

## [1.1.104-beta.13] - 2026-09-25

### Fixed

- Planning Center "Sign in with Planning Center" now actually works in
  packaged builds. The OAuth `client_id` was never wired into the real
  release pipeline, so every shipped build (including beta.12) failed
  every sign-in attempt with "This build of StagePilot cannot sign in to
  Planning Center." The client_id is now baked into the desktop binary at
  compile time and threaded through to the backend sidecar, sourced from
  the same `PLANNING_CENTER_CLIENT_ID` repo secret the control-plane
  deploy already uses.

## [1.1.104-beta.12] - 2026-09-24

### Added

- Planning Center OAuth 2.0 sign-in: "Sign in with Planning Center" is now
  the default connection method in Planning Center settings, using an
  in-app browser handshake (desktop PKCE loopback listener + control-plane
  token exchange/refresh). The existing manual App ID/Secret method is
  kept as an Advanced/fallback option — nothing is forced to migrate.
  Includes a connected/disconnected state indicator and a "Reconnect to
  Planning Center" prompt if access is denied, cancelled, or later revoked
  from Planning Center's side. (PRs #29, #30, #31)

### Fixed

- Eliminate a flaky race in the pco_oauth end-to-end callback test: replace
  a fixed 50ms pre-connect sleep with a retry-connect loop and widen the
  callback wait timeout, removing a test-only timing race (no production
  behavior change). (PR #32)
- Eliminate a further structural race in the callback wait() polling loop
  that could still time out on macOS CI runners even with a connection
  already queued (PR #33).
- Fix a real production bug: `CallbackServer::wait()` detached its accept
  thread and leaked the bound TCP listener whenever a Planning Center
  sign-in attempt timed out or was abandoned. Since only 4 loopback ports
  are registered with Planning Center, repeated timed-out sign-in attempts
  by a real user could exhaust all four and break sign-in until the app
  was restarted. Now the listener is properly shut down and joined before
  `wait()` returns. This was also the root cause of the macOS CI flake
  (leaked listeners from prior runs contending for the same 4-port pool).
  (PR #34)

## [1.1.104-beta.11] - 2026-09-23

### Fixed

- Restore the desktop window's maximum size cap (1700x2560) so it matches
  the background image dimensions again; verified against the actual
  measured asset and native window-manager resize/maximize clamping.
- Make a fresh install's dashboard layout match what Reset Layout produces,
  instead of using a different static sizing pass on first load.
- Auto-fit dashboard widget content height on mobile / Remote Access (the
  same React app on narrow viewports), replacing the old static row clamp.
- Allow enabling Remote Access with zero Operators configured.

### Added

- Recent Event Stream: add a hide/show toggle scoped to edit-layout mode
  (persisted, defaults to visible on Reset Layout) and 120-second
  auto-expiry for non-error events.

## [1.1.104-beta.4] - 2026-09-19

### Fixed

- Fix the Regenerate Remote link button so it correctly regenerates the
  Remote link instead of breaking Remote access.
- Add a loading state to the Regenerate Remote link button while the
  request is in flight.
- Add a regression test covering dashboard widget layout persistence.

## [1.1.104-beta.3] - 2026-09-18

### Changed

- Move the Release Channel row and add a Remote Access checkbox with an
  animated in-widget expand in Backend Settings.
- Set the dashboard widget layout default.
- Rename "Open Remote" to "Regenerate Remote link".

### Fixed

- Fix narrow-window layout by setting `tauri.conf.json` minWidth=640 /
  minHeight=480 and removing the max window-size constraints.

## [1.1.104-beta.2] - 2026-09-18

### Fixed

- Add a release-channel (STABLE / BETA) toggle to the frontend updater path.
- Use a 16-hex-character `randomHex(8)` label in the control plane instead of
  a shorter/weaker identifier.
- Retain the enrollment nonce as the durable installation identity across
  `finish_revoke()` so re-enabling after a disable does not mint a new
  identity.
- Move the Remote Access settings entry point inside the Backend Settings
  widget.

## [Unreleased]

### Added

- Register a searchable native macOS Help Book generated from StagePilot's
  Markdown documentation so the system Help menu can return useful local
  setup, operation, and troubleshooting topics.

## [1.1.41] - 2026-07-29

### Fixed

- Add a browser-only PIN gate before the dashboard loading screen, with
  password protection enabled by default and configurable from StagePilot
  Backend without changing the desktop startup experience.
- Report ProPresenter as disconnected, rather than errored, while the
  application is closed or unreachable, while retaining error status for
  confirmed API and configuration failures.
- Continuously monitor ProPresenter, Playback MIDI input, and Lights MIDI
  output availability so status cards reflect closed applications, detached
  devices, and subsequent reconnections without manual refreshes.
- Keep only the newest unresolved error pinned in Recent Event Stream and
  automatically remove its pin when the affected integration recovers.
- Keep the Lights configuration header synchronized with the Lights dashboard
  status instead of showing a separate loading state.
- Move readiness details into a translucent header-status hover/focus panel,
  show disconnected checks neutrally, and keep unconfigured Lights optional
  until a MIDI output has been selected.
- Double the Service Plan widget's default desktop and tablet height so more of
  the service order is visible without scrolling.
- Keep readiness and Planning Center PAT help popovers open while their trigger
  or panel is hovered, open them immediately on click, and use a short,
  discoverable hover delay.

### Changed

- Replace the standalone readiness widget with connection details available
  from the header system status while keeping optional Lights configuration
  from blocking overall readiness.
- Enlarge connection-icon backgrounds without enlarging their icons.
- Place both the Edit Layout button and its editing toolbar below the dashboard.
- Add concise Personal Access Token setup guidance and a direct Planning Center
  help link beside Application ID.

## [1.1.35] - 2026-07-29

### Fixed

- Fixed the packaged backend startup failure on Intel macOS by explicitly
  disabling Hardened Runtime library validation for the ad-hoc release flavor.
- Added final `.app`, DMG, and updater-archive backend launch verification for
  both Intel and Apple Silicon release jobs.
- Preserved specific packaged-backend exit diagnostics so a later generic
  startup timeout cannot hide a macOS signing rejection.
- Replaced the startup bar's fixed 58% backend target with smooth,
  milestone-bounded progress that continues through long healthy starts,
  freezes on confirmed failure, and resumes without moving backward on retry.
- Prevented demo plans and simulated MIDI or timer behavior from reporting
  Planning Center, MIDI, or ProPresenter as connected, and made production
  readiness distinguish real plans, real connections, and timer discovery.

### Changed

- Made the macOS 12.0 deployment target explicit throughout release builds and
  added final Mach-O deployment-target checks.
- Added bounded backend-log rotation and actionable startup recovery controls.

## [1.1.32] - 2026-07-29

- Added production-desktop-only GitHub Release update checks.
- Added explicit update confirmation, signed download/install progress,
  automatic relaunch, and post-update status beside the existing logo.
- Added window size, position, maximized, and fullscreen restoration without
  restoring a minimized window.
- Added coordinated updater archives, signatures, and `latest.json` automation
  for Windows x64, Intel macOS, and Apple Silicon macOS.
- Replaced fixed dashboard snap slots with a responsive GridStack layout that
  supports explicit edit mode, two-dimensional dragging, constrained resizing,
  automatic compaction, intentional spacers, keyboard ordering, responsive
  desktop/tablet/mobile layouts, and v1 layout migration.

All notable changes to StagePilot will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
intends to use [Semantic Versioning](https://semver.org/) once releases begin.

## [1.0.0] - 2026-07-19

### Added

- Production-ready Windows and macOS desktop packaging.
- Persistent Planning Center, MIDI, ProPresenter, lighting, and backend configuration.
- Secure Planning Center credential storage and automatic service-plan loading.
- Playback MIDI automation, synchronized ProPresenter countdowns, and lighting cue timelines.
- Persistent drag-and-snap dashboard layouts with accessible movement controls.

### Reliability

- Added cached service-plan fallback, connection recovery, readiness checks, structured diagnostics,
  and verified ProPresenter timer configuration across repeated cues.

## [0.9.14] - 2026-07-19

- Fixed ProPresenter timer-type conversion by atomically configuring and resetting timers.
- Added pointer-based dashboard widget dragging and made Manual Controls movable.
- Enlarged the dashboard logo without changing header height and refined loading-screen spacing.

## [0.9.13] - 2026-07-19

- Added movable, snapping dashboard widgets with persistent layouts and accessible controls.
- Improved ProPresenter Look confirmation, settings messaging, and timer behavior.
- Disabled the first-launch setup overlay while preserving its implementation.
- Refined the application background placement and loading-screen branding.

## [0.9.12] - 2026-07-19

### Fixed

- Configure ProPresenter timer type and duration before issuing Reset, preventing
  the previous song duration from appearing before the next countdown starts.
- Explicitly reset the ProPresenter timer after Reset Position changes its
  duration to `0:00`.

## [0.9.11] - 2026-07-19

### Fixed

- Configure and reset ProPresenter timers atomically before duration verification,
  allowing repeated song cues to start reliably.
- Allow Countdown to Time and Elapsed Time timers to be selected and convert
  them to Countdown Timers when a song-start cue arrives.
- Preserve packaged backend diagnostics in the macOS application log directory.

## [0.9.10] - 2026-07-19

### Fixed

- Retry loading general settings only after the packaged backend is reachable,
  preventing a stale macOS WebKit `Load failed.` message.
- Verify ProPresenter duration changes through the documented single-timer API
  and allow slower Intel Mac updates without starting an unconfirmed timer.

## [0.9.9] - 2026-07-19

### Fixed

- Preserve completed first-launch onboarding when a stale settings snapshot is
  saved by another configuration panel.
- Verify ProPresenter countdown durations before resetting and starting timers,
  including delayed updates observed on Intel macOS.
- Terminate nested PyInstaller backend processes when StagePilot exits or
  restarts on macOS.
- Explicitly create, show, focus, and restore the StagePilot window when the app
  launches or is reopened from the macOS Dock.

### Changed

- Added macOS desktop lifecycle compilation and regression tests to CI.

## [0.9.8] - 2026-07-18

### Added

- Select and apply a saved ProPresenter Look when ProPresenter settings are saved.
- Standalone MultiTracks MCP cue utility with dry-run planning, guarded writes,
  verification, secure authentication, and reporting.

### Fixed

- Restart the managed desktop backend automatically after MIDI or Planning
  Center settings enable a previously inactive integration.
- Fully quit the managed backend with the desktop application and pass the
  saved-settings path explicitly to packaged backend processes on macOS.

## [0.9.7] - 2026-07-15

### Added

- Initial monorepo foundation for the FastAPI backend, React dashboard, and
  Tauri desktop shell.
- Typed asynchronous event bus, observable application state, isolated plugin
  manager, structured logging, and graceful application lifecycle.
- Demo service workflow with safe song navigation, timer simulation, REST
  actions, health/state APIs, and full-state WebSocket updates.
- Dark, responsive live-production dashboard with connection status, ordered
  songs, current timer state, readiness checks, manual controls, and events.
- Core, plugin, API, and WebSocket tests plus backend and frontend quality
  tooling.
- Shared architecture, contribution, configuration, security, plugin, and
  milestone documentation.
- Safe example environment configuration and repository ignore rules.
- Secret-aware Planning Center PAT configuration with validated credentials,
  IANA time zone, request timeout, and identifying user-agent settings.
- Typed asynchronous Planning Center service-type client with Basic Auth,
  version pinning, safe pagination, timeout handling, sanitized errors, and
  mocked contract tests.
- Today-first Planning Center plan discovery with timezone-aware service-time
  matching and a configurable nearest-upcoming fallback window that defaults to
  30 days. Discovery excludes past and no-service-time plans and preserves
  explicit ambiguity when multiple plans share the selected date.
- Ordered linked and generic Planning Center song parsing with scheduled
  durations, source song IDs, and visible skipped-item reasons.
- Production Planning Center plugin startup and reload orchestration with
  current-or-upcoming last-known-good plans, connection and discovery state,
  explicit dashboard plan selection, skipped-item visibility, live health,
  single-flight refreshes, date-rollover cleanup, and demo-mode isolation.
- First backend MIDI Playback slice with disabled-by-default Mido/RtMidi input
  discovery, environment-configured port, channel, and six cue mappings, bounded
  ordered dispatch, duplicate protection, reconnect handling, safe port metadata,
  session-only API and dashboard input selection, disconnect and refresh
  controls, and manual cue simulation through the hardware action path.
- Bounded live MIDI note monitor with port, channel, note, velocity, and
  accepted-or-ignored diagnostics in the production dashboard.
- Vitest and React Testing Library coverage for dashboard plan ambiguity,
  pending selection, revision-safe live state, stale readiness, and skipped-item
  warnings.
- Persistent v0.5 setup with validated atomic settings, Windows Credential
  Manager PAT storage, independent backend integration modes, service-type
  onboarding, preference-aware plan selection, and a non-secret
  last-known-good service cache.
- Six-step first-launch checklist plus editable general, advanced MIDI, and
  ProPresenter configuration. Production panels activate real integrations when
  saved and no longer expose demo or simulated choices.
- Auto-closing first-launch completion feedback with manual dismissal, plus a
  full MIDI 0–127 note-name dropdown using Playback's octave convention.
- Live MIDI cue-filter reconfiguration so saved note, channel, velocity, and
  debounce changes affect the running Playback input without a restart.
- A persistent **Lights** connection and lighting configuration panel with
  macOS MIDI-output discovery, Lightkey-compatible Note On/Off test pulses, and
  per-song elapsed-time cue maps keyed by stable Planning Center song IDs.
- A monotonic backend lighting scheduler that starts from the confirmed shared
  countdown event, cancels safely on stop/restart, and prevents old timelines
  from firing after a song change.
- Live remaining and elapsed song clocks in the dashboard, derived from the
  same timer start and scheduled duration used by ProPresenter.
- ProPresenter-aligned countdown rounding and start timestamps, plus a Reset
  Position sequence that stops the configured timer and resets its duration to
  zero in both StagePilot and ProPresenter.
- A reserved header notification queue with content-width confirmation and
  error highlights. Concurrent action and service-state messages display in
  order for up to six seconds without shifting dashboard controls.
- Planning Center non-song items interleaved into the service-plan display by
  their original sequence, with compact header separators and subdued ordinary
  items that show Planning Center descriptions and scheduled durations while
  remaining excluded from song controls.
- A bundled 1700-by-2560 film-flare application background that scrolls from
  the top without scaling, plus matching desktop maximum window dimensions.
- A simplified StagePilot header wordmark using a locally bundled Instrument
  Serif font across browser and desktop builds.
- Cohesive, higher-contrast setup panels with a shared Production setup header,
  brighter supporting text, compact connection-status badge, and restrained
  per-integration button accents.
- A darker translucent Now Playing card, a larger outlined StagePilot wordmark,
  and right-aligned header notifications beside the system status.
- Dark translucent connection-card hover states with brighter text, semantic
  manual-control button accents, and green/red event-stream severity styling.
- Outline-first manual controls that reveal their semantic colors on hover and
  press, including a green Restart Current action and dark-green Now Playing label.
- A locally bundled StagePilot header font and a two-message notification queue
  that discards older messages when newer notifications arrive.
- Dark-orange Now Playing labeling and deeper READY-green highlighting for the
  current song row, subtitle, and numbered icon.
- A reproducible PyInstaller backend sidecar, Tauri startup/readiness/port-conflict
  supervisor, owned-process-tree shutdown, NSIS release configuration, desktop
  connection-status bridge, and CI-built Windows installer artifact.

[Unreleased]: https://github.com/huntrw6/stage-pilot/compare/v0.9.9...HEAD
[0.9.9]: https://github.com/huntrw6/stage-pilot/releases/tag/v0.9.9
[0.9.8]: https://github.com/huntrw6/stage-pilot/releases/tag/v0.9.8
[0.9.7]: https://github.com/huntrw6/stage-pilot/releases/tag/v0.9.7
