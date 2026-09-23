from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from stagepilot.core.config import PlanningCenterSettings
from stagepilot.core.event_bus import EventBus, Subscription
from stagepilot.core.events import (
    ActionName,
    ConnectionPayload,
    EventType,
    ServiceLoadPayload,
    ServicePlanSelectionPayload,
    StagePilotEvent,
    new_event,
)
from stagepilot.core.plan_cache import CachedServicePlan, MemoryPlanCacheStore, PlanCacheStore
from stagepilot.core.state import StateStore
from stagepilot.models.state import (
    ApplicationState,
    ConnectionStatus,
    PluginStatus,
    ServiceLoadStatus,
    ServicePlan,
    Song,
)
from stagepilot.plugins.planning_center.errors import (
    PlanningCenterConfigurationError,
    PlanningCenterError,
    PlanningCenterTimeoutError,
)
from stagepilot.plugins.planning_center.models import (
    PlanAmbiguousResult,
    PlanDiscoveryResult,
    PlanLoadedResult,
    PlanningCenterPlanCandidate,
    PlanningCenterServiceType,
    PlanNotFoundResult,
    SkippedItemReason,
    SkippedPlanItem,
)
from stagepilot.plugins.planning_center.plugin import (
    PlanningCenterClientContract,
    PlanningCenterPlugin,
)
from stagepilot.services.state_service import StateService

SERVICE_DATE = date(2026, 7, 12)
NEXT_DATE = date(2026, 7, 13)
TIMEZONE_NAME = "America/Los_Angeles"
StatePredicate = Callable[[ApplicationState], bool]
ClientOutcome = PlanDiscoveryResult | Exception


@dataclass(frozen=True, slots=True)
class LoadCall:
    service_type: PlanningCenterServiceType
    target_date: date
    timezone_name: str
    selected_plan_id: str | None
    lookahead_days: int


