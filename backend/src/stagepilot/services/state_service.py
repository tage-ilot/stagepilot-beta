"""Event-driven service plan navigation and state projection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from stagepilot.core.actions import ActionOutcome
from stagepilot.core.event_bus import EventBus, Subscription
from stagepilot.core.events import (
    ActionName,
    ActionPayload,
    ConnectionPayload,
    EventType,
    PluginPayload,
    ServiceLoadPayload,
    ServicePayload,
    SongPayload,
    StagePilotEvent,
    TimerPayload,
    new_event,
)
from stagepilot.core.state import StateStore
from stagepilot.models.state import (
    ApplicationState,
    ApplicationStatus,
    ConnectionStatus,
    ErrorSummary,
    EventSummary,
    PluginHealth,
    ServiceLoadState,
    ServiceLoadStatus,
    ServicePlan,
    TimerState,
    TimerStatus,
)


class StateService:
    """Apply domain events to state and execute all navigation actions consistently."""

    def __init__(
        self,
        event_bus: EventBus,
        state_store: StateStore,
        *,
        recent_event_limit: int = 100,
        recent_error_limit: int = 50,
    ) -> None:
        self._event_bus = event_bus
        self._state_store = state_store
        self._recent_event_limit = recent_event_limit
        self._recent_error_limit = recent_error_limit
        self._subscriptions: list[Subscription] = []
        self._pending_start_index = 0
        self._action_lock = asyncio.Lock()
        self._action_results: dict[UUID, ActionOutcome] = {}

    async def start(self) -> None:
        handlers: tuple[
            tuple[EventType | None, Callable[[StagePilotEvent], Coroutine[Any, Any, None]]], ...
        ] = (
            (EventType.ACTION_REQUESTED, self._handle_action),
            (EventType.SERVICE_LOAD_CHANGED, self._handle_service_load_changed),
            (EventType.SERVICE_LOADED, self._handle_service_loaded),
            (EventType.TIMER_STARTED, self._handle_timer_started),
            (EventType.TIMER_STOPPED, self._handle_timer_stopped),
            (EventType.TIMER_FAILED, self._handle_timer_failed),
            (EventType.CONNECTION_CHANGED, self._handle_connection_changed),
            (EventType.PLUGIN_STATUS_CHANGED, self._handle_plugin_status),
            (EventType.PLUGIN_FAILED, self._handle_plugin_status),
            (EventType.APPLICATION_STARTED, self._handle_application_lifecycle),
            (EventType.APPLICATION_STOPPING, self._handle_application_lifecycle),
            (None, self._record_event),
        )
        for event_type, handler in handlers:
            self._subscriptions.append(await self._event_bus.subscribe(event_type, handler))

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            await self._event_bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def dispatch(self, action: ActionName, source: str = "api") -> ActionOutcome:
        request_id = uuid4()
        report = await self._event_bus.publish(
            new_event(
                EventType.ACTION_REQUESTED,
                source=source,
                payload=ActionPayload(action=action, request_id=request_id),
            )
        )
        outcome = self._action_results.pop(request_id, None)
        if outcome is not None:
            return outcome
        if report.failures:
            return ActionOutcome(False, "The action handler failed; see recent errors.")
        return ActionOutcome(False, "No service is available to handle this action.")

    async def dispatch_song_position(
        self,
        position: int,
        source: str = "api",
    ) -> ActionOutcome:
        """Start an exact one-based song position without changing manual-control semantics."""
        async with self._action_lock:
            return await self._start_song_position(position, source=source)

    async def _handle_action(self, event: StagePilotEvent) -> None:
        if not isinstance(event.payload, ActionPayload):
            return
        async with self._action_lock:
            handlers = {
                ActionName.START_NEXT: self._start_next,
                ActionName.RESTART_CURRENT: self._restart_current,
                ActionName.PREVIOUS: self._previous,
                ActionName.NEXT: self._next,
                ActionName.STOP_TIMER: self._stop_timer,
                ActionName.RELOAD_PLAN: self._reload_plan,
                ActionName.RESET_POSITION: self._reset_position,
            }
            outcome = await handlers[event.payload.action]()
            self._action_results[event.payload.request_id] = outcome

    async def _start_next(self) -> ActionOutcome:
        state = await self._state_store.snapshot()
        songs = state.plan.songs if state.plan else []
        if not songs:
            return ActionOutcome(False, "No songs are loaded.")
        target = self._pending_start_index
        if target >= len(songs):
            return ActionOutcome(False, "Already at the final song.")
        song = songs[target]
        await self._select_song(target, ActionName.START_NEXT)
        if not song.duration_seconds:
            self._pending_start_index = target
            message = f'Cannot start "{song.title}": scheduled duration is missing or zero.'
            await self._set_timer_error(message)
            return ActionOutcome(False, message)
        self._pending_start_index = target + 1
        await self._event_bus.publish(
            new_event(
                EventType.SONG_STARTED,
                source="state_service",
                payload=SongPayload(song=song, index=target),
            )
        )
        return ActionOutcome(True, f'Started "{song.title}".')

    async def _start_song_position(self, position: int, *, source: str) -> ActionOutcome:
        state = await self._state_store.snapshot()
        songs = state.plan.songs if state.plan else []
        if not songs:
            return ActionOutcome(False, "No songs are loaded.")
        if position < 1 or position > len(songs):
            return ActionOutcome(
                False,
                f"Song velocity {position} does not match a loaded song (1-{len(songs)}).",
            )

        target = position - 1
        song = songs[target]
        await self._select_song(target, ActionName.START_NEXT)
        if not song.duration_seconds:
            self._pending_start_index = target
            message = f'Cannot start "{song.title}": scheduled duration is missing or zero.'
            await self._set_timer_error(message)
            return ActionOutcome(False, message)

        self._pending_start_index = target + 1
        await self._event_bus.publish(
            new_event(
                EventType.SONG_STARTED,
                source=source,
                payload=SongPayload(song=song, index=target),
            )
        )
        return ActionOutcome(True, f'Started song {position}, "{song.title}".')

    async def _restart_current(self) -> ActionOutcome:
        state = await self._state_store.snapshot()
        if state.current_song is None or state.current_song_index is None:
            return ActionOutcome(False, "No current song to restart.")
        if not state.current_song.duration_seconds:
            message = f'Cannot restart "{state.current_song.title}": duration is missing or zero.'
            await self._set_timer_error(message)
            return ActionOutcome(False, message)
        await self._set_last_action(ActionName.RESTART_CURRENT)
        await self._event_bus.publish(
            new_event(
                EventType.SONG_RESTARTED,
                source="state_service",
                payload=SongPayload(song=state.current_song, index=state.current_song_index),
            )
        )
        return ActionOutcome(True, f'Restarted "{state.current_song.title}".')

    async def _previous(self) -> ActionOutcome:
        state = await self._state_store.snapshot()
        if state.plan is None or not state.plan.songs:
            return ActionOutcome(False, "No songs are loaded.")
        if state.current_song_index is None or state.current_song_index == 0:
            return ActionOutcome(False, "Already at the first song.")
        target = state.current_song_index - 1
        self._pending_start_index = target
        await self._select_song(target, ActionName.PREVIOUS)
        return ActionOutcome(True, f'Selected "{state.plan.songs[target].title}".')

    async def _next(self) -> ActionOutcome:
        state = await self._state_store.snapshot()
        if state.plan is None or not state.plan.songs:
            return ActionOutcome(False, "No songs are loaded.")
        target = 0 if state.current_song_index is None else state.current_song_index + 1
        if target >= len(state.plan.songs):
            return ActionOutcome(False, "Already at the final song.")
        self._pending_start_index = target
        await self._select_song(target, ActionName.NEXT)
        return ActionOutcome(True, f'Selected "{state.plan.songs[target].title}".')

    async def _stop_timer(self) -> ActionOutcome:
        await self._set_last_action(ActionName.STOP_TIMER)
        await self._event_bus.publish(
            new_event(EventType.TIMER_STOP_REQUESTED, source="state_service")
        )
        return ActionOutcome(True, "Timer stop requested.")

    async def _reload_plan(self) -> ActionOutcome:
        await self._set_last_action(ActionName.RELOAD_PLAN)
        await self._event_bus.publish(
            new_event(EventType.SERVICE_RELOAD_REQUESTED, source="state_service")
        )
        return ActionOutcome(True, "Service plan reload requested.")

    async def _reset_position(self) -> ActionOutcome:
        self._pending_start_index = 0
        await self._event_bus.publish(
            new_event(EventType.TIMER_RESET_REQUESTED, source="state_service")
        )

        def mutation(state: ApplicationState) -> None:
            state.current_song = None
            state.current_song_index = None
            state.next_song = state.plan.songs[0] if state.plan and state.plan.songs else None
            state.timer = TimerState(status=TimerStatus.STOPPED)
            state.last_action = ActionName.RESET_POSITION

        await self._state_store.mutate(mutation)
        return ActionOutcome(True, "Service position and timer reset.")

    async def _select_song(self, index: int, action: ActionName) -> None:
        def mutation(state: ApplicationState) -> None:
            if state.plan is None:
                return
            state.current_song_index = index
            state.current_song = state.plan.songs[index]
            state.next_song = (
                state.plan.songs[index + 1] if index + 1 < len(state.plan.songs) else None
            )
            state.last_action = action

        snapshot = await self._state_store.mutate(mutation)
        if snapshot.current_song is not None:
            await self._event_bus.publish(
                new_event(
                    EventType.SONG_SELECTED,
                    source="state_service",
                    payload=SongPayload(song=snapshot.current_song, index=index),
                )
            )

    async def _handle_service_loaded(self, event: StagePilotEvent) -> None:
        if not isinstance(event.payload, ServicePayload):
            return
        incoming = event.payload.plan
        previous = await self._state_store.snapshot()
        preserved_index = self._safe_match(previous.plan, previous.current_song_index, incoming)
        self._pending_start_index = 0 if preserved_index is None else preserved_index + 1

        def mutation(state: ApplicationState) -> None:
            state.plan = incoming
            state.last_successful_plan_reload_at = datetime.now(UTC)
            if preserved_index is None:
                state.current_song = None
                state.current_song_index = None
                state.next_song = incoming.songs[0] if incoming.songs else None
            else:
                state.current_song_index = preserved_index
                state.current_song = incoming.songs[preserved_index]
                state.next_song = (
                    incoming.songs[preserved_index + 1]
                    if preserved_index + 1 < len(incoming.songs)
                    else None
                )

        await self._state_store.mutate(mutation)

    async def _handle_service_load_changed(self, event: StagePilotEvent) -> None:
        if not isinstance(event.payload, ServiceLoadPayload):
            return
        payload = event.payload
        before = await self._state_store.snapshot()
        is_date_rollover = (
            before.plan is not None
            and payload.target_date is not None
            and before.plan.date != payload.target_date
        )
        if is_date_rollover and before.timer.status is TimerStatus.RUNNING:
            await self._event_bus.publish(
                new_event(
                    EventType.TIMER_STOP_REQUESTED,
                    source="state_service",
                )
            )

        def mutation(state: ApplicationState) -> None:
            plan_type_invalidated = (
                state.plan is not None
                and payload.status is ServiceLoadStatus.NOT_FOUND
                and not payload.is_stale
            )
            if (
                state.plan is not None
                and payload.target_date is not None
                and state.plan.date != payload.target_date
            ) or plan_type_invalidated:
                state.plan = None
                state.current_song = None
                state.next_song = None
                state.current_song_index = None
                state.timer = TimerState(status=TimerStatus.STOPPED)
                self._pending_start_index = 0
            state.service_load = ServiceLoadState(
                status=payload.status,
                target_date=payload.target_date,
                candidates=payload.candidates,
                skipped_items=payload.skipped_items,
                message=payload.message,
                is_stale=payload.is_stale,
                last_attempt_at=event.timestamp,
            )

        await self._state_store.mutate(mutation)

    @staticmethod
    def _safe_match(
        previous_plan: ServicePlan | None,
        previous_index: int | None,
        incoming: ServicePlan,
    ) -> int | None:
        if previous_plan is None or previous_index is None:
            return None
        previous_song = previous_plan.songs[previous_index]
        id_matches = [
            index for index, song in enumerate(incoming.songs) if song.id == previous_song.id
        ]
        if len(id_matches) == 1:
            return id_matches[0]
        old_title_count = sum(song.title == previous_song.title for song in previous_plan.songs)
        title_matches = [
            index for index, song in enumerate(incoming.songs) if song.title == previous_song.title
        ]
        if old_title_count == 1 and len(title_matches) == 1:
            return title_matches[0]
        return None

    async def _handle_timer_started(self, event: StagePilotEvent) -> None:
        if not isinstance(event.payload, TimerPayload):
            return
        payload = event.payload

        def mutation(state: ApplicationState) -> None:
            state.timer = TimerState(
                status=TimerStatus.RUNNING,
                duration_seconds=payload.duration_seconds,
                started_at=payload.started_at or event.timestamp,
            )

        await self._state_store.mutate(mutation)

    async def _handle_timer_stopped(self, event: StagePilotEvent) -> None:
        def mutation(state: ApplicationState) -> None:
            state.timer = TimerState(
                status=TimerStatus.STOPPED,
                duration_seconds=state.timer.duration_seconds,
            )

        await self._state_store.mutate(mutation)

    async def _handle_timer_failed(self, event: StagePilotEvent) -> None:
        if not isinstance(event.payload, TimerPayload):
            return
        await self._set_timer_error(event.payload.message or "Timer operation failed.", event.id)

    async def _set_timer_error(self, message: str, event_id: UUID | None = None) -> None:
        def mutation(state: ApplicationState) -> None:
            state.timer = TimerState(status=TimerStatus.ERROR, last_error=message)
            state.recent_errors.append(
                ErrorSummary(component="timer", message=message, event_id=event_id)
            )
            state.recent_errors[:] = state.recent_errors[-self._recent_error_limit :]

        await self._state_store.mutate(mutation)

    async def _handle_connection_changed(self, event: StagePilotEvent) -> None:
        if not isinstance(event.payload, ConnectionPayload):
            return
        # Simulation can exercise domain behavior, but it is never evidence of
        # a verified external connection.
        if event.source == "demo":
            return
        payload = event.payload

        def mutation(state: ApplicationState) -> None:
            field_names = {
                "planning_center": "planning_center_status",
                "midi": "midi_status",
                "propresenter": "propresenter_status",
                "lights": "lights_status",
            }
            setattr(state, field_names[payload.integration], payload.status)
            if payload.status is ConnectionStatus.ERROR and payload.detail:
                state.recent_errors.append(
                    ErrorSummary(
                        component=payload.integration,
                        message=payload.detail,
                        event_id=event.id,
                    )
                )
                state.recent_errors[:] = state.recent_errors[-self._recent_error_limit :]

        await self._state_store.mutate(mutation)

    async def _handle_plugin_status(self, event: StagePilotEvent) -> None:
        if not isinstance(event.payload, PluginPayload):
            return
        payload = event.payload

        def mutation(state: ApplicationState) -> None:
            state.plugins[payload.name] = PluginHealth(
                name=payload.name,
                version=payload.version,
                status=payload.status,
                last_error=payload.error,
                last_activity_at=event.timestamp,
            )
            if event.type is EventType.PLUGIN_FAILED and payload.error:
                state.recent_errors.append(
                    ErrorSummary(
                        component=payload.name,
                        message=payload.error,
                        event_id=event.id,
                    )
                )
                state.recent_errors[:] = state.recent_errors[-self._recent_error_limit :]

        await self._state_store.mutate(mutation)

    async def _handle_application_lifecycle(self, event: StagePilotEvent) -> None:
        status = (
            ApplicationStatus.RUNNING
            if event.type is EventType.APPLICATION_STARTED
            else ApplicationStatus.STOPPING
        )
        await self._state_store.mutate(lambda state: setattr(state, "application_status", status))

    async def _set_last_action(self, action: ActionName) -> None:
        await self._state_store.mutate(lambda state: setattr(state, "last_action", action))

    async def _record_event(self, event: StagePilotEvent) -> None:
        def mutation(state: ApplicationState) -> None:
            state.recent_events.append(
                EventSummary(
                    id=event.id,
                    type=event.type,
                    timestamp=event.timestamp,
                    source=event.source,
                )
            )
            state.recent_events[:] = state.recent_events[-self._recent_event_limit :]

        await self._state_store.mutate(mutation)
