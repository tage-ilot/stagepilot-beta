"""Regression test for the beta.8 "settings not applied to live plugin" bug.

Saving Planning Center settings with an unchanged App ID/Secret (so
`restart_required` is False) must actually reach the already-running
plugin instance -- not just persist to disk. Before the fix, the route
never touched `runtime.planning_center`, so the live plugin kept using its
stale in-memory settings/client and `GET /planning-center/status` kept
reporting the old, wrong state even though the POST returned 200.

This test exercises the exact scenario the operator hit on live beta.8:
save settings against a RUNNING backend with the App ID/Secret unchanged
but the service type changed, and assert the live connection/plan
reflects the new service type without any process restart.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from stagepilot.core.config import (
    IntegrationModes,
    PlanningCenterSettings,
    ServiceSource,
    Settings,
)
from stagepilot.main import create_app
from stagepilot.models.state import ServicePlan, Song
from stagepilot.plugins.planning_center.models import (
    PlanDiscoveryResult,
    PlanLoadedResult,
    PlanningCenterPlanCandidate,
    PlanningCenterServiceType,
)

APP_ID = "operator-app-id"
SECRET = "operator-secret"
OLD_SERVICE_TYPE = "old-service-type"
NEW_SERVICE_TYPE = "new-service-type"


def _plan(service_type_id: str, plan_id: str, target_date: date) -> PlanLoadedResult:
    return PlanLoadedResult(
        candidate=PlanningCenterPlanCandidate(
            id=plan_id,
            title=f"Plan {plan_id}",
            service_type_id=service_type_id,
            service_type_name=f"Service {service_type_id}",
            target_date=target_date,
            service_times=[
                datetime(target_date.year, target_date.month, target_date.day, 9, tzinfo=UTC),
            ],
        ),
        plan=ServicePlan(
            id=plan_id,
            title=f"Plan {plan_id}",
            date=target_date,
            service_type=f"Service {service_type_id}",
            service_type_id=service_type_id,
            service_times=["09:00"],
            duration_source="Planning Center scheduled item length",
            songs=[Song(id="song-a", title="Song A", duration_seconds=240, order=1)],
        ),
        skipped_items=[],
    )


class LiveClient:
    """A fake Planning Center HTTP client bound to one service_type_id."""

    def __init__(self, service_type_id: str) -> None:
        self.service_type_id = service_type_id
        self.closed = False

    async def list_service_types(self) -> list[PlanningCenterServiceType]:
        return [
            PlanningCenterServiceType(
                id=self.service_type_id,
                name=f"Service {self.service_type_id}",
                sequence=1,
            )
        ]

    async def load_plan_for_date(
        self,
        service_type: PlanningCenterServiceType,
        target_date: date,
        _timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        return _plan(service_type.id, f"plan-{service_type.id}", target_date)

    async def load_plan_for_service_types(
        self,
        service_types: list[PlanningCenterServiceType],
        target_date: date,
        _timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        service_type = service_types[0]
        return _plan(service_type.id, f"plan-{service_type.id}", target_date)

    async def close(self) -> None:
        self.closed = True


class LiveClientFactory:
    """Builds a client bound to whatever service_type_id the settings carry."""

    def __init__(self) -> None:
        self.built_settings: list[PlanningCenterSettings] = []
        self.built_clients: list[LiveClient] = []

    def __call__(self, settings: PlanningCenterSettings) -> LiveClient:
        self.built_settings.append(settings)
        client = LiveClient(settings.service_type_id or OLD_SERVICE_TYPE)
        self.built_clients.append(client)
        return client


def test_settings_save_with_unchanged_credentials_applies_live_without_restart() -> None:
    factory = LiveClientFactory()
    settings = Settings(
        integration_modes=IntegrationModes(service_source=ServiceSource.PLANNING_CENTER),
        planning_center=PlanningCenterSettings(
            app_id=APP_ID,
            secret=SECRET,
            service_type_id=OLD_SERVICE_TYPE,
        ),
    )
    app = create_app(settings, planning_center_client_factory=factory)

    with TestClient(app) as client:
        status_before = client.get("/api/v1/planning-center/status")
        assert status_before.status_code == 200
        assert status_before.json()["service_type_id"] == OLD_SERVICE_TYPE

        # Save with the App ID/Secret left unchanged (blank secret means
        # "leave blank to keep"), only the service type changed -- the exact
        # scenario that silently no-op'd on live beta.8.
        response = client.post(
            "/api/v1/planning-center/settings",
            json={
                "app_id": APP_ID,
                "service_type_id": NEW_SERVICE_TYPE,
            },
        )
        assert response.status_code == 200
        body = response.json()

        # restart_required must genuinely stay False for this restart-exempt
        # change, and the reconfigure must have actually happened.
        assert body["restart_required"] is False

        status_after = client.get("/api/v1/planning-center/status")
        assert status_after.status_code == 200
        status_body = status_after.json()
        assert status_body["service_type_id"] == NEW_SERVICE_TYPE
        assert status_body["connection_status"] == "connected"

        state = client.get("/api/v1/state").json()
        assert state["plan"]["id"] == f"plan-{NEW_SERVICE_TYPE}"
        assert state["plan"]["service_type_id"] == NEW_SERVICE_TYPE

    # A fresh client was built for the new settings, and the stale one from
    # backend startup was actually replaced/closed rather than lingering.
    assert factory.built_settings[-1].service_type_id == NEW_SERVICE_TYPE
    assert len(factory.built_clients) >= 2
    assert factory.built_clients[0].closed is True
