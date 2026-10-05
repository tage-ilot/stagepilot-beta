"""Exercise real HTTP parsing, cross-type discovery and plugin state projection."""

from __future__ import annotations

import httpx
import pytest
from structlog.testing import capture_logs

from stagepilot.core.config import PlanningCenterSettings
from stagepilot.core.event_bus import EventBus
from stagepilot.core.events import EventType, ServicePlanSelectionPayload, new_event
from stagepilot.core.state import StateStore
from stagepilot.models.state import ConnectionStatus, ServiceLoadStatus
from stagepilot.plugins.planning_center.client import PlanningCenterClient
from stagepilot.plugins.planning_center.errors import PlanningCenterResponseError
from stagepilot.plugins.planning_center.plugin import ALL_SERVICE_TYPES_ID, PlanningCenterPlugin
from stagepilot.services.state_service import StateService
from test_planning_center_plan_discovery import (
    TARGET_DATE,
    TIMEZONE_NAME,
    JsonObject,
    item_resource,
    plan_resource,
    plan_time_resource,
)


class FourTypeApi:
    """Four active types plus an archived type, using JSON:API resource fixtures."""

    def __init__(self, scenario: str, *, reverse: bool = False) -> None:
        self.scenario = scenario
        self.active_ids = ["11", "22", "33", "44"]
        if reverse:
            self.active_ids.reverse()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        data: list[JsonObject]
        if path == "/services/v2/service_types":
            data = [
                {
                    "type": "ServiceType",
                    "id": identifier,
                    "attributes": {
                        "name": f"Service {identifier}",
                        "sequence": sequence,
                        "archived_at": "2026-01-01T00:00:00Z" if identifier == "55" else None,
                    },
                }
                for sequence, identifier in enumerate(["55", *self.active_ids])
            ]
        else:
            service_id = path.split("/")[4]
            assert service_id in self.active_ids, "Archived service type must not be queried"
            if path.endswith("/plans"):
                # Each active type has a plan; 33 has no matching service time.
                data = [plan_resource(f"plan-{service_id}", f"Plan {service_id}")]
                if self.scenario == "invalid_response" and service_id == "44":
                    data = [{"type": "Plan", "id": "plan-44", "attributes": {"title": None}}]
            elif path.endswith("/plan_times"):
                starts_at = {
                    "11": "2026-07-13T16:00:00Z",
                    # UTC Monday is still Sunday evening in Los Angeles.
                    "22": "2026-07-13T01:00:00Z",
                    "33": "2026-08-20T16:00:00Z",
                    "44": "2026-07-14T16:00:00Z",
                }[service_id]
                if self.scenario in {"tie", "preference", "selection"} and service_id == "11":
                    starts_at = "2026-07-12T16:00:00Z"
                if self.scenario == "not_found":
                    starts_at = "2026-08-20T16:00:00Z"
                data = [plan_time_resource(f"time-{service_id}", starts_at)]
                if service_id == "33":
                    data.append(
                        plan_time_resource(
                            "rehearsal-33", "2026-07-12T16:00:00Z", time_type="rehearsal"
                        )
                    )
            elif path.endswith("/items"):
                data = [
                    item_resource(f"header-{service_id}", "Opening", "header", 1),
                    item_resource(
                        f"song-{service_id}", "Test song", "song", 2, linked_song_id="song-source"
                    ),
                ]
            else:
                raise AssertionError(f"Unexpected request: {path}")
        # Deliberately include real pagination metadata and null terminal links.
        return httpx.Response(
            200,
            json={
                "data": data,
                "meta": {"total_count": len(data), "count": len(data)},
                "links": {"next": None},
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_method", ["manual", "oauth"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("scenario", ["nearest", "tie", "preference", "selection", "not_found"])
async def test_four_type_discovery_through_real_client_and_plugin(
    connection_method: str,
    reverse: bool,
    scenario: str,
) -> None:
    api = FourTypeApi(scenario, reverse=reverse)
    settings = PlanningCenterSettings(
        connection_method=connection_method,
        app_id="test-app-id",
        secret="test-secret",
        access_token="test-access-token",
        service_type_id=ALL_SERVICE_TYPES_ID,
        plan_title_preference="Plan 22" if scenario == "preference" else None,
        upcoming_lookahead_days=30,
    )
    client = PlanningCenterClient(settings, transport=httpx.MockTransport(api))
    event_bus = EventBus()
    state_store = StateStore()
    state_service = StateService(event_bus, state_store)
    plugin = PlanningCenterPlugin(
        event_bus,
        state_store,
        settings,
        timezone_name=TIMEZONE_NAME,
        client_factory=lambda _settings: client,
        today_provider=lambda _timezone: TARGET_DATE,
    )
    await state_service.start()
    try:
        await plugin.start()
        state = await state_store.snapshot()
        assert state.planning_center_status is ConnectionStatus.CONNECTED
        assert state.service_load.target_date == TARGET_DATE
        if scenario in {"tie", "selection"}:
            assert state.service_load.status is ServiceLoadStatus.AMBIGUOUS
            assert [candidate.id for candidate in state.service_load.candidates] == [
                "plan-11",
                "plan-22",
            ]
            assert state.plan is None
        elif scenario == "not_found":
            assert state.service_load.status is ServiceLoadStatus.NOT_FOUND
            assert state.plan is None
        else:
            assert state.service_load.status is ServiceLoadStatus.LOADED
            assert state.plan is not None and state.plan.id == "plan-22"
            assert state.plan.service_type_id == "22"
            assert [song.id for song in state.plan.songs] == ["song-22"]
            assert [item.item_id for item in state.service_load.skipped_items] == ["header-22"]
        queried_ids = [
            request.url.path.split("/")[4]
            for request in api.requests
            if request.url.path.endswith("/plans")
        ]
        assert queried_ids == api.active_ids
        if scenario == "selection":
            await event_bus.publish(
                new_event(
                    EventType.SERVICE_PLAN_SELECTION_REQUESTED,
                    source="test",
                    payload=ServicePlanSelectionPayload(plan_id="plan-22"),
                )
            )
            state = await state_store.snapshot()
            assert state.service_load.status is ServiceLoadStatus.LOADED
            assert state.plan is not None and state.plan.id == "plan-22"
        await plugin.refresh_now()
        refreshed = await state_store.snapshot()
        assert refreshed.planning_center_status is ConnectionStatus.CONNECTED
        assert refreshed.service_load.status in {
            ServiceLoadStatus.LOADED,
            ServiceLoadStatus.AMBIGUOUS,
            ServiceLoadStatus.NOT_FOUND,
        }
        assert (await plugin.health()).last_error is None
    finally:
        await plugin.stop()
        await state_service.stop()


@pytest.mark.asyncio
async def test_invalid_ancillary_type_response_preserves_valid_plan() -> None:
    """An invalid ancillary response must not undo the isolation shipped in PR82."""
    api = FourTypeApi("invalid_response")
    settings = PlanningCenterSettings(
        app_id="test-app-id",
        secret="test-secret",
        service_type_id=ALL_SERVICE_TYPES_ID,
    )
    client = PlanningCenterClient(settings, transport=httpx.MockTransport(api))
    event_bus = EventBus()
    state_store = StateStore()
    state_service = StateService(event_bus, state_store)
    plugin = PlanningCenterPlugin(
        event_bus,
        state_store,
        settings,
        timezone_name=TIMEZONE_NAME,
        client_factory=lambda _settings: client,
        today_provider=lambda _timezone: TARGET_DATE,
    )
    await state_service.start()
    try:
        await plugin.start()
        state = await state_store.snapshot()
        assert state.planning_center_status is ConnectionStatus.CONNECTED
        assert state.service_load.status is ServiceLoadStatus.LOADED
        assert state.plan is not None and state.plan.id == "plan-22"
        assert (await plugin.health()).last_error is None
        assert [
            request.url.path.split("/")[4]
            for request in api.requests
            if request.url.path.endswith("/plans")
        ] == api.active_ids
        assert [
            request.url.path for request in api.requests if request.url.path.endswith("/items")
        ] == ["/services/v2/service_types/22/plans/plan-22/items"]
    finally:
        await plugin.stop()
        await state_service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_json", [False, True])
async def test_temporary_response_diagnostic_never_logs_response_content(
    invalid_json: bool,
) -> None:
    private_text = "private-account-name-and-secret-value"

    def handler(_request: httpx.Request) -> httpx.Response:
        if invalid_json:
            return httpx.Response(200, text=private_text)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "ServiceType",
                        "id": "11",
                        "attributes": {"name": {"private": private_text}},
                    }
                ]
            },
        )

    settings = PlanningCenterSettings(app_id="test-app-id", secret="test-secret")
    with capture_logs() as logs:
        async with PlanningCenterClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PlanningCenterResponseError):
                await client.list_service_types()
    diagnostics = [
        entry
        for entry in logs
        if entry.get("event") == "planning_center_response_validation_diagnostic"
    ]
    assert diagnostics == [
        {
            "component": "planning_center",
            "event": "planning_center_response_validation_diagnostic",
            "temporary_diagnostic": True,
            "exception_type": "JSONDecodeError" if invalid_json else "ValidationError",
            "resource_kind": "service type",
            "http_status": 200,
            "log_level": "warning",
        }
    ]
    assert private_text not in repr(logs)