class FakePlanningCenterClient:
    def __init__(
        self,
        outcomes: list[ClientOutcome],
        *,
        service_types: list[PlanningCenterServiceType] | None = None,
        blocked_call_indexes: set[int] | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.service_types = service_types or [service_type()]
        self.outcomes = deque(outcomes)
        self.blocked_call_indexes = blocked_call_indexes or set()
        self.close_error = close_error
        self.list_calls = 0
        self.load_calls: list[LoadCall] = []
        self.close_calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.load_entered = asyncio.Event()
        self.release_load = asyncio.Event()
        self.load_cancelled = asyncio.Event()
        self.operation_log: list[str] = []

    async def list_service_types(self) -> list[PlanningCenterServiceType]:
        self.list_calls += 1
        return [value.model_copy(deep=True) for value in self.service_types]

    async def load_plan_for_date(
        self,
        service_type: PlanningCenterServiceType,
        target_date: date,
        timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        call_index = len(self.load_calls)
        self.load_calls.append(
            LoadCall(
                service_type=service_type,
                target_date=target_date,
                timezone_name=timezone_name,
                selected_plan_id=selected_plan_id,
                lookahead_days=lookahead_days,
            )
        )
        if not self.outcomes:
            raise AssertionError("The fake Planning Center client has no queued outcome.")
        outcome = self.outcomes.popleft()
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if call_index in self.blocked_call_indexes:
                self.load_entered.set()
                try:
                    await self.release_load.wait()
                except asyncio.CancelledError:
                    self.operation_log.append("load_cancelled")
                    self.load_cancelled.set()
                    raise
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.in_flight -= 1

    async def close(self) -> None:
        self.close_calls += 1
        self.operation_log.append("close")
        if self.close_error is not None:
            raise self.close_error


class FakeClientFactory:
    def __init__(self, client: PlanningCenterClientContract) -> None:
        self.client = client
        self.calls = 0
        self.settings: list[PlanningCenterSettings] = []

    def __call__(self, settings: PlanningCenterSettings) -> PlanningCenterClientContract:
        self.calls += 1
        self.settings.append(settings)
        return self.client


class MutableToday:
    def __init__(self, value: date) -> None:
        self.value = value
        self.timezones: list[str] = []

    def __call__(self, timezone: ZoneInfo) -> date:
        self.timezones.append(timezone.key)
        return self.value


@dataclass(slots=True)
class PluginHarness:
    event_bus: EventBus
    state_store: StateStore
    state_service: StateService
    plugin: PlanningCenterPlugin
    client: FakePlanningCenterClient
    factory: FakeClientFactory
    today: MutableToday
    events: list[StagePilotEvent]
    event_subscription: Subscription

    async def close(self) -> None:
        await self.plugin.stop()
        await self.state_service.stop()
        await self.event_bus.unsubscribe(self.event_subscription)


def configured_settings() -> PlanningCenterSettings:
    return PlanningCenterSettings(
        app_id="test-app-id",
        secret="test-secret",
        service_type_id="42",
    )


def service_type(
    identifier: str = "42",
    name: str = "Weekend Services",
    *,
    archived: bool = False,
) -> PlanningCenterServiceType:
    return PlanningCenterServiceType(
        id=identifier,
        name=name,
        sequence=1,
        archived=archived,
    )


def candidate(
    identifier: str,
    target_date: date = SERVICE_DATE,
    *,
    hour: int = 9,
) -> PlanningCenterPlanCandidate:
    return PlanningCenterPlanCandidate(
        id=identifier,
        title=f"Plan {identifier}",
        service_type_id="42",
        service_type_name="Weekend Services",
        target_date=target_date,
        service_times=[
            datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                hour,
                tzinfo=ZoneInfo(TIMEZONE_NAME),
            )
        ],
    )


def plan(
    identifier: str,
    target_date: date = SERVICE_DATE,
    *,
    song_ids: tuple[str, ...] = ("song-a", "song-b"),
) -> ServicePlan:
    return ServicePlan(
        id=identifier,
        title=f"Plan {identifier}",
        date=target_date,
        service_type="Weekend Services",
        service_type_id="42",
        service_times=["09:00"],
        duration_source="Planning Center scheduled item length",
        songs=[
            Song(
                id=song_id,
                title=f"Song {song_id}",
                duration_seconds=240 + index,
                order=index + 1,
            )
            for index, song_id in enumerate(song_ids)
        ],
    )


def skipped_item(identifier: str) -> SkippedPlanItem:
    return SkippedPlanItem(
        item_id=identifier,
        title="Service Header",
        description="Opening section",
        item_type="header",
        sequence=1,
        duration_seconds=90,
        reason=SkippedItemReason.HEADER,
    )


def loaded_result(
    identifier: str,
    target_date: date = SERVICE_DATE,
    *,
    song_ids: tuple[str, ...] = ("song-a", "song-b"),
    warning_id: str = "header-1",
) -> PlanLoadedResult:
    return PlanLoadedResult(
        candidate=candidate(identifier, target_date),
        plan=plan(identifier, target_date, song_ids=song_ids),
        skipped_items=[skipped_item(warning_id)],
    )


def not_found_result(target_date: date = SERVICE_DATE) -> PlanNotFoundResult:
    return PlanNotFoundResult(
        service_type=service_type(),
        target_date=target_date,
    )


def ambiguous_result(target_date: date = SERVICE_DATE) -> PlanAmbiguousResult:
    return PlanAmbiguousResult(
        service_type=service_type(),
        target_date=target_date,
        candidates=[
            candidate("plan-morning", target_date, hour=9),
            candidate("plan-evening", target_date, hour=18),
        ],
    )


async def plugin_harness(
    client: FakePlanningCenterClient,
    *,
    today: MutableToday | None = None,
    settings: PlanningCenterSettings | None = None,
    plan_cache_store: PlanCacheStore | None = None,
) -> PluginHarness:
    event_bus = EventBus()
    state_store = StateStore()
    state_service = StateService(event_bus, state_store)
    await state_service.start()
    events: list[StagePilotEvent] = []

    async def capture(event: StagePilotEvent) -> None:
        events.append(event)

    event_subscription = await event_bus.subscribe(None, capture)
    resolved_today = today or MutableToday(SERVICE_DATE)
    factory = FakeClientFactory(client)
    plugin = PlanningCenterPlugin(
        event_bus,
        state_store,
        settings or configured_settings(),
        timezone_name=TIMEZONE_NAME,
        client_factory=factory,
        today_provider=resolved_today,
        plan_cache_store=plan_cache_store,
    )
    return PluginHarness(
        event_bus=event_bus,
        state_store=state_store,
        state_service=state_service,
        plugin=plugin,
        client=client,
        factory=factory,
        today=resolved_today,
        events=events,
        event_subscription=event_subscription,
    )


async def wait_for_state(
    state_store: StateStore,
    predicate: StatePredicate,
) -> ApplicationState:
    queue = await state_store.subscribe()
    try:
        state = await state_store.snapshot()
        while not predicate(state):
            state = await asyncio.wait_for(queue.get(), timeout=2)
        return state
    finally:
        await state_store.unsubscribe(queue)


async def settle_scheduled_refresh() -> None:
    for _iteration in range(3):
        await asyncio.sleep(0)


def planning_center_events(
    harness: PluginHarness,
    event_type: EventType,
) -> list[StagePilotEvent]:
    return [
        event
        for event in harness.events
        if event.source == "planning_center" and event.type is event_type
    ]


@pytest.mark.asyncio
async def test_startup_resolves_service_type_and_loads_configured_local_date() -> None:
    client = FakePlanningCenterClient(
        [loaded_result("plan-1")],
        service_types=[
            service_type("7", "Other Services"),
            service_type("42", "Weekend Services"),
        ],
    )
    today = MutableToday(SERVICE_DATE)
    harness = await plugin_harness(client, today=today)
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert harness.factory.calls == 1
        assert client.list_calls == 1
        assert len(client.load_calls) == 1
        call = client.load_calls[0]
        assert call.service_type.id == "42"
        assert call.service_type.name == "Weekend Services"
        assert call.target_date == SERVICE_DATE
        assert call.timezone_name == TIMEZONE_NAME
        assert call.selected_plan_id is None
        assert call.lookahead_days == 30
        assert today.timezones == [TIMEZONE_NAME]
        assert state.planning_center_status is ConnectionStatus.CONNECTED
        assert state.service_load.status is ServiceLoadStatus.LOADED
        assert state.service_load.is_stale is False
        assert state.plan and state.plan.id == "plan-1"
        assert state.next_song and state.next_song.id == "song-a"
        assert [item.item_id for item in state.service_load.skipped_items] == ["header-1"]
        assert state.service_load.skipped_items[0].duration_seconds == 90
        assert state.service_load.skipped_items[0].description == "Opening section"
        assert state.last_successful_plan_reload_at is not None
        health = await harness.plugin.health()
        assert health.status is PluginStatus.RUNNING
        assert health.last_error is None
        connection_events = planning_center_events(harness, EventType.CONNECTION_CHANGED)
        assert [
            event.payload.status
            for event in connection_events
            if isinstance(event.payload, ConnectionPayload)
        ] == [ConnectionStatus.CONNECTING, ConnectionStatus.CONNECTED]
        assert len(planning_center_events(harness, EventType.SERVICE_LOADED)) == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_not_found_keeps_connection_healthy_without_loading_a_plan() -> None:
    harness = await plugin_harness(FakePlanningCenterClient([not_found_result()]))
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert state.planning_center_status is ConnectionStatus.CONNECTED
        assert state.service_load.status is ServiceLoadStatus.NOT_FOUND
        assert state.service_load.target_date == SERVICE_DATE
        assert state.service_load.is_stale is False
        assert state.plan is None
        assert planning_center_events(harness, EventType.SERVICE_LOADED) == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_ambiguity_exposes_safe_candidates_without_loading_a_plan() -> None:
    harness = await plugin_harness(FakePlanningCenterClient([ambiguous_result()]))
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert state.planning_center_status is ConnectionStatus.CONNECTED
        assert state.service_load.status is ServiceLoadStatus.AMBIGUOUS
        assert [value.id for value in state.service_load.candidates] == [
            "plan-morning",
            "plan-evening",
        ]
        assert [value.service_times for value in state.service_load.candidates] == [
            ["09:00"],
            ["18:00"],
        ]
        assert state.plan is None
        assert planning_center_events(harness, EventType.SERVICE_LOADED) == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_preferred_service_time_resolves_one_ambiguous_plan() -> None:
    client = FakePlanningCenterClient(
        [
            ambiguous_result(),
            loaded_result("plan-evening", warning_id="preferred-warning"),
        ]
    )
    settings = configured_settings().model_copy(update={"preferred_service_time": "18:00"})
    harness = await plugin_harness(client, settings=settings)
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert [call.selected_plan_id for call in client.load_calls] == [
            None,
            "plan-evening",
        ]
        assert state.service_load.status is ServiceLoadStatus.LOADED
        assert state.plan and state.plan.id == "plan-evening"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_equally_scored_preferences_remain_ambiguous() -> None:
    client = FakePlanningCenterClient([ambiguous_result()])
    settings = configured_settings().model_copy(
        update={
            "plan_title_preference": "Plan plan-morning",
            "preferred_service_time": "18:00",
        }
    )
    harness = await plugin_harness(client, settings=settings)
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert len(client.load_calls) == 1
        assert state.service_load.status is ServiceLoadStatus.AMBIGUOUS
        assert [candidate.id for candidate in state.service_load.candidates] == [
            "plan-morning",
            "plan-evening",
        ]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_planning_center_outage_restores_last_known_good_service() -> None:
    refreshed_at = datetime(2026, 7, 11, 18, tzinfo=UTC)
    cache = MemoryPlanCacheStore(
        CachedServicePlan(
            plan=plan("cached-plan", SERVICE_DATE),
            last_successful_refresh=refreshed_at,
        )
    )
    harness = await plugin_harness(
        FakePlanningCenterClient([PlanningCenterTimeoutError("Planning Center timed out.")]),
        plan_cache_store=cache,
    )
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert state.plan and state.plan.id == "cached-plan"
        assert state.service_load.status is ServiceLoadStatus.ERROR
        assert state.service_load.is_stale is True
        assert "last-known-good cache" in (state.service_load.message or "")
        assert state.last_successful_plan_reload_at == refreshed_at
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_successful_refresh_replaces_the_cached_service() -> None:
    cache = MemoryPlanCacheStore()
    harness = await plugin_harness(
        FakePlanningCenterClient([loaded_result("fresh-plan")]),
        plan_cache_store=cache,
    )
    try:
        await harness.plugin.start()

        assert cache.cached is not None
        assert cache.cached.plan.id == "fresh-plan"
        assert cache.cached.last_successful_refresh.tzinfo is not None
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_expired_cache_is_not_loaded_and_surfaces_a_warning() -> None:
    cache = MemoryPlanCacheStore(
        CachedServicePlan(
            plan=plan("expired-plan", SERVICE_DATE),
            last_successful_refresh=datetime(2026, 7, 12, 18, tzinfo=UTC),
        )
    )
    today = MutableToday(NEXT_DATE)
    harness = await plugin_harness(
        FakePlanningCenterClient([PlanningCenterTimeoutError("Planning Center timed out.")]),
        today=today,
        plan_cache_store=cache,
    )
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert state.plan is None
        assert state.service_load.is_stale is False
        assert "expired on 2026-07-12" in (state.service_load.message or "")
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_valid_ambiguous_plan_selection_loads_only_selected_candidate() -> None:
    client = FakePlanningCenterClient(
        [
            ambiguous_result(),
            loaded_result("plan-evening", warning_id="selected-warning"),
        ]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()

        await harness.event_bus.publish(
            new_event(
                EventType.SERVICE_PLAN_SELECTION_REQUESTED,
                source="test",
                payload=ServicePlanSelectionPayload(plan_id="plan-evening"),
            )
        )

        state = await harness.state_store.snapshot()
        assert len(client.load_calls) == 2
        assert client.load_calls[-1].selected_plan_id == "plan-evening"
        assert state.service_load.status is ServiceLoadStatus.LOADED
        assert state.service_load.candidates == []
        assert state.plan and state.plan.id == "plan-evening"
        assert [item.item_id for item in state.service_load.skipped_items] == ["selected-warning"]
        assert len(planning_center_events(harness, EventType.SERVICE_LOADED)) == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_future_ambiguous_plan_selection_loads_selected_candidate() -> None:
    client = FakePlanningCenterClient(
        [
            ambiguous_result(NEXT_DATE),
            loaded_result(
                "plan-evening",
                NEXT_DATE,
                warning_id="selected-future-warning",
            ),
        ]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        ambiguous_state = await harness.state_store.snapshot()
        assert ambiguous_state.service_load.status is ServiceLoadStatus.AMBIGUOUS
        assert ambiguous_state.service_load.target_date == NEXT_DATE

        await harness.event_bus.publish(
            new_event(
                EventType.SERVICE_PLAN_SELECTION_REQUESTED,
                source="test",
                payload=ServicePlanSelectionPayload(plan_id="plan-evening"),
            )
        )

        state = await harness.state_store.snapshot()
        assert len(client.load_calls) == 2
        assert client.load_calls[-1].selected_plan_id == "plan-evening"
        assert client.load_calls[-1].lookahead_days == 30
        assert state.service_load.status is ServiceLoadStatus.LOADED
        assert state.service_load.target_date == NEXT_DATE
        assert state.service_load.candidates == []
        assert state.plan and state.plan.id == "plan-evening"
        assert state.plan.date == NEXT_DATE
        assert [item.item_id for item in state.service_load.skipped_items] == [
            "selected-future-warning"
        ]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_failed_plan_selection_keeps_candidates_available_for_retry() -> None:
    client = FakePlanningCenterClient(
        [
            ambiguous_result(),
            PlanningCenterTimeoutError("Planning Center request timed out."),
        ]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()

        await harness.event_bus.publish(
            new_event(
                EventType.SERVICE_PLAN_SELECTION_REQUESTED,
                source="test",
                payload=ServicePlanSelectionPayload(plan_id="plan-evening"),
            )
        )

        state = await harness.state_store.snapshot()
        assert state.planning_center_status is ConnectionStatus.ERROR
        assert state.service_load.status is ServiceLoadStatus.AMBIGUOUS
        assert [candidate.id for candidate in state.service_load.candidates] == [
            "plan-morning",
            "plan-evening",
        ]
        assert state.plan is None
        assert client.load_calls[-1].selected_plan_id == "plan-evening"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_cross_date_selection_refreshes_without_reusing_old_candidates() -> None:
    client = FakePlanningCenterClient([ambiguous_result(SERVICE_DATE), not_found_result(NEXT_DATE)])
    today = MutableToday(SERVICE_DATE)
    harness = await plugin_harness(client, today=today)
    try:
        await harness.plugin.start()
        today.value = NEXT_DATE

        await harness.event_bus.publish(
            new_event(
                EventType.SERVICE_PLAN_SELECTION_REQUESTED,
                source="test",
                payload=ServicePlanSelectionPayload(plan_id="plan-evening"),
            )
        )

        state = await harness.state_store.snapshot()
        assert len(client.load_calls) == 2
        assert client.load_calls[-1].target_date == NEXT_DATE
        assert client.load_calls[-1].selected_plan_id is None
        assert state.service_load.status is ServiceLoadStatus.NOT_FOUND
        assert state.service_load.target_date == NEXT_DATE
        assert state.service_load.candidates == []
        assert state.service_load.skipped_items == []
        assert state.plan is None
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_same_day_failure_preserves_last_known_good_plan_position_and_warnings() -> None:
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-1", warning_id="active-warning"),
            PlanningCenterTimeoutError("Planning Center request timed out."),
        ]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        assert (await harness.state_service.dispatch(ActionName.START_NEXT)).accepted is True
        before = await harness.state_store.snapshot()

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: value.service_load.status is ServiceLoadStatus.ERROR,
        )

        assert state.plan == before.plan
        assert state.current_song == before.current_song
        assert state.current_song_index == before.current_song_index
        assert state.next_song == before.next_song
        assert state.last_successful_plan_reload_at == before.last_successful_plan_reload_at
        assert state.planning_center_status is ConnectionStatus.ERROR
        assert state.service_load.is_stale is True
        assert state.service_load.skipped_items == before.service_load.skipped_items
        assert len(planning_center_events(harness, EventType.SERVICE_LOADED)) == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refresh_result", "expected_status"),
    [
        (not_found_result(), ServiceLoadStatus.NOT_FOUND),
        (ambiguous_result(), ServiceLoadStatus.AMBIGUOUS),
    ],
)
async def test_semantic_reload_outcome_keeps_same_day_last_known_good_plan(
    refresh_result: PlanDiscoveryResult,
    expected_status: ServiceLoadStatus,
) -> None:
    client = FakePlanningCenterClient(
        [loaded_result("plan-1", warning_id="active-warning"), refresh_result]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        before = await harness.state_store.snapshot()

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: value.service_load.status is expected_status,
        )

        assert state.planning_center_status is ConnectionStatus.CONNECTED
        assert state.plan == before.plan
        assert state.service_load.is_stale is True
        assert state.service_load.skipped_items == before.service_load.skipped_items
        assert state.last_successful_plan_reload_at == before.last_successful_plan_reload_at
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_transient_reload_failure_keeps_upcoming_last_known_good_plan() -> None:
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-upcoming", NEXT_DATE, warning_id="upcoming-warning"),
            PlanningCenterTimeoutError("Planning Center request timed out."),
        ]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        before = await harness.state_store.snapshot()

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: value.service_load.status is ServiceLoadStatus.ERROR,
        )

        assert state.plan == before.plan
        assert state.plan and state.plan.date == NEXT_DATE
        assert state.service_load.target_date == NEXT_DATE
        assert state.service_load.is_stale is True
        assert state.service_load.skipped_items == before.service_load.skipped_items
        assert state.last_successful_plan_reload_at == before.last_successful_plan_reload_at
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_reload_clears_preloaded_plan_after_its_service_date_passes() -> None:
    day_after_service = date(2026, 7, 14)
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-upcoming", NEXT_DATE, warning_id="expired-warning"),
            PlanningCenterTimeoutError("Planning Center request timed out."),
        ]
    )
    today = MutableToday(SERVICE_DATE)
    harness = await plugin_harness(client, today=today)
    try:
        await harness.plugin.start()
        today.value = day_after_service

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: value.service_load.status is ServiceLoadStatus.ERROR,
        )

        assert state.plan is None
        assert state.current_song is None
        assert state.next_song is None
        assert state.service_load.target_date == day_after_service
        assert state.service_load.skipped_items == []
        assert state.service_load.is_stale is False
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_new_current_day_plan_replaces_preloaded_upcoming_plan() -> None:
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-upcoming", NEXT_DATE),
            loaded_result("plan-today", SERVICE_DATE, song_ids=("song-current",)),
        ]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        assert (await harness.state_service.dispatch(ActionName.START_NEXT)).accepted is True

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: (
                value.service_load.status is ServiceLoadStatus.LOADED
                and value.plan is not None
                and value.plan.id == "plan-today"
            ),
        )

        assert state.plan is not None
        assert state.plan.date == SERVICE_DATE
        assert state.current_song is None
        assert state.current_song_index is None
        assert state.next_song and state.next_song.id == "song-current"
        assert state.service_load.target_date == SERVICE_DATE
        assert state.service_load.is_stale is False
        reload_statuses = [
            event.payload.status
            for event in planning_center_events(harness, EventType.SERVICE_LOAD_CHANGED)[2:]
            if isinstance(event.payload, ServiceLoadPayload)
        ]
        assert ServiceLoadStatus.NOT_FOUND not in reload_statuses
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_date_rollover_clears_old_plan_instead_of_marking_it_stale() -> None:
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-1", warning_id="old-warning"),
            PlanningCenterTimeoutError("Planning Center request timed out."),
        ]
    )
    today = MutableToday(SERVICE_DATE)
    harness = await plugin_harness(client, today=today)
    try:
        await harness.plugin.start()
        assert (await harness.state_service.dispatch(ActionName.START_NEXT)).accepted is True
        successful_reload_at = (await harness.state_store.snapshot()).last_successful_plan_reload_at
        today.value = NEXT_DATE

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: (
                value.service_load.status is ServiceLoadStatus.ERROR
                and value.service_load.target_date == NEXT_DATE
            ),
        )

        assert state.plan is None
        assert state.current_song is None
        assert state.current_song_index is None
        assert state.next_song is None
        assert state.service_load.skipped_items == []
        assert state.service_load.is_stale is False
        assert state.last_successful_plan_reload_at == successful_reload_at
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_successful_reload_recovers_after_a_transient_failure() -> None:
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-1", warning_id="old-warning"),
            PlanningCenterTimeoutError("Planning Center request timed out."),
            loaded_result(
                "plan-2",
                song_ids=("song-b", "song-c"),
                warning_id="new-warning",
            ),
        ]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        await wait_for_state(
            harness.state_store,
            lambda value: value.service_load.status is ServiceLoadStatus.ERROR,
        )
        await settle_scheduled_refresh()

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: (
                value.service_load.status is ServiceLoadStatus.LOADED
                and value.plan is not None
                and value.plan.id == "plan-2"
            ),
        )

        assert state.planning_center_status is ConnectionStatus.CONNECTED
        assert state.service_load.is_stale is False
        assert state.service_load.candidates == []
        assert [item.item_id for item in state.service_load.skipped_items] == ["new-warning"]
        health = await harness.plugin.health()
        assert health.status is PluginStatus.RUNNING
        assert health.last_error is None
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_successful_reload_preserves_current_song_by_stable_item_id() -> None:
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-1", song_ids=("song-a", "song-b")),
            loaded_result("plan-2", song_ids=("song-b", "song-a", "song-c")),
        ]
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        assert (await harness.state_service.dispatch(ActionName.START_NEXT)).accepted is True

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: (
                value.service_load.status is ServiceLoadStatus.LOADED
                and value.plan is not None
                and value.plan.id == "plan-2"
            ),
        )

        assert state.current_song and state.current_song.id == "song-a"
        assert state.current_song_index == 1
        assert state.next_song and state.next_song.id == "song-c"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_service_projection_subscriber_failure_cannot_report_loaded() -> None:
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-1", song_ids=("song-a",)),
            loaded_result("plan-1", song_ids=("song-b",)),
        ]
    )
    harness = await plugin_harness(client)

    async def fail_projection_observer(_event: StagePilotEvent) -> None:
        raise RuntimeError("projection observer failed")

    failing_subscription: Subscription | None = None
    try:
        await harness.plugin.start()
        failing_subscription = await harness.event_bus.subscribe(
            EventType.SERVICE_LOADED,
            fail_projection_observer,
        )

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        state = await wait_for_state(
            harness.state_store,
            lambda value: value.service_load.status is ServiceLoadStatus.ERROR,
        )

        assert state.service_load.message == (
            "Planning Center plan projection failed unexpectedly."
        )
        assert state.service_load.is_stale is True
        assert (await harness.plugin.health()).status is PluginStatus.ERROR
    finally:
        if failing_subscription is not None:
            await harness.event_bus.unsubscribe(failing_subscription)
        await harness.close()


