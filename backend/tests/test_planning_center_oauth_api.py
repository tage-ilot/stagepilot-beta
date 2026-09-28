"""HTTP surface for the Planning Center OAuth sign-in flow."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import stagepilot.main as main_module
from stagepilot.core.config import (
    IntegrationModes,
    PlanningCenterSettings,
    ServiceSource,
    Settings,
)
from stagepilot.core.settings import (
    MemoryCredentialStore,
    MemorySettingsStore,
    PersistentPlanningCenterSettings,
    PersistentSettings,
    SettingsService,
)
from stagepilot.main import create_app
from stagepilot.planning_center_oauth import ControlPlaneOAuthClient, OAuthTokens
from stagepilot.plugins.planning_center.models import (
    PlanDiscoveryResult,
    PlanningCenterPlanCandidate,
    PlanningCenterServiceType,
    PlanNotFoundResult,
)

FLOW = {"flow_id": "aabbccdd", "ticket": "ticket-1"}
TOKENS = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "expires_in": 7200,
    "scope": "services",
    "token_type": "Bearer",
}


def app_with(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    store: MemoryCredentialStore,
) -> FastAPI:
    monkeypatch.setenv("STAGEPILOT_PCO_CLIENT_ID", "client-id-1")
    # A mock transport so no real control-plane call is ever made here.
    return create_app(
        Settings(),
        oauth_credential_store=store,
        oauth_control_plane=ControlPlaneOAuthClient(transport=httpx.MockTransport(handler)),
    )


def control_plane_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/flow"):
        return httpx.Response(201, json=FLOW)
    return httpx.Response(200, json=TOKENS)


class RecordingPlanningCenterClient:
    def __init__(self, bearer_token: str | None) -> None:
        self.bearer_token = bearer_token
        self.request_tokens: list[str | None] = []

    async def list_service_types(self) -> list[PlanningCenterServiceType]:
        self.request_tokens.append(self.bearer_token)
        return [PlanningCenterServiceType(id="42", name="Weekend Services", sequence=0)]

    async def load_plan_for_date(
        self,
        service_type: PlanningCenterServiceType,
        target_date: date,
        timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        return PlanNotFoundResult(service_type=service_type, target_date=target_date)

    async def load_plan_for_service_types(
        self,
        service_types: list[PlanningCenterServiceType],
        target_date: date,
        timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        return PlanNotFoundResult(service_type=service_types[0], target_date=target_date)

    async def resolve_selected_plan(
        self,
        candidates: list[PlanningCenterPlanCandidate],
        service_types: list[PlanningCenterServiceType],
        target_date: date,
        *,
        selected_plan_id: str,
    ) -> PlanDiscoveryResult:
        return PlanNotFoundResult(service_type=service_types[0], target_date=target_date)

    async def close(self) -> None:
        return None


def test_sign_in_round_trip_marks_the_installation_as_oauth_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryCredentialStore()
    application = app_with(monkeypatch, control_plane_handler, store)
    with TestClient(application) as client:
        started = client.post("/api/v1/planning-center/oauth/start")
        assert started.status_code == 200
        state = started.json()["state"]
        assert started.json()["authorize_url"].startswith(
            "https://api.planningcenteronline.com/oauth/authorize?"
        )
        assert "scope=services" in started.json()["authorize_url"]

        completed = client.post(
            "/api/v1/planning-center/oauth/callback",
            json={
                "state": state,
                "code": "the-code",
                "redirect_uri": "http://127.0.0.1:52847/callback",
            },
        )
        assert completed.status_code == 200
        assert completed.json()["connected"] is True
        assert completed.json()["connection_method"] == "oauth"
        assert completed.json()["needs_reconnect"] is False

        status = client.get("/api/v1/planning-center/status")
        assert status.status_code == 200
        assert status.json()["connection_method"] == "oauth"
        assert status.json()["oauth_connected"] is True

    # Secrets never leave through the API surface.
    assert "access-1" not in "\n".join((started.text, completed.text, status.text))
    assert store.secret is not None
    assert json.loads(store.secret)["refresh_token"] == "refresh-1"


def test_a_mismatched_callback_state_is_rejected_with_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryCredentialStore()
    application = app_with(monkeypatch, control_plane_handler, store)
    with TestClient(application) as client:
        client.post("/api/v1/planning-center/oauth/start")
        response = client.post(
            "/api/v1/planning-center/oauth/callback",
            json={
                "state": "not-the-state",
                "code": "the-code",
                "redirect_uri": "http://127.0.0.1:52847/callback",
            },
        )
    assert response.status_code == 401
    assert store.secret is None


def test_a_transient_control_plane_failure_returns_503_not_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blip must not be reported as "your connection is dead"."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json=FLOW)
        return httpx.Response(503, json={"error": "planning center unavailable"})

    store = MemoryCredentialStore()
    application = app_with(monkeypatch, handler, store)
    with TestClient(application) as client:
        state = client.post("/api/v1/planning-center/oauth/start").json()["state"]
        response = client.post(
            "/api/v1/planning-center/oauth/callback",
            json={
                "state": state,
                "code": "the-code",
                "redirect_uri": "http://127.0.0.1:52847/callback",
            },
        )
    assert response.status_code == 503


def test_disconnect_restores_the_manual_path_without_touching_the_pat_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryCredentialStore(
        OAuthTokens(
            access_token="a", refresh_token="r", expires_at=time.time() + 7200
        ).model_dump_json()
    )
    application = app_with(monkeypatch, control_plane_handler, store)
    with TestClient(application) as client:
        response = client.post("/api/v1/planning-center/oauth/disconnect")
        status = client.get("/api/v1/planning-center/oauth/status")
    assert response.status_code == 200
    assert response.json()["connection_method"] == "manual"
    assert response.json()["connected"] is False
    assert status.json()["connection_method"] == "manual"
    assert store.secret is None


def test_restart_refreshes_an_expired_stored_oauth_token_before_runtime_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overnight restart restores and refreshes OAuth without another sign-in."""

    monkeypatch.setenv("STAGEPILOT_PCO_CLIENT_ID", "client-id-1")
    settings_store = MemorySettingsStore(
        PersistentSettings(
            integration_modes=IntegrationModes(service_source=ServiceSource.PLANNING_CENTER),
            planning_center=PersistentPlanningCenterSettings(
                connection_method="oauth",
                service_type_id="42",
            ),
        )
    )
    oauth_store = MemoryCredentialStore(
        OAuthTokens(
            access_token="expired-access",
            refresh_token="persisted-refresh",
            expires_at=time.time() - 60,
        ).model_dump_json()
    )
    settings_service = SettingsService(settings_store, MemoryCredentialStore(), environ={})
    planning_clients: list[RecordingPlanningCenterClient] = []

    def planning_center_client_factory(
        settings: PlanningCenterSettings,
    ) -> RecordingPlanningCenterClient:
        client = RecordingPlanningCenterClient(settings.bearer_token())
        planning_clients.append(client)
        return client

    application = create_app(
        settings_service=settings_service,
        dashboard_auth_enforced=False,
        oauth_credential_store=oauth_store,
        oauth_control_plane=ControlPlaneOAuthClient(
            transport=httpx.MockTransport(control_plane_handler)
        ),
        planning_center_client_factory=planning_center_client_factory,
    )
    with TestClient(application) as client:
        status = client.get("/api/v1/planning-center/status")
        runtime_settings = (
            application.state.runtime.settings_service.effective_runtime_settings().planning_center
        )
        planning_plugin = application.state.runtime.planning_center

    assert status.status_code == 200
    assert status.json()["connection_method"] == "oauth"
    assert status.json()["oauth_connected"] is True
    assert runtime_settings.bearer_token() == "access-1"
    assert planning_plugin is not None
    assert planning_plugin._settings.bearer_token() == "access-1"
    assert len(planning_clients) == 1
    assert planning_clients[0].request_tokens == ["access-1"]
    assert oauth_store.secret is not None
    refreshed = json.loads(oauth_store.secret)
    assert refreshed["access_token"] == "access-1"
    assert refreshed["refresh_token"] == "refresh-1"


