"""Regression: listing Planning Center service types refreshes an expired
OAuth access token before firing the request, instead of sending the stale
cached Bearer token and letting Planning Center reject it with a 401.
"""

from __future__ import annotations

import time
from datetime import date

import httpx
import pytest

from stagepilot.core.config import PlanningCenterSettings, Settings
from stagepilot.core.settings import MemoryCredentialStore, SettingsService
from stagepilot.planning_center_oauth import (
    ControlPlaneOAuthClient,
    OAuthTokens,
    OAuthTokenStore,
    PlanningCenterOAuthService,
)
from stagepilot.plugins.planning_center.models import (
    PlanDiscoveryResult,
    PlanningCenterPlanCandidate,
    PlanningCenterServiceType,
)
from stagepilot.services.planning_center_setup import PlanningCenterSetupService

CLIENT_ID = "test-client-id"


class RecordingClient:
    """A fake Planning Center client that records the Bearer token it saw."""

    def __init__(self, settings: PlanningCenterSettings) -> None:
        self.settings = settings

    async def list_service_types(self) -> list[PlanningCenterServiceType]:
        return [PlanningCenterServiceType(id="sunday", name="Sunday Morning", sequence=1)]

    async def load_plan_for_date(
        self,
        _service_type: PlanningCenterServiceType,
        _target_date: date,
        _timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        raise AssertionError("Plan loading is not used by the setup service.")

    async def load_plan_for_service_types(
        self,
        _service_types: list[PlanningCenterServiceType],
        _target_date: date,
        _timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        raise AssertionError("Plan loading is not used by the setup service.")

    async def resolve_selected_plan(
        self,
        _candidates: list[PlanningCenterPlanCandidate],
        _service_types: list[PlanningCenterServiceType],
        _target_date: date,
        *,
        selected_plan_id: str,
    ) -> PlanDiscoveryResult:
        raise AssertionError("Plan loading is not used by the setup service.")

    async def close(self) -> None:
        pass


class RecordingFactory:
    def __init__(self) -> None:
        self.calls: list[PlanningCenterSettings] = []

    def __call__(self, settings: PlanningCenterSettings) -> RecordingClient:
        self.calls.append(settings)
        return RecordingClient(settings)


def oauth_service_with_stored_tokens(
    *, access_token: str, refresh_token: str, expires_at: float, refreshed_access_token: str
) -> tuple[PlanningCenterOAuthService, OAuthTokenStore]:
    store = OAuthTokenStore(MemoryCredentialStore())
    store.save(
        OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json={"flow_id": "flow-1", "ticket": "ticket-1"})
        return httpx.Response(
            200,
            json={
                "access_token": refreshed_access_token,
                "refresh_token": "refresh-2",
                "expires_in": 7200,
                "scope": "services",
            },
        )

    control_plane = ControlPlaneOAuthClient(transport=httpx.MockTransport(handler))
    oauth = PlanningCenterOAuthService(
        client_id=CLIENT_ID, tokens=store, control_plane=control_plane
    )
    return oauth, store


def settings_service_for_oauth(oauth: PlanningCenterOAuthService) -> SettingsService:
    settings = Settings(
        planning_center=PlanningCenterSettings(connection_method="oauth"),
    )
    service = SettingsService.ephemeral(settings)

    def provider() -> str | None:
        tokens = oauth.stored()
        return tokens.access_token if tokens is not None and not tokens.needs_reconnect else None

    service.set_access_token_provider(provider)
    return service


@pytest.mark.asyncio
async def test_expired_oauth_access_token_is_refreshed_before_listing_service_types() -> None:
    now = time.time()
    oauth, store = oauth_service_with_stored_tokens(
        access_token="expired-access-token",
        refresh_token="refresh-1",
        # Already within the refresh margin (in fact already expired).
        expires_at=now - 10,
        refreshed_access_token="fresh-access-token",
    )
    settings_service = settings_service_for_oauth(oauth)
    factory = RecordingFactory()
    setup = PlanningCenterSetupService(
        settings_service, client_factory=factory, oauth_service=oauth
    )

    service_types = await setup.list_service_types()

    assert [item.id for item in service_types] == ["sunday"]
    # The request must have been made with the freshly refreshed token, not
    # the expired one that was cached when the SettingsService started.
    assert factory.calls[0].bearer_token() == "fresh-access-token"
    stored = store.load()
    assert stored is not None
    assert stored.access_token == "fresh-access-token"


@pytest.mark.asyncio
async def test_valid_oauth_access_token_is_not_refreshed() -> None:
    now = time.time()
    oauth, store = oauth_service_with_stored_tokens(
        access_token="still-valid-access-token",
        refresh_token="refresh-1",
        # Comfortably outside the refresh margin.
        expires_at=now + 7200,
        refreshed_access_token="should-not-be-used",
    )
    settings_service = settings_service_for_oauth(oauth)
    factory = RecordingFactory()
    setup = PlanningCenterSetupService(
        settings_service, client_factory=factory, oauth_service=oauth
    )

    await setup.list_service_types()

    assert factory.calls[0].bearer_token() == "still-valid-access-token"
    stored = store.load()
    assert stored is not None
    assert stored.access_token == "still-valid-access-token"


@pytest.mark.asyncio
async def test_manual_pat_connections_are_unaffected_by_oauth_refresh_logic() -> None:
    factory = RecordingFactory()
    settings_service = SettingsService.ephemeral(
        Settings(
            planning_center=PlanningCenterSettings(app_id="app-id", secret="secret"),
        )
    )
    setup = PlanningCenterSetupService(settings_service, client_factory=factory, oauth_service=None)

    await setup.list_service_types()

    assert factory.calls[0].credentials() == ("app-id", "secret")