@pytest.mark.asyncio
async def test_reload_during_blocked_startup_runs_after_initial_refresh() -> None:
    client = FakePlanningCenterClient(
        [loaded_result("plan-1"), loaded_result("plan-2")],
        blocked_call_indexes={0},
    )
    harness = await plugin_harness(client)
    try:
        start_task = asyncio.create_task(harness.plugin.start())
        await asyncio.wait_for(client.load_entered.wait(), timeout=2)

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        assert len(client.load_calls) == 1
        assert client.max_in_flight == 1

        client.release_load.set()
        await start_task
        state = await harness.state_store.snapshot()

        assert state.plan and state.plan.id == "plan-2"
        assert len(client.load_calls) == 2
        assert client.max_in_flight == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_stop_during_blocked_startup_cancels_before_client_close() -> None:
    client = FakePlanningCenterClient(
        [loaded_result("plan-1")],
        blocked_call_indexes={0},
    )
    harness = await plugin_harness(client)
    try:
        start_task = asyncio.create_task(harness.plugin.start())
        await asyncio.wait_for(client.load_entered.wait(), timeout=2)

        await harness.plugin.stop()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        assert client.load_cancelled.is_set()
        assert client.operation_log == ["load_cancelled", "close"]
        assert client.close_calls == 1
        assert planning_center_events(harness, EventType.SERVICE_LOADED) == []
        assert (await harness.plugin.health()).status is PluginStatus.STOPPED
        assert (await harness.state_store.snapshot()).planning_center_status is (
            ConnectionStatus.DISCONNECTED
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_concurrent_reload_requests_are_single_flight_and_coalesced() -> None:
    client = FakePlanningCenterClient(
        [
            loaded_result("plan-1"),
            loaded_result("plan-2"),
            loaded_result("plan-3"),
        ],
        blocked_call_indexes={1},
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        first = asyncio.create_task(
            harness.event_bus.publish(
                new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test-first")
            )
        )
        await asyncio.wait_for(client.load_entered.wait(), timeout=2)
        loading_state = await harness.state_store.snapshot()
        assert loading_state.service_load.status is ServiceLoadStatus.LOADING
        assert loading_state.service_load.is_stale is True
        second = asyncio.create_task(
            harness.event_bus.publish(
                new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test-second")
            )
        )
        await asyncio.gather(first, second)
        assert len(client.load_calls) == 2

        client.release_load.set()
        state = await wait_for_state(
            harness.state_store,
            lambda value: (
                value.service_load.status is ServiceLoadStatus.LOADED
                and value.plan is not None
                and value.plan.id == "plan-3"
            ),
        )
        await settle_scheduled_refresh()

        assert state.service_load.status is ServiceLoadStatus.LOADED
        assert len(client.load_calls) == 3
        assert client.list_calls == 3
        assert client.max_in_flight == 1
        assert len(planning_center_events(harness, EventType.SERVICE_LOADED)) == 3
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_stop_closes_once_and_unsubscribes_reload_and_selection_handlers() -> None:
    client = FakePlanningCenterClient([loaded_result("plan-1")])
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        calls_before_stop = len(client.load_calls)

        await harness.plugin.stop()
        await harness.plugin.stop()
        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        await harness.event_bus.publish(
            new_event(
                EventType.SERVICE_PLAN_SELECTION_REQUESTED,
                source="test",
                payload=ServicePlanSelectionPayload(plan_id="plan-1"),
            )
        )
        await settle_scheduled_refresh()

        assert client.close_calls == 1
        assert len(client.load_calls) == calls_before_stop
        assert (await harness.plugin.health()).status is PluginStatus.STOPPED
        assert (await harness.state_store.snapshot()).planning_center_status is (
            ConnectionStatus.DISCONNECTED
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_close_failure_is_generic_stopped_unsubscribed_and_idempotent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private-close-secret"
    client = FakePlanningCenterClient(
        [loaded_result("plan-1")],
        close_error=RuntimeError(f"close failed with {secret}"),
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        calls_before_stop = len(client.load_calls)

        with pytest.raises(PlanningCenterError) as raised:
            await harness.plugin.stop()

        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        await harness.plugin.stop()
        await settle_scheduled_refresh()

        state = await harness.state_store.snapshot()
        health = await harness.plugin.health()
        public_text = "\n".join(
            [
                str(raised.value),
                state.model_dump_json(),
                health.model_dump_json(),
                *(event.model_dump_json() for event in harness.events),
                caplog.text,
            ]
        )
        assert str(raised.value) == "Planning Center client shutdown failed."
        assert client.close_calls == 1
        assert len(client.load_calls) == calls_before_stop
        assert state.planning_center_status is ConnectionStatus.DISCONNECTED
        assert health.status is PluginStatus.STOPPED
        assert health.last_error == "Planning Center client shutdown failed."
        assert secret not in public_text
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_stop_cancels_inflight_refresh_before_closing_client() -> None:
    client = FakePlanningCenterClient(
        [loaded_result("plan-1"), loaded_result("plan-2")],
        blocked_call_indexes={1},
    )
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()
        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        await asyncio.wait_for(client.load_entered.wait(), timeout=2)

        await harness.plugin.stop()

        assert client.load_cancelled.is_set()
        assert client.close_calls == 1
        assert client.operation_log == ["load_cancelled", "close"]
        assert client.in_flight == 0
        assert (await harness.state_store.snapshot()).planning_center_status is (
            ConnectionStatus.DISCONNECTED
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (PlanningCenterSettings(), "credentials are not configured"),
        (
            PlanningCenterSettings(app_id="test-app-id", secret="test-secret"),
            "service type must be configured",
        ),
    ],
)
async def test_configuration_failure_is_safe_and_does_not_construct_client(
    settings: PlanningCenterSettings,
    message: str,
) -> None:
    client = FakePlanningCenterClient([])
    harness = await plugin_harness(client, settings=settings)
    try:
        with pytest.raises(PlanningCenterConfigurationError, match=message):
            await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert harness.factory.calls == 0
        assert client.list_calls == 0
        assert client.close_calls == 0
        assert state.planning_center_status is ConnectionStatus.ERROR
        assert state.service_load.status is ServiceLoadStatus.ERROR
        assert state.service_load.message and message in state.service_load.message
        assert (await harness.plugin.health()).status is PluginStatus.ERROR
        await harness.event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test")
        )
        await settle_scheduled_refresh()
        assert client.list_calls == 0
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("available_types", "message"),
    [
        ([service_type("7", "Other Services")], "service type is unavailable"),
        ([service_type("42", archived=True)], "service type is archived"),
    ],
)
async def test_invalid_configured_service_type_does_not_attempt_plan_load(
    available_types: list[PlanningCenterServiceType],
    message: str,
) -> None:
    client = FakePlanningCenterClient([], service_types=available_types)
    harness = await plugin_harness(client)
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        assert client.list_calls == 1
        assert client.load_calls == []
        assert state.planning_center_status is ConnectionStatus.CONNECTED
        assert state.service_load.status is ServiceLoadStatus.ERROR
        assert state.service_load.message and message in state.service_load.message
        assert planning_center_events(harness, EventType.SERVICE_LOADED) == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_unexpected_failure_never_exposes_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app_id = "private-app-id"
    secret = "private-secret"
    client = FakePlanningCenterClient(
        [RuntimeError(f"unexpected failure containing {app_id}:{secret}")]
    )
    harness = await plugin_harness(
        client,
        settings=PlanningCenterSettings(
            app_id=app_id,
            secret=secret,
            service_type_id="42",
        ),
    )
    try:
        await harness.plugin.start()

        state = await harness.state_store.snapshot()
        health = await harness.plugin.health()
        public_text = "\n".join(
            [
                state.model_dump_json(),
                health.model_dump_json(),
                *(event.model_dump_json() for event in harness.events),
                caplog.text,
            ]
        )
        assert state.planning_center_status is ConnectionStatus.ERROR
        assert state.service_load.status is ServiceLoadStatus.ERROR
        assert state.service_load.message == "Planning Center plan loading failed unexpectedly."
        assert health.last_error == "Planning Center plan loading failed unexpectedly."
        assert app_id not in public_text
        assert secret not in public_text
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_unexpected_factory_failure_is_sanitized_and_cleaned_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private-factory-secret"
    event_bus = EventBus()
    state_store = StateStore()
    state_service = StateService(event_bus, state_store)
    await state_service.start()
    events: list[StagePilotEvent] = []
    event_subscription = await event_bus.subscribe(None, events.append)

    def failing_factory(
        _settings: PlanningCenterSettings,
    ) -> PlanningCenterClientContract:
        raise RuntimeError(f"factory failed with {secret}")

    plugin = PlanningCenterPlugin(
        event_bus,
        state_store,
        configured_settings(),
        timezone_name=TIMEZONE_NAME,
        client_factory=failing_factory,
        today_provider=MutableToday(SERVICE_DATE),
    )
    try:
        with pytest.raises(PlanningCenterError) as raised:
            await plugin.start()

        state = await state_store.snapshot()
        health = await plugin.health()
        public_text = "\n".join(
            [
                str(raised.value),
                state.model_dump_json(),
                health.model_dump_json(),
                *(event.model_dump_json() for event in events),
                caplog.text,
            ]
        )
        assert str(raised.value) == ("Planning Center plugin initialization failed unexpectedly.")
        assert health.status is PluginStatus.ERROR
        assert state.planning_center_status is ConnectionStatus.ERROR
        assert state.service_load.status is ServiceLoadStatus.ERROR
        assert state.service_load.message == (
            "Planning Center plugin initialization failed unexpectedly."
        )
        assert secret not in public_text

        await event_bus.publish(new_event(EventType.SERVICE_RELOAD_REQUESTED, source="test"))
        await settle_scheduled_refresh()
        assert (await plugin.health()).status is PluginStatus.ERROR
    finally:
        await plugin.stop()
        await state_service.stop()
        await event_bus.unsubscribe(event_subscription)


@pytest.mark.asyncio
async def test_reconfigure_applies_new_service_type_to_running_plugin_without_restart() -> None:
    """Regression test for the beta.8 stale-settings bug.

    Saving settings with an unchanged App ID/Secret but a different
    service_type_id must reach the already-running plugin instance: a fresh
    client is built from the new settings, the connection is refreshed in
    place, and the live plugin's cached settings/plan reflect the change --
    all without stopping or recreating the Plugin object itself.
    """
    initial_client = FakePlanningCenterClient(
        [loaded_result("plan-old")],
        service_types=[service_type("42", "Weekend Services")],
    )
    today = MutableToday(SERVICE_DATE)
    harness = await plugin_harness(initial_client, today=today)
    try:
        await harness.plugin.start()
        state = await harness.state_store.snapshot()
        assert state.plan and state.plan.id == "plan-old"
        assert harness.factory.calls == 1

        new_client = FakePlanningCenterClient(
            [loaded_result("plan-new")],
            service_types=[service_type("99", "Youth Services")],
        )
        harness.factory.client = new_client
        new_settings = configured_settings().model_copy(update={"service_type_id": "99"})

        outcome = await harness.plugin.reconfigure(new_settings)

        assert outcome.accepted is True
        assert harness.factory.calls == 2
        assert harness.factory.settings[-1].service_type_id == "99"
        assert initial_client.close_calls == 1

        state = await harness.state_store.snapshot()
        assert state.plan and state.plan.id == "plan-new"
        assert state.planning_center_status is ConnectionStatus.CONNECTED

        health = await harness.plugin.health()
        assert health.status is PluginStatus.RUNNING

        connection_events = planning_center_events(harness, EventType.CONNECTION_CHANGED)
        assert ConnectionStatus.CONNECTED in [
            event.payload.status
            for event in connection_events
            if isinstance(event.payload, ConnectionPayload)
        ]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_reconfigure_surfaces_failure_without_killing_the_plugin() -> None:
    """A reconfigure() that fails (invalid new service type) must not be
    swallowed silently -- it reports an ERROR connection status and a
    rejected ActionOutcome, while the plugin itself stays alive.
    """
    initial_client = FakePlanningCenterClient(
        [loaded_result("plan-old")],
        service_types=[service_type("42", "Weekend Services")],
    )
    today = MutableToday(SERVICE_DATE)
    harness = await plugin_harness(initial_client, today=today)
    try:
        await harness.plugin.start()

        broken_client = FakePlanningCenterClient(
            [PlanningCenterConfigurationError("boom")],
            service_types=[service_type("99", "Youth Services")],
        )
        harness.factory.client = broken_client
        new_settings = configured_settings().model_copy(update={"service_type_id": "99"})

        outcome = await harness.plugin.reconfigure(new_settings)

        assert outcome.accepted is False
        health = await harness.plugin.health()
        assert health.status is PluginStatus.ERROR
        assert health.last_error is not None
        state = await harness.state_store.snapshot()
        assert state.service_load.status is ServiceLoadStatus.ERROR

        # The plugin itself must survive the failed reconfigure: a follow-up
        # reconfigure() with valid settings must still succeed rather than
        # the plugin being left dead.
        recovered_client = FakePlanningCenterClient(
            [loaded_result("plan-recovered")],
            service_types=[service_type("42", "Weekend Services")],
        )
        harness.factory.client = recovered_client
        recovery_outcome = await harness.plugin.reconfigure(configured_settings())
        assert recovery_outcome.accepted is True
        state = await harness.state_store.snapshot()
        assert state.plan and state.plan.id == "plan-recovered"
    finally:
        await harness.close()
