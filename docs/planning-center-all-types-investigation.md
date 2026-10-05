# All-service-types loading investigation (beta.22)

Status: **unresolved; no verified production fix**. Investigation starts at
`v1.1.104-beta.22` (`7bc0ba8`). No operator API credentials, response bodies,
or actual plan fixtures were available to this run. The test resources are
synthetic JSON:API fixtures using the application's existing fixture helpers,
not a replay of the operator's account.

Release integration update: the observations below describe beta.22, not the
current behavior. PR #82 subsequently isolated per-service-type failures. On
current main, the synthetic invalid fourth-type response leaves the valid
plan loaded and the connection healthy. The integration regression now asserts
that isolation; it no longer expects the historical CONNECTED-to-ERROR result.
The original operator report remains unverified against a real account.

## What was exercised

`backend/tests/test_planning_center_multi_type_integration.py` runs the real
`PlanningCenterClient` against `httpx.MockTransport`, through the real plugin,
event bus, state service and state store. It provides four active service types
and one archived type. Each active type returns a plan; one has no service time
in the search window and also returns an in-window rehearsal time. The remaining
plans span different local dates, including a UTC-next-day time that is still
the target date in America/Los_Angeles. Responses include pagination metadata
and null terminal next links. Loaded items include a header and linked song.

Twenty passing cases cover manual/OAuth authentication, forward/reversed service
type order, nearest-plan selection, cross-type ambiguity, saved title preference,
explicit candidate selection and no matching candidates. They verify archive
filtering, actual items/songs/state projection and a second refresh. All twenty
passed with the beta.22 discovery/resolution implementation unchanged. The
initial run also passed the negative control: 21 passed before diagnostic edits.
There is no failing-then-passing reproduction of the operator's reported bug.

## Traced paths and conclusions

- `plugin.py::_configured_service_types` filters archived/deleted types in all
  mode. `_load_plan_for_service_types` routes all mode to the multi-type client
  and leaves the single-type route separate.
- `client.py::load_plan_for_service_types` obtains candidates sequentially for
  every active type, then calls `_resolve_plan_candidates`. Each type uses the
  same `_plan_candidates_for_date` as the single-type route.
- `_plan_candidates_for_date` skips plans without matching service times before
  building candidates; generated candidates therefore always have service times.
- `_resolve_plan_candidates` filters to the nearest **local date**, sorts by time
  and plan ID, returns ambiguity for ties, and loads items only for the selected
  candidate. `None` service types in ambiguous/not-found results are explicitly
  permitted by the models and worked through plugin projection in these tests.
- Candidate IDs are not globally deduplicated across types. This is not evidence
  of the reported error: ties remain ambiguous, while duplicate IDs with explicit
  selection would select the first match rather than cause the logged safe error.
  No evidence of colliding real Planning Center Plan IDs was available.
- `_refresh_once` also runs `_preferred_candidate` / `resolve_selected_plan` for
  saved preferences. Both paths passed with four active types. A stale explicit
  selection can raise `PlanningCenterPlanSelectionError`, but that does not follow
  from merely selecting the all-types configuration.
- All HTTP calls succeeding does **not** prove discovery reached resolution.
  `_get_collection_page` validates each successful response before returning it.
  Invalid JSON/schema, duplicate IDs and pagination safety checks can still raise
  a safe `PlanningCenterResponseError` after HTTP 200. Projection errors have a
  separate unexpected-projection event, not just the generic safe warning.

## Hypothesis, not a confirmed root cause

The leading testable hypothesis is an API response shape rejected by the typed
boundary for an ancillary service type. All mode visits types that a working
single-type configuration never examines. A synthetic negative control with a
null plan title in the fourth type reproduces CONNECTED-to-ERROR behavior with
all mock HTTP responses 200, after querying all four types and before loading
items/resolving candidates. This proves the **failure mechanism is possible**,
not that null titles are valid upstream data or that this operator received one.
The public Planning Center Plan reference describes title as a string and does
not establish nullability, so no schema relaxation was made:
https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan

Other possible conditions are invalid plan-time fields, repeated collection IDs,
pagination metadata/link anomalies, or an unobserved timeout/429 on a later call.
A 429 requires a non-200 response, so it is less consistent if the supplied log
summary is exhaustive. Timing/ordering is not ruled out by mock-transport tests.

## Temporary diagnostic only

At `client.py::_get_collection_page`'s JSON/schema exception handler, this PR adds
`planning_center_response_validation_diagnostic` at warning level, with
`temporary_diagnostic=true`, the original exception class, resource kind and
actual HTTP status. This is marked temporary to remove after investigation.
It does not log exception messages, args, validation inputs, response bodies,
URLs, titles, credentials or account names. Two additional privacy tests cover
JSONDecodeError and ValidationError and assert the exact categorical payload.
The original error translation and all discovery/selection behavior are unchanged.

Companion task `t_2510543d` opened PR #80 (not merged at the final check):
https://github.com/tage-ilot/stagepilot-beta/pull/80
This PR deliberately avoids its `plugin.py` logging changes. Use its categorical
exception/service-type fields together with this response-stage diagnostic.

## Verification

From the isolated clean clone:

- `uv sync --project backend --frozen --extra dev`: succeeded.
- In `backend`, `uv run --frozen pytest -q`: **458 passed, 1 skipped**, one
  existing Starlette/httpx deprecation warning, 71.38 seconds. The skip is the
  Windows-only st_mode test.
- Initial lint found E731 in the new test's annotated lambda assignment; changed
  it to the same direct factory argument used by the other integration test.
- After that test-only edit, targeted pytest: **23 passed** in 0.51 seconds.
- `uv run --frozen ruff check .`: all checks passed.
- `uv run --frozen ruff format --check .`: 125 files already formatted.
- `uv run --frozen mypy`: no issues in 125 source files.
- `git diff --check`: clean.

## Operator review / next evidence

This is an investigation/diagnostics PR, not a claim that service loading is fixed.
Review alongside #80. If approved for a future diagnostic build, reproduce once
on the affected installation and retrieve the existing diagnostic bundle. Inspect
`exception_type` / `http_status` plus the temporary response event's `resource_kind`.
If response validation is confirmed, obtain a privacy-sanitized fixture through
an authorized path (preserve field types/nulls/relationships/pagination; remove
names and credentials), then create the real failing regression and narrowly
correct the supported response handling. Current PR #82 policy isolates failed
service types and logs safe warnings, while propagating an error if every type
fails. These mock tests do not establish that every production account scenario
is resolved. No release or tag was created by the original investigation run.
