"""FastAPI application factory and command-line entry point."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from stagepilot.api.access import router as access_router
from stagepilot.api.dashboard_auth import router as dashboard_auth_router
from stagepilot.api.dashboard_auth_middleware import DashboardAuthMiddleware
from stagepilot.api.remote_auth import router as remote_auth_router
from stagepilot.api.remote_feature import router as remote_feature_router
from stagepilot.api.remote_ingress import RemoteAccess
from stagepilot.api.routes import router as api_router
from stagepilot.api.websocket import router as websocket_router
from stagepilot.core.config import MidiSource, ServiceSource, Settings, TimerOutput, get_settings
from stagepilot.core.event_bus import EventBus
from stagepilot.core.events import EventType, new_event
from stagepilot.core.logging import configure_logging, get_logger
from stagepilot.core.plan_cache import (
    FilePlanCacheStore,
    MemoryPlanCacheStore,
    PlanCacheStore,
    default_plan_cache_path,
)
from stagepilot.core.plugin import PluginManager
from stagepilot.core.runtime import Runtime
from stagepilot.core.settings import (
    CredentialStore,
    MemoryCredentialStore,
    SettingsService,
    default_oauth_credential_store,
)
from stagepilot.core.state import StateStore
from stagepilot.planning_center_oauth import (
    ControlPlaneOAuthClient,
    OAuthTokenStore,
    PlanningCenterOAuthService,
    proactive_refresh_loop,
)
from stagepilot.plugins.demo import DemoPlugin
from stagepilot.plugins.lights import LightsPlugin, MidiOutputBackendFactory
from stagepilot.plugins.midi_playback import MidiBackendFactory, MidiPlaybackPlugin
from stagepilot.plugins.planning_center import (
    PlanningCenterClientFactory,
    PlanningCenterPlugin,
    TodayProvider,
)
from stagepilot.plugins.propresenter import ProPresenterClientFactory, ProPresenterPlugin
from stagepilot.services.dashboard_auth import DashboardSessionStore
from stagepilot.services.planning_center_setup import PlanningCenterSetupService
from stagepilot.services.remote_auth import RemoteStore
from stagepilot.services.startup_activation import StartupActivationService
from stagepilot.services.state_service import StateService


def default_web_root() -> Path | None:
    """Locate the compiled dashboard in development and packaged sidecars."""

    configured = os.environ.get("STAGEPILOT_WEB_ROOT")
    candidates = [
        Path(configured) if configured else None,
        Path(getattr(sys, "_MEIPASS", "")) / "stagepilot_web"
        if getattr(sys, "_MEIPASS", None)
        else None,
        Path(__file__).resolve().parents[3] / "frontend" / "dist",
    ]
    return next(
        (
            candidate
            for candidate in candidates
            if candidate is not None and (candidate / "index.html").is_file()
        ),
        None,
    )


def create_app(
    settings: Settings | None = None,
    *,
    planning_center_client_factory: PlanningCenterClientFactory | None = None,
    planning_center_today_provider: TodayProvider | None = None,
    midi_backend_factory: MidiBackendFactory | None = None,
    lights_backend_factory: MidiOutputBackendFactory | None = None,
    propresenter_client_factory: ProPresenterClientFactory | None = None,
    settings_service: SettingsService | None = None,
    dashboard_auth_enforced: bool | None = None,
    plan_cache_store: PlanCacheStore | None = None,
    remote_store: RemoteStore | None = None,
    oauth_credential_store: CredentialStore | None = None,
    oauth_control_plane: ControlPlaneOAuthClient | None = None,
    web_root: Path | None = None,
) -> FastAPI:
    """Create an independently testable StagePilot application instance."""

    desktop_remote_root = os.environ.get("STAGEPILOT_DESKTOP_REMOTE_ROOT")
    cloudflared_binary = os.environ.get("STAGEPILOT_CLOUDFLARED_BINARY")
    if remote_store is None and desktop_remote_root and cloudflared_binary:
        remote_store = RemoteStore(Path(desktop_remote_root) / "identity.sqlite3")

    resolved_settings_service = settings_service or (
        SettingsService.ephemeral(settings) if settings is not None else SettingsService.default()
    )
    resolved_settings = settings or resolved_settings_service.load()
    configure_logging(resolved_settings.log_level)
    logger = get_logger("application")
    event_bus = EventBus()
    state_store = StateStore()
    state_service = StateService(
        event_bus,
        state_store,
        recent_event_limit=resolved_settings.recent_event_limit,
        recent_error_limit=resolved_settings.recent_error_limit,
    )
    plugin_manager = PluginManager(event_bus)
    midi_plugin: MidiPlaybackPlugin | None = None
    planning_center_plugin: PlanningCenterPlugin | None = None
    propresenter_plugin: ProPresenterPlugin | None = None
    lights_plugin = LightsPlugin(
        event_bus,
        state_store,
        resolved_settings.lights,
        backend_factory=lights_backend_factory,
    )
    plugin_manager.register(lights_plugin)
    resolved_plan_cache_store = plan_cache_store or (
        MemoryPlanCacheStore()
        if settings is not None
        else FilePlanCacheStore(default_plan_cache_path())
    )

    if resolved_settings.integration_modes.service_source is ServiceSource.DEMO:
        plugin_manager.register(
            DemoPlugin(
                event_bus,
                state_store,
                simulate_midi=(
                    resolved_settings.integration_modes.midi_source is MidiSource.SIMULATED
                ),
                simulate_propresenter=(
                    resolved_settings.integration_modes.timer_output is TimerOutput.SIMULATED
                ),
            )
        )
    else:
        planning_center_plugin = PlanningCenterPlugin(
            event_bus,
            state_store,
            resolved_settings.planning_center,
            timezone_name=resolved_settings.timezone,
            client_factory=planning_center_client_factory,
            today_provider=planning_center_today_provider,
            plan_cache_store=resolved_plan_cache_store,
        )
        plugin_manager.register(planning_center_plugin)

    real_midi_enabled = (
        resolved_settings.midi.enabled
        and resolved_settings.integration_modes.midi_source is MidiSource.REAL
    )
    if real_midi_enabled:
        midi_plugin = MidiPlaybackPlugin(
            event_bus,
            state_store,
            resolved_settings.midi,
            state_service,
            backend_factory=midi_backend_factory,
        )
        plugin_manager.register(midi_plugin)

    real_propresenter_enabled = (
        resolved_settings.propresenter.enabled
        and resolved_settings.integration_modes.timer_output is TimerOutput.PROPRESENTER
    )
    if real_propresenter_enabled:
        propresenter_plugin = ProPresenterPlugin(
            event_bus,
            state_store,
            resolved_settings.propresenter,
            client_factory=propresenter_client_factory,
        )
        plugin_manager.register(propresenter_plugin)

    planning_center_oauth = PlanningCenterOAuthService(
        # The OAuth client_id is not a secret (only the client_secret is,
        # and that lives in the control-plane Worker), but it is a
        # product-wide registration value rather than something StagePilot
        # can invent, so it is supplied by the packaged build/environment.
        # With no client id configured, sign-in reports itself as
        # unavailable instead of building a broken authorize URL.
        client_id=os.environ.get("STAGEPILOT_PCO_CLIENT_ID", ""),
        tokens=OAuthTokenStore(
            oauth_credential_store
            or (
                MemoryCredentialStore()
                if settings is not None
                else default_oauth_credential_store()
            )
        ),
        control_plane=oauth_control_plane,
    )
    resolved_settings_service.set_access_token_provider(
        lambda: tokens.access_token if (tokens := planning_center_oauth.stored()) else None
    )

    runtime = Runtime(
        settings=resolved_settings,
        event_bus=event_bus,
        state_store=state_store,
        state_service=state_service,
        plugin_manager=plugin_manager,
        settings_service=resolved_settings_service,
        planning_center_setup=PlanningCenterSetupService(
            resolved_settings_service,
            client_factory=planning_center_client_factory,
        ),
        midi_controller=midi_plugin,
        propresenter_controller=propresenter_plugin,
        lights_controller=lights_plugin,
        planning_center=planning_center_plugin,
        planning_center_oauth=planning_center_oauth,
    )
    startup_activation = StartupActivationService(
        plugin_manager=plugin_manager,
        state_store=state_store,
        midi=midi_plugin,
        propresenter=propresenter_plugin,
        propresenter_settings=resolved_settings.propresenter,
        lights=lights_plugin,
        lights_settings=resolved_settings.lights,
        planning_center=planning_center_plugin,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("application_starting", version=resolved_settings.version)
        await state_service.start()
        await plugin_manager.start_all()
        await event_bus.publish(new_event(EventType.APPLICATION_STARTED, source="application"))
        logger.info("application_started", version=resolved_settings.version)
        activation_task = asyncio.create_task(
            _run_startup_activation(startup_activation),
            name="stagepilot-startup-activation",
        )
        # Proactive OAuth refresh: one lifespan-owned polling task, the
        # same shape as the managed-Remote reconcile loop, rather than a
        # new scheduler dependency.
        oauth_stop = asyncio.Event()
        oauth_task = asyncio.create_task(
            proactive_refresh_loop(planning_center_oauth, oauth_stop),
            name="stagepilot-planning-center-oauth-refresh",
        )
        try:
            yield
        finally:
            logger.info("application_stopping")
            activation_task.cancel()
            oauth_stop.set()
            oauth_task.cancel()
            with suppress(asyncio.CancelledError):
                await activation_task
            with suppress(asyncio.CancelledError):
                await oauth_task
            await event_bus.publish(new_event(EventType.APPLICATION_STOPPING, source="application"))
            await plugin_manager.stop_all()
            await state_service.stop()
            logger.info("application_stopped")

    application = FastAPI(
        title="StagePilot API",
        version=resolved_settings.version,
        lifespan=lifespan,
    )
    application.state.remote_access = RemoteAccess(remote_store) if remote_store else None
    application.state.runtime = runtime
    application.state.dashboard_sessions = DashboardSessionStore()
    application.state.dashboard_auth_enforced = (
        settings is None if dashboard_auth_enforced is None else dashboard_auth_enforced
    )
    application.add_middleware(DashboardAuthMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://tauri.localhost",
            "https://tauri.localhost",
            "tauri://localhost",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-StagePilot-Remote", "X-CSRF-Token", "Idempotency-Key"],
    )
    application.include_router(access_router)
    application.include_router(api_router)
    application.include_router(dashboard_auth_router)
    application.include_router(remote_auth_router)
    application.include_router(remote_feature_router)
    application.include_router(websocket_router)
    if desktop_remote_root and cloudflared_binary and remote_store is not None:
        from stagepilot.remote_desktop import DesktopRemoteManager, attach_desktop_remote

        manager = DesktopRemoteManager(
            Path(desktop_remote_root),
            Path(cloudflared_binary),
            revoke_sessions=lambda: remote_store.installation_generation(str(uuid4())),
            lan_port=resolved_settings.bind_port,
        )
        attach_desktop_remote(application, manager)
    dashboard_root = web_root or default_web_root()
    if dashboard_root is not None and (dashboard_root / "index.html").is_file():
        application.mount(
            "/",
            StaticFiles(directory=dashboard_root, html=True),
            name="dashboard",
        )
    return application


async def _run_startup_activation(service: StartupActivationService) -> None:
    """Let macOS and network integrations settle, then reconcile saved settings."""

    await asyncio.sleep(1)
    await service.run()


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "stagepilot.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.casefold(),
    )


if __name__ == "__main__":
    run()
