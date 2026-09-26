"""Production Planning Center plugin lifecycle and plan refresh orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from stagepilot.core.actions import ActionOutcome
from stagepilot.core.config import PlanningCenterSettings
from stagepilot.core.event_bus import EventBus, Subscription
from stagepilot.core.events import (
    ConnectionPayload,
    EventType,
    ServiceLoadPayload,
    ServicePayload,
    ServicePlanSelectionPayload,
    StagePilotEvent,
    new_event,
)
from stagepilot.core.logging import get_logger
from stagepilot.core.plan_cache import (
    CachedServicePlan,
    MemoryPlanCacheStore,
    PlanCacheError,
    PlanCacheStore,
)
from stagepilot.core.plugin import Plugin
from stagepilot.core.state import StateStore
from stagepilot.models.state import (
    ApplicationState,
    ConnectionStatus,
    PluginHealth,
    PluginStatus,
    ServiceLoadStatus,
    ServicePlan,
    ServicePlanCandidate,
    SkippedServiceItem,
)
from stagepilot.plugins.planning_center.client import PlanningCenterClient
from stagepilot.plugins.planning_center.errors import (
    PlanningCenterConfigurationError,
    PlanningCenterError,
    PlanningCenterPlanSelectionError,
)
from stagepilot.plugins.planning_center.models import (
    PlanAmbiguousResult,
    PlanDiscoveryResult,
    PlanLoadedResult,
    PlanningCenterPlanCandidate,
    PlanningCenterServiceType,
    PlanNotFoundResult,
)

ALL_SERVICE_TYPES_ID = "stagepilot:all-service-types"


class PlanningCenterClientContract(Protocol):
    async def list_service_types(self) -> list[PlanningCenterServiceType]: ...

    async def load_plan_for_date(
        self,
        service_type: PlanningCenterServiceType,
        target_date: date,
        timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult: ...

    async def load_plan_for_service_types(
        self,
        service_types: list[PlanningCenterServiceType],
        target_date: date,
        timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult: ...

    async def close(self) -> None: ...


type PlanningCenterClientFactory = Callable[[PlanningCenterSettings], PlanningCenterClientContract]
type TodayProvider = Callable[[ZoneInfo], date]


def _local_today(timezone: ZoneInfo) -> date:
    return datetime.now(timezone).date()


class PlanningCenterPlugin(Plugin):
    """Load and safely refresh the configured Planning Center service plan."""

    name = "planning_center"
    version = "0.1.0"

    def __init__(
        self,
        event_bus: EventBus,
        state_store: StateStore,
        settings: PlanningCenterSettings,
        *,
        timezone_name: str,
        client_factory: PlanningCenterClientFactory | None = None,
        today_provider: TodayProvider | None = None,
        plan_cache_store: PlanCacheStore | None = None,
    ) -> None:
        super().__init__(event_bus, state_store)
        self._settings = settings
        self._timezone_name = timezone_name
        self._timezone = ZoneInfo(timezone_name)
        self._client_factory = client_factory or PlanningCenterClient
        self._today_provider = today_provider or _local_today
        self._plan_cache_store = plan_cache_store or MemoryPlanCacheStore()
        self._cache_warning: str | None = None
        self._client: PlanningCenterClientContract | None = None
        self._subscriptions: list[Subscription] = []
        self._active_refresh_task: asyncio.Task[None] | None = None
        self._pending_selection_id: str | None = None
        self._pending_reload = False
        self._status = PluginStatus.STOPPED
        self._last_error: str | None = None
        self._last_activity_at: datetime | None = None
        self._stopping = False
        self._logger = get_logger(self.name)

    async def start(self) -> None:
        self._status = PluginStatus.STARTING
        self._stopping = False
        try:
            await self._restore_cached_plan()
            self._validate_configuration()
            self._client = self._client_factory(self._settings)
            self._subscriptions.extend(
                [
                    await self.event_bus.subscribe(
                        EventType.SERVICE_RELOAD_REQUESTED,
                        self._on_reload_requested,
                    ),
                    await self.event_bus.subscribe(
                        EventType.SERVICE_PLAN_SELECTION_REQUESTED,
                        self._on_plan_selection_requested,
                    ),
                ]
            )
        except PlanningCenterError as exc:
            await self._handle_start_failure(self._with_cache_warning(str(exc)))
            raise
        except Exception:
            detail = "Planning Center plugin initialization failed unexpectedly."
            self._logger.error("planning_center_unexpected_initialization_failure")
            await self._handle_start_failure(detail)
            raise PlanningCenterError(detail) from None

        self._status = PluginStatus.RUNNING
        task = asyncio.create_task(self._run_scheduled_refresh())
        self._active_refresh_task = task
        await task

    async def stop(self) -> None:
        if self._status is PluginStatus.STOPPED and self._client is None:
            return
        self._status = PluginStatus.STOPPING
        self._stopping = True
        for subscription in self._subscriptions:
            await self.event_bus.unsubscribe(subscription)
        self._subscriptions.clear()

        task = self._active_refresh_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._active_refresh_task = None
        self._pending_selection_id = None
        self._pending_reload = False

        close_error: Exception | None = None
        try:
            await self._close_client()
        except Exception as exc:  # shutdown must complete even if a transport close fails
            close_error = exc
        finally:
            await self._publish_connection(
                ConnectionStatus.DISCONNECTED,
                "Planning Center plugin stopped.",
            )
            self._status = PluginStatus.STOPPED
            self._last_activity_at = datetime.now(UTC)
        if close_error is not None:
            self._last_error = "Planning Center client shutdown failed."
            raise PlanningCenterError(self._last_error) from None

    async def health(self) -> PluginHealth:
        return PluginHealth(
            name=self.name,
            version=self.version,
            status=self._status,
            last_error=self._last_error,
            last_activity_at=self._last_activity_at,
        )

    async def refresh_now(self) -> None:
        """Refresh the saved service after the desktop startup reconciliation pass."""

        if self._status is not PluginStatus.RUNNING or self._client is None:
            return
        await self._request_regular_refresh(wait=True)

    async def reconfigure(self, settings: PlanningCenterSettings) -> ActionOutcome:
        """Apply restart-exempt settings changes to the already-running plugin.

        Replaces the cached settings and HTTP client in place, then re-runs
        configuration validation and a full plan refresh -- the same steps
        ``start()`` performs -- so the dashboard's connection status reflects
        the new settings without a backend restart. The plugin's event
        subscriptions and background task bookkeeping are left untouched.
        """

        if self._stopping or self._status is PluginStatus.STOPPED:
            return ActionOutcome(
                False, "Restart StagePilot to apply the updated Planning Center settings."
            )

        previous_settings = self._settings
        previous_client = self._client
        self._settings = settings
        try:
            self._validate_configuration()
        except PlanningCenterError as exc:
            self._settings = previous_settings
            detail = self._with_cache_warning(str(exc))
            self._record_error(detail)
            await self._publish_connection(ConnectionStatus.ERROR, detail)
            return ActionOutcome(False, detail)

        self._client = self._client_factory(self._settings)
        try:
            await self._request_regular_refresh(wait=True)
        except Exception:
            self._settings = previous_settings
            self._client = previous_client
            detail = "Planning Center settings could not be applied to the running plugin."
            self._logger.error("planning_center_reconfigure_failed")
            self._record_error(detail)
            await self._publish_connection(ConnectionStatus.ERROR, detail)
            return ActionOutcome(False, detail)

        if previous_client is not None and previous_client is not self._client:
            with suppress(Exception):
                await previous_client.close()

        if self._status is PluginStatus.ERROR:
            detail = (
                self._last_error
                or "Planning Center settings were applied but the connection failed."
            )
            return ActionOutcome(False, detail)

        return ActionOutcome(
            True, "Planning Center settings applied to the running service source."
        )

    async def _on_reload_requested(self, _event: StagePilotEvent) -> None:
        if self._stopping:
            return
        task = self._active_refresh_task
        if task is not None and not task.done():
            self._pending_reload = True
            return
        self._active_refresh_task = asyncio.create_task(self._run_scheduled_refresh())

    async def _on_plan_selection_requested(self, event: StagePilotEvent) -> None:
        if self._stopping or not isinstance(event.payload, ServicePlanSelectionPayload):
            return
        snapshot = await self.state_store.snapshot()
        today = self._safe_today()
        target_date = snapshot.service_load.target_date
        candidate_ids = {
            candidate.id
            for candidate in snapshot.service_load.candidates
            if candidate.target_date == target_date
        }
        if (
            snapshot.service_load.status is not ServiceLoadStatus.AMBIGUOUS
            or target_date is None
            or target_date < today
        ):
            if event.payload.plan_id in {
                candidate.id for candidate in snapshot.service_load.candidates
            }:
                await self._request_regular_refresh(wait=True)
            return
        if event.payload.plan_id not in candidate_ids:
            return

        task = self._active_refresh_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._run_scheduled_refresh(
                    initial_selection_id=event.payload.plan_id,
                )
            )
            self._active_refresh_task = task
        else:
            self._pending_selection_id = event.payload.plan_id
        await asyncio.shield(task)

    async def _request_regular_refresh(self, *, wait: bool) -> None:
        if self._stopping:
            return
        task = self._active_refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._run_scheduled_refresh())
            self._active_refresh_task = task
        else:
            self._pending_reload = True
        if wait:
            await asyncio.shield(task)

    async def _run_scheduled_refresh(
        self,
        *,
        initial_selection_id: str | None = None,
    ) -> None:
        selected_plan_id = initial_selection_id
        try:
            while not self._stopping:
                await self._refresh_once(selected_plan_id=selected_plan_id)
                if self._pending_selection_id is not None:
                    selected_plan_id = self._pending_selection_id
                    self._pending_selection_id = None
                    self._pending_reload = False
                    continue
                if self._pending_reload:
                    self._pending_reload = False
                    selected_plan_id = None
                    continue
                return
        finally:
            self._active_refresh_task = None

    async def _refresh_once(self, *, selected_plan_id: str | None = None) -> None:
        search_date = self._safe_today()
        previous_state = await self.state_store.snapshot()
        previous_plan_date = self._actionable_plan_date(previous_state, search_date)
        has_actionable_plan = previous_plan_date is not None
        previous_candidate_date = previous_state.service_load.target_date
        previous_candidates = (
            [
                candidate
                for candidate in previous_state.service_load.candidates
                if candidate.target_date == previous_candidate_date
            ]
            if previous_candidate_date is not None and previous_candidate_date >= search_date
            else []
        )
        previous_skipped_items = (
            previous_state.service_load.skipped_items if has_actionable_plan else []
        )
        retained_target_date = (
            previous_plan_date
            or (previous_candidate_date if len(previous_candidates) >= 2 else None)
            or search_date
        )
        await self._publish_load_state(
            ServiceLoadStatus.LOADING,
            retained_target_date,
            skipped_items=previous_skipped_items,
            message="Looking for the current or next upcoming Planning Center plan.",
            is_stale=has_actionable_plan,
        )
        await self._publish_connection(
            ConnectionStatus.CONNECTING,
            "Loading Planning Center Services.",
        )

        api_connected = False
        try:
            client = self._require_client()
            service_types = await client.list_service_types()
            api_connected = True
            configured_service_types = self._configured_service_types(service_types)
            result = await self._load_plan_for_service_types(
                client,
                configured_service_types,
                search_date,
                selected_plan_id=selected_plan_id,
            )
            if isinstance(result, PlanAmbiguousResult) and selected_plan_id is None:
                preferred = self._preferred_candidate(result.candidates)
                if preferred is not None:
                    result = await self._load_plan_for_service_types(
                        client,
                        configured_service_types,
                        search_date,
                        selected_plan_id=preferred.id,
                    )
        except PlanningCenterPlanSelectionError as exc:
            self._record_error(str(exc))
            await self._publish_connection(ConnectionStatus.CONNECTED, str(exc))
            await self._publish_load_state(
                ServiceLoadStatus.AMBIGUOUS
                if len(previous_candidates) >= 2
                else ServiceLoadStatus.ERROR,
                retained_target_date,
                candidates=previous_candidates,
                skipped_items=previous_skipped_items,
                message=str(exc),
                is_stale=has_actionable_plan,
            )
            return
        except PlanningCenterError as exc:
            detail = self._with_cache_warning(str(exc))
            self._record_error(detail)
            connection_status = (
                ConnectionStatus.CONNECTED
                if api_connected and isinstance(exc, PlanningCenterConfigurationError)
                else ConnectionStatus.ERROR
            )
            await self._publish_connection(connection_status, detail)
            await self._publish_load_state(
                ServiceLoadStatus.AMBIGUOUS
                if selected_plan_id is not None and len(previous_candidates) >= 2
                else ServiceLoadStatus.ERROR,
                retained_target_date,
                candidates=(
                    previous_candidates
                    if selected_plan_id is not None and len(previous_candidates) >= 2
                    else []
                ),
                skipped_items=previous_skipped_items,
                message=detail,
                is_stale=has_actionable_plan,
            )
            return
        except Exception:
            detail = self._with_cache_warning("Planning Center plan loading failed unexpectedly.")
            self._logger.error("planning_center_unexpected_load_failure")
            self._record_error(detail)
            await self._publish_connection(ConnectionStatus.ERROR, detail)
            await self._publish_load_state(
                ServiceLoadStatus.AMBIGUOUS
                if selected_plan_id is not None and len(previous_candidates) >= 2
                else ServiceLoadStatus.ERROR,
                retained_target_date,
                candidates=(
                    previous_candidates
                    if selected_plan_id is not None and len(previous_candidates) >= 2
                    else []
                ),
                skipped_items=previous_skipped_items,
                message=detail,
                is_stale=has_actionable_plan,
            )
            return

        self._status = PluginStatus.RUNNING
        self._last_error = None
        self._last_activity_at = datetime.now(UTC)
        await self._publish_connection(
            ConnectionStatus.CONNECTED,
            "Planning Center Services is connected.",
        )
        try:
            await self._apply_result(
                result,
                search_date,
                previous_plan_date,
                previous_skipped_items,
            )
        except Exception:
            detail = "Planning Center plan projection failed unexpectedly."
            self._logger.error("planning_center_unexpected_projection_failure")
            self._record_error(detail)
            current_state = await self.state_store.snapshot()
            current_plan_date = self._actionable_plan_date(current_state, search_date)
            await self._publish_connection(ConnectionStatus.ERROR, detail)
            await self._publish_load_state(
                ServiceLoadStatus.AMBIGUOUS
                if selected_plan_id is not None and len(previous_candidates) >= 2
                else ServiceLoadStatus.ERROR,
                current_plan_date or retained_target_date,
                candidates=(
                    previous_candidates
                    if selected_plan_id is not None and len(previous_candidates) >= 2
                    else []
                ),
                skipped_items=previous_skipped_items,
                message=detail,
                is_stale=current_plan_date is not None,
            )
            return

    async def _apply_result(
        self,
        result: PlanDiscoveryResult,
        search_date: date,
        previous_plan_date: date | None,
        previous_skipped_items: list[SkippedServiceItem],
    ) -> None:
        search_end_date = search_date + timedelta(days=self._settings.upcoming_lookahead_days)
        if isinstance(result, PlanNotFoundResult):
            if result.target_date != search_date:
                raise RuntimeError("Planning Center returned an invalid search anchor.")
            retained_plan = previous_plan_date is not None
            target_date = previous_plan_date or result.target_date
            message = (
                "No current or upcoming Planning Center plan was found through "
                f"{search_end_date.isoformat()}; keeping the previously loaded plan stale."
                if retained_plan
                else "No Planning Center service plan was found from "
                f"{result.target_date.isoformat()} through {search_end_date.isoformat()}."
            )
            await self._publish_load_state(
                ServiceLoadStatus.NOT_FOUND,
                target_date,
                skipped_items=previous_skipped_items if retained_plan else [],
                message=message,
                is_stale=retained_plan,
            )
            return
        if isinstance(result, PlanAmbiguousResult):
            if not search_date <= result.target_date <= search_end_date:
                raise RuntimeError("Planning Center returned candidates outside the search window.")
            retained_plan = previous_plan_date == result.target_date
            await self._publish_load_state(
                ServiceLoadStatus.AMBIGUOUS,
                result.target_date,
                candidates=[self._candidate(value) for value in result.candidates],
                skipped_items=previous_skipped_items if retained_plan else [],
                message=(
                    "Multiple Planning Center plans match "
                    f"{result.target_date.isoformat()}. Select one to continue."
                ),
                is_stale=retained_plan,
            )
            return
        if not isinstance(result, PlanLoadedResult):
            raise AssertionError("Unsupported Planning Center discovery result.")

        loaded_date = result.plan.date
        if (
            not search_date <= loaded_date <= search_end_date
            or result.candidate.target_date != loaded_date
            or result.candidate.id != result.plan.id
        ):
            raise RuntimeError("Planning Center returned an invalid loaded plan.")
        if previous_plan_date is not None and previous_plan_date != loaded_date:
            await self._publish_load_state(
                ServiceLoadStatus.LOADING,
                loaded_date,
                message=f"Loading the Planning Center plan for {loaded_date.isoformat()}.",
            )

        before_projection = await self.state_store.snapshot()
        report = await self.event_bus.publish(
            new_event(
                EventType.SERVICE_LOADED,
                source=self.name,
                payload=ServicePayload(plan=result.plan),
            )
        )
        projected = await self.state_store.snapshot()
        if (
            report.failures
            or projected.revision <= before_projection.revision
            or projected.plan != result.plan
        ):
            raise RuntimeError("Planning Center plan was not projected into application state.")
        await self._publish_load_state(
            ServiceLoadStatus.LOADED,
            loaded_date,
            skipped_items=[
                SkippedServiceItem(
                    item_id=item.item_id,
                    title=item.title,
                    description=item.description,
                    item_type=item.item_type,
                    sequence=item.sequence,
                    duration_seconds=item.duration_seconds,
                    reason=item.reason,
                )
                for item in result.skipped_items
            ],
            message=self._cache_fresh_plan(
                result.plan,
                projected.last_successful_plan_reload_at or datetime.now(UTC),
            ),
        )

    async def _publish_failure(
        self,
        target_date: date,
        detail: str,
    ) -> None:
        state = await self.state_store.snapshot()
        plan_date = self._actionable_plan_date(state, target_date)
        await self._publish_connection(ConnectionStatus.ERROR, detail)
        await self._publish_load_state(
            ServiceLoadStatus.ERROR,
            plan_date or target_date,
            skipped_items=state.service_load.skipped_items if plan_date is not None else [],
            message=detail,
            is_stale=plan_date is not None,
        )

    async def _publish_load_state(
        self,
        status: ServiceLoadStatus,
        target_date: date,
        *,
        candidates: list[ServicePlanCandidate] | None = None,
        skipped_items: list[SkippedServiceItem] | None = None,
        message: str | None = None,
        is_stale: bool = False,
    ) -> None:
        await self.event_bus.publish(
            new_event(
                EventType.SERVICE_LOAD_CHANGED,
                source=self.name,
                payload=ServiceLoadPayload(
                    status=status,
                    target_date=target_date,
                    candidates=candidates or [],
                    skipped_items=skipped_items or [],
                    message=message,
                    is_stale=is_stale,
                ),
            )
        )

    async def _publish_connection(
        self,
        status: ConnectionStatus,
        detail: str,
    ) -> None:
        await self.event_bus.publish(
            new_event(
                EventType.CONNECTION_CHANGED,
                source=self.name,
                payload=ConnectionPayload(
                    integration="planning_center",
                    status=status,
                    detail=detail,
                ),
            )
        )

    def _validate_configuration(self) -> None:
        if not self._settings.is_configured:
            raise PlanningCenterConfigurationError(
                "Planning Center credentials are not configured."
            )
        if self._settings.service_type_id is None:
            raise PlanningCenterConfigurationError(
                "A Planning Center service type must be configured."
            )

    def _require_client(self) -> PlanningCenterClientContract:
        if self._client is None:
            raise PlanningCenterConfigurationError("Planning Center is not initialized.")
        return self._client

    def _configured_service_types(
        self,
        service_types: list[PlanningCenterServiceType],
    ) -> list[PlanningCenterServiceType]:
        configured_id = self._settings.service_type_id
        if configured_id == ALL_SERVICE_TYPES_ID:
            active_service_types = [value for value in service_types if not value.archived]
            if not active_service_types:
                raise PlanningCenterConfigurationError(
                    "No active Planning Center service types are available."
                )
            return active_service_types

        service_type = next(
            (value for value in service_types if value.id == configured_id),
            None,
        )
        if service_type is None:
            raise PlanningCenterConfigurationError(
                "The configured Planning Center service type is unavailable."
            )
        if service_type.archived:
            raise PlanningCenterConfigurationError(
                "The configured Planning Center service type is archived."
            )
        return [service_type]

    async def _load_plan_for_service_types(
        self,
        client: PlanningCenterClientContract,
        service_types: list[PlanningCenterServiceType],
        search_date: date,
        *,
        selected_plan_id: str | None,
    ) -> PlanDiscoveryResult:
        if self._settings.service_type_id == ALL_SERVICE_TYPES_ID:
            return await client.load_plan_for_service_types(
                service_types,
                search_date,
                self._timezone_name,
                selected_plan_id=selected_plan_id,
                lookahead_days=self._settings.upcoming_lookahead_days,
            )
        return await client.load_plan_for_date(
            service_types[0],
            search_date,
            self._timezone_name,
            selected_plan_id=selected_plan_id,
            lookahead_days=self._settings.upcoming_lookahead_days,
        )

    def _preferred_candidate(
        self,
        candidates: list[PlanningCenterPlanCandidate],
    ) -> PlanningCenterPlanCandidate | None:
        title_preference = self._settings.plan_title_preference
        time_preference = self._settings.preferred_service_time
        if title_preference is None and time_preference is None:
            return None

        normalized_title = title_preference.casefold().strip() if title_preference else None
        scores: list[tuple[int, PlanningCenterPlanCandidate]] = []
        for candidate in candidates:
            score = 0
            if (
                normalized_title is not None
                and candidate.title.casefold().strip() == normalized_title
            ):
                score += 1
            if time_preference is not None and any(
                value.strftime("%H:%M") == time_preference for value in candidate.service_times
            ):
                score += 1
            scores.append((score, candidate))

        highest_score = max((score for score, _candidate in scores), default=0)
        if highest_score == 0:
            return None
        matches = [candidate for score, candidate in scores if score == highest_score]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _candidate(candidate: PlanningCenterPlanCandidate) -> ServicePlanCandidate:
        return ServicePlanCandidate(
            id=candidate.id,
            title=candidate.title,
            service_type_id=candidate.service_type_id,
            service_type_name=candidate.service_type_name,
            target_date=candidate.target_date,
            service_times=[
                service_time.strftime("%H:%M") for service_time in candidate.service_times
            ],
        )

    @staticmethod
    def _actionable_plan_date(state: ApplicationState, search_date: date) -> date | None:
        if state.plan is None or state.plan.date < search_date:
            return None
        return state.plan.date

    def _record_error(self, detail: str) -> None:
        self._status = PluginStatus.ERROR
        self._last_error = detail
        self._last_activity_at = datetime.now(UTC)
        self._logger.warning(
            "planning_center_load_failed",
            error_type="safe_planning_center_error",
        )

    async def _restore_cached_plan(self) -> None:
        try:
            cached = self._plan_cache_store.load()
        except PlanCacheError as exc:
            self._cache_warning = str(exc)
            return
        if cached is None:
            return

        today = self._safe_today()
        if cached.plan.date < today:
            self._cache_warning = (
                f'The cached service "{cached.plan.title}" expired on '
                f"{cached.plan.date.isoformat()} and was not loaded."
            )
            return
        if (
            self._settings.service_type_id != ALL_SERVICE_TYPES_ID
            and cached.plan.service_type_id != self._settings.service_type_id
        ):
            self._cache_warning = (
                "The cached service belongs to a different Planning Center service type "
                "and was not loaded."
            )
            return

        report = await self.event_bus.publish(
            new_event(
                EventType.SERVICE_LOADED,
                source=self.name,
                payload=ServicePayload(plan=cached.plan),
            )
        )
        state = await self.state_store.snapshot()
        if report.failures or state.plan != cached.plan:
            self._cache_warning = "The last-known-good service could not be restored."
            return

        def restore_refresh_timestamp(application_state: ApplicationState) -> None:
            application_state.last_successful_plan_reload_at = cached.last_successful_refresh

        await self.state_store.mutate(restore_refresh_timestamp)
        await self._publish_load_state(
            ServiceLoadStatus.LOADING,
            cached.plan.date,
            message=(
                f'Loaded cached service "{cached.plan.title}" from '
                f"{cached.last_successful_refresh.isoformat()}; refreshing Planning Center."
            ),
            is_stale=True,
        )
        self._cache_warning = (
            "The displayed service was loaded from the last-known-good cache and may be stale."
        )

    def _cache_fresh_plan(self, plan: ServicePlan, refreshed_at: datetime) -> str:
        try:
            self._plan_cache_store.save(
                CachedServicePlan(
                    plan=plan,
                    last_successful_refresh=refreshed_at,
                )
            )
            self._cache_warning = None
        except PlanCacheError as exc:
            self._cache_warning = str(exc)
        message = f'Loaded "{plan.title}" for {plan.date.isoformat()} from Planning Center.'
        return self._with_cache_warning(message)

    def _with_cache_warning(self, detail: str) -> str:
        if self._cache_warning is None:
            return detail
        return f"{detail} {self._cache_warning}"

    async def _cleanup_resources(self) -> None:
        for subscription in self._subscriptions:
            await self.event_bus.unsubscribe(subscription)
        self._subscriptions.clear()
        await self._close_client()

    async def _handle_start_failure(self, detail: str) -> None:
        self._status = PluginStatus.ERROR
        self._last_error = detail
        self._last_activity_at = datetime.now(UTC)
        try:
            await self._publish_failure(self._safe_today(), detail)
        except Exception:
            self._logger.error("planning_center_start_failure_publish_failed")
        try:
            await self._cleanup_resources()
        except Exception:
            self._logger.error("planning_center_start_failure_cleanup_failed")

    def _safe_today(self) -> date:
        try:
            return self._today_provider(self._timezone)
        except Exception:
            self._logger.error("planning_center_today_provider_failed")
            return datetime.now(self._timezone).date()

    async def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.close()
