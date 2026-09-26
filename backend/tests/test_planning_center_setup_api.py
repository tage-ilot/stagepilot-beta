from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from stagepilot.core.config import PlanningCenterSettings, Settings
from stagepilot.main import create_app
from stagepilot.plugins.planning_center.errors import PlanningCenterAuthenticationError
from stagepilot.plugins.planning_center.models import (
    PlanDiscoveryResult,
    PlanningCenterServiceType,
)


class SetupClient:
    def __init__(
        self,
        *,
        error: Exception | None = None,
    ) -> None:
        self.error = error
        self.closed = False

    async def list_service_types(self) -> list[PlanningCenterServiceType]:
        if self.error is not None:
            raise self.error
        return [
            PlanningCenterServiceType(
                id="sunday",
                name="Sunday Morning",
                sequence=1,
            ),
            PlanningCenterServiceType(
                id="archived",
                name="Archived Service",
                sequence=2,
                archived=True,
            ),
        ]

    async def load_plan_for_date(
        self,
        _service_type: PlanningCenterServiceType,
        _target_date: date,
        _timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        raise AssertionError("Plan loading is not used during onboarding.")

    async def load_plan_for_service_types(
        self,
        _service_types: list[PlanningCenterServiceType],
        _target_date: date,
        _timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        raise AssertionError("Plan loading is not used during onboarding.")

    async def close(self) -> None:
        self.closed = True


class SetupFactory:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.settings: list[PlanningCenterSettings] = []
        self.clients: list[SetupClient] = []

    def __call__(self, settings: PlanningCenterSettings) -> SetupClient:
        self.settings.append(settings)
        client = SetupClient(error=self.error)
        self.clients.append(client)
        return client


def test_temporary_credentials_are_tested_without_being_returned_or_saved() -> None:
    factory = SetupFactory()
    app = create_app(Settings(), planning_center_client_factory=factory)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/planning-center/test",
            json={"app_id": "temporary-app-id", "secret": "temporary-secret"},
        )
        settings = client.get("/api/v1/settings")

    assert response.status_code == 200
    assert response.json() == {
        "authenticated": True,
        "message": "Planning Center authentication succeeded.",
        "service_types": [{"id": "sunday", "name": "Sunday Morning"}],
    }
    assert "temporary-secret" not in response.text
    assert settings.json()["settings"]["planning_center"]["app_id"] is None
    assert settings.json()["planning_center_secret_saved"] is False
    assert factory.settings[0].credentials() == (
        "temporary-app-id",
        "temporary-secret",
    )
    assert factory.clients[0].closed is True


def test_saved_credentials_are_used_to_list_active_service_types() -> None:
    factory = SetupFactory()
    app = create_app(
        Settings(
            planning_center=PlanningCenterSettings(
                app_id="saved-app-id",
                secret="saved-secret",
            )
        ),
        planning_center_client_factory=factory,
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/planning-center/service-types")

    assert response.status_code == 200
    assert response.json() == [{"id": "sunday", "name": "Sunday Morning"}]
    assert factory.settings[0].credentials() == ("saved-app-id", "saved-secret")
    assert factory.clients[0].closed is True


def test_authentication_failure_is_safe_and_uses_401() -> None:
    factory = SetupFactory(
        error=PlanningCenterAuthenticationError(
            "Planning Center rejected the configured application ID or secret."
        )
    )
    app = create_app(Settings(), planning_center_client_factory=factory)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/planning-center/test",
            json={"app_id": "bad-app-id", "secret": "do-not-return-this"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == (
        "Planning Center rejected the configured application ID or secret."
    )
    assert "do-not-return-this" not in response.text
    assert factory.clients[0].closed is True


def test_service_type_listing_requires_complete_credentials() -> None:
    app = create_app(Settings())

    with TestClient(app) as client:
        response = client.get("/api/v1/planning-center/service-types")

    assert response.status_code == 409
    assert response.json()["detail"] == "Planning Center credentials are not configured."