@pytest.mark.asyncio
async def test_startup_bounds_a_slow_oauth_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STAGEPILOT_PCO_CLIENT_ID", "client-id-1")
    monkeypatch.setattr(main_module, "OAUTH_STARTUP_REFRESH_TIMEOUT_SECONDS", 0.01)
    refresh_cancelled = asyncio.Event()

    async def slow_control_plane(_request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            refresh_cancelled.set()
            raise
        raise AssertionError("unreachable")

    settings_store = MemorySettingsStore(
        PersistentSettings(
            integration_modes=IntegrationModes(service_source=ServiceSource.PLANNING_CENTER),
            planning_center=PersistentPlanningCenterSettings(
                connection_method="oauth",
                service_type_id="42",
            ),
        )
    )
    oauth_store = MemoryCredentialStore(
        OAuthTokens(
            access_token="expired-access",
            refresh_token="persisted-refresh",
            expires_at=time.time() - 60,
        ).model_dump_json()
    )
    settings_service = SettingsService(settings_store, MemoryCredentialStore(), environ={})
    application = create_app(
        settings_service=settings_service,
        dashboard_auth_enforced=False,
        oauth_credential_store=oauth_store,
        oauth_control_plane=ControlPlaneOAuthClient(
            transport=httpx.MockTransport(slow_control_plane)
        ),
        planning_center_client_factory=lambda settings: RecordingPlanningCenterClient(
            settings.bearer_token()
        ),
    )

    async with application.router.lifespan_context(application):
        assert refresh_cancelled.is_set()


def test_sign_in_is_unavailable_without_a_configured_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STAGEPILOT_PCO_CLIENT_ID", raising=False)
    application = create_app(Settings(), oauth_credential_store=MemoryCredentialStore())
    with TestClient(application) as client:
        response = client.post("/api/v1/planning-center/oauth/start")
        status = client.get("/api/v1/planning-center/oauth/status")
    assert response.status_code == 503
    assert status.status_code == 200
    assert status.json()["connected"] is False


def test_existing_manual_installations_report_the_manual_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additive: nothing changes for a PAT-connected installation."""

    monkeypatch.delenv("STAGEPILOT_PCO_CLIENT_ID", raising=False)
    application = create_app(Settings(), oauth_credential_store=MemoryCredentialStore())
    with TestClient(application) as client:
        status = client.get("/api/v1/planning-center/status")
    assert status.status_code == 200
    assert status.json()["connection_method"] == "manual"
    assert status.json()["oauth_connected"] is False
    assert status.json()["oauth_needs_reconnect"] is False
