"""Planning Center onboarding operations that are available before plugin startup."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from pydantic import SecretStr

from stagepilot.core.config import PlanningCenterSettings
from stagepilot.core.settings import SettingsService
from stagepilot.plugins.planning_center import (
    PlanningCenterClient,
    PlanningCenterClientFactory,
    PlanningCenterServiceType,
)
from stagepilot.plugins.planning_center.errors import PlanningCenterError

if TYPE_CHECKING:
    from stagepilot.planning_center_oauth import PlanningCenterOAuthService


class PlanningCenterSetupService:
    """Test credentials and discover service types without starting the plugin."""

    def __init__(
        self,
        settings_service: SettingsService,
        *,
        client_factory: PlanningCenterClientFactory | None = None,
        oauth_service: PlanningCenterOAuthService | None = None,
    ) -> None:
        self._settings_service = settings_service
        self._client_factory = client_factory or PlanningCenterClient
        self._oauth_service = oauth_service

    async def test_connection(
        self,
        *,
        app_id: str | None = None,
        secret: SecretStr | None = None,
    ) -> list[PlanningCenterServiceType]:
        """Authenticate and return active service types using temporary overrides."""

        settings = self._connection_settings(app_id=app_id, secret=secret)
        return await self._list_service_types(settings)

    async def list_service_types(self) -> list[PlanningCenterServiceType]:
        """Return active service types using the effective saved configuration."""

        # An OAuth access token cached from an earlier request can have
        # expired since it was resolved. Force a refresh (a no-op if the
        # cached token is still valid) before reading the effective
        # settings, so this request never fires with a stale Bearer token
        # when a valid refresh token could have avoided the 401 entirely.
        await self._ensure_fresh_oauth_token()
        settings = self._settings_service.effective_runtime_settings().planning_center
        return await self._list_service_types(settings)

    async def _ensure_fresh_oauth_token(self) -> None:
        current = self._settings_service.effective_runtime_settings().planning_center
        if current.connection_method != "oauth" or self._oauth_service is None:
            return
        with suppress(Exception):
            await self._oauth_service.valid_access_token()
        self._settings_service.refresh_access_token()

    def _connection_settings(
        self,
        *,
        app_id: str | None,
        secret: SecretStr | None,
    ) -> PlanningCenterSettings:
        current = self._settings_service.effective_runtime_settings().planning_center
        updates: dict[str, object] = {}
        if app_id is not None:
            updates["app_id"] = SecretStr(app_id)
        if secret is not None:
            updates["secret"] = secret
        return current.model_copy(update=updates, deep=True)

    async def _list_service_types(
        self,
        settings: PlanningCenterSettings,
    ) -> list[PlanningCenterServiceType]:
        try:
            client = self._client_factory(settings)
        except PlanningCenterError:
            raise
        except Exception:
            raise PlanningCenterError(
                "Planning Center connection setup failed unexpectedly."
            ) from None

        try:
            service_types = await client.list_service_types()
        except PlanningCenterError:
            raise
        except Exception:
            raise PlanningCenterError(
                "Planning Center service-type discovery failed unexpectedly."
            ) from None
        finally:
            with suppress(Exception):
                await client.close()

        return [service_type for service_type in service_types if not service_type.archived]
