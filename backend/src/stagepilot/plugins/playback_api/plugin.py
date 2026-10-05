"""Selected input lifecycle, bounded observations and explicit order discovery."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from stagepilot.core.actions import ActionDispatcher
from stagepilot.core.config import MidiSource, Settings
from stagepilot.core.event_bus import EventBus
from stagepilot.core.events import ConnectionPayload, EventType, new_event
from stagepilot.core.plugin import Plugin
from stagepilot.core.settings import PersistentSettings, SettingsService
from stagepilot.core.state import StateStore
from stagepilot.models.state import ConnectionStatus, PluginHealth, PluginStatus
from stagepilot.plugins.midi_playback import MidiPlaybackPlugin
from stagepilot.plugins.playback_api.client import (
    ConnectionOptions,
    Observer,
    PlaybackClient,
    PlaybackStatus,
)
from stagepilot.plugins.playback_api.mapping import Observation, PlaybackMapper
from stagepilot.plugins.playback_api.normalizer import Heartbeat, PlaybackEvent

ClientFactory = Callable[[ConnectionOptions, Observer], PlaybackClient]
MidiFactory = Callable[[Settings], MidiPlaybackPlugin]


def default_client(options: ConnectionOptions, observer: Observer) -> PlaybackClient:
    return PlaybackClient(options, observer=observer)


class DiscoveryConflict(ValueError):
    """Operator must resolve a live-state conflict before discovery."""


@dataclass(frozen=True)
class MonitorEntry:
    event: PlaybackEvent
    discovery: bool


class PlaybackInputPlugin(Plugin):
    version = "0.1.0"

    def __init__(
        self,
        event_bus: EventBus,
        state_store: StateStore,
        settings: Settings,
        dispatcher: ActionDispatcher,
        settings_service: SettingsService,
        midi_factory: MidiFactory,
        *,
        client_factory: ClientFactory = default_client,
        step_timeout: float = 5.0,
        max_steps: int = 200,
    ) -> None:
        super().__init__(event_bus, state_store)
        if step_timeout <= 0 or max_steps < 1:
            raise ValueError("Discovery limits must be positive")
        self.name = (
            "midi_playback"
            if settings.integration_modes.midi_source is MidiSource.REAL
            else "playback_api"
        )
        self.settings = settings.model_copy(deep=True)
        self._service = settings_service
        self._midi_factory = midi_factory
        self._client_factory = client_factory
        self.midi: MidiPlaybackPlugin | None = None
        if settings.integration_modes.midi_source is MidiSource.REAL and settings.midi.enabled:
            self.midi = midi_factory(settings)
        self.mapper = PlaybackMapper(dispatcher, state_store)
        saved = settings.playback_api
        if saved.song_order and saved.captured_version is not None:
            self.mapper.install_order(tuple(saved.song_order), saved.captured_version)
        self.client: PlaybackClient | None = None
        self._queue: asyncio.Queue[tuple[int, Observation | ConnectionPayload]] = asyncio.Queue(64)
        self._history: deque[MonitorEntry] = deque(maxlen=100)
        self._consumer: asyncio.Task[None] | None = None
        self._generation = 0
        self._running = False
        self._active = False
        self._lifecycle = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._discovery_task: asyncio.Task[object] | None = None
        self.discovery: Literal["idle", "running", "failed", "done"] = "idle"
        self.progress = 0
        self.discovery_error: str | None = None
        self._changed = asyncio.Event()
        self._heartbeat: Heartbeat | None = None
        self._serial = 0
        self._fresh_at = 0.0
        self._connection = False
        self._step_timeout = step_timeout
        self._max_steps = max_steps

    @property
    def selected(self) -> bool:
        return self.settings.integration_modes.midi_source is MidiSource.PLAYBACK_API

    @property
    def events(self) -> tuple[MonitorEntry, ...]:
        return tuple(self._history)

    @property
    def status(self) -> PlaybackStatus:
        return (
            self.client.status
            if self.client is not None
            else PlaybackStatus(port=self.settings.playback_api.port)
        )

    def _options(self) -> ConnectionOptions:
        config = self.settings.playback_api
        return ConnectionOptions(
            config.enabled and self.selected, config.host, config.port, config.auto_scan
        )

    async def start(self) -> None:
        async with self._lifecycle:
            if self._running:
                return
            self._running = True
            self._consumer = asyncio.create_task(self._consume(), name="playback-input-consumer")
            await self._start_selected()

    async def _start_selected(self) -> None:
        if self.selected and self.settings.playback_api.enabled:
            self._active = True
            generation = self._generation
            self.client = self._client_factory(
                self._options(), lambda status, events: self._observe(generation, status, events)
            )
            await self.client.start()
        elif (
            self.settings.integration_modes.midi_source is MidiSource.REAL
            and self.settings.midi.enabled
        ):
            if self.midi is None:
                self.midi = self._midi_factory(self.settings)
            await self.midi.start()

    async def _stop_selected(self) -> None:
        self._active = False
        self._generation += 1
        self.mapper.discard_pending()
        if self.client is not None:
            await self.client.stop()
        if self.midi is not None:
            await self.midi.stop()
        self._heartbeat = None
        self._connection = False
        self._changed.set()
        while not self._queue.empty():
            self._queue.get_nowait()
        await self.event_bus.publish(
            new_event(
                EventType.CONNECTION_CHANGED,
                source="playback_api",
                payload=ConnectionPayload(integration="midi", status=ConnectionStatus.DISCONNECTED),
            )
        )

    async def stop(self) -> None:
        task = self._discovery_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        async with self._lifecycle:
            self._running = False
            # Finish any action already in flight before releasing input resources.
            async with self._dispatch_lock:
                await self._stop_selected()
            if self._consumer is not None:
                self._consumer.cancel()
                with suppress(asyncio.CancelledError):
                    await self._consumer
                self._consumer = None

    async def save_settings(self, settings: PersistentSettings) -> None:
        async with self._lifecycle:
            if self.discovery == "running":
                raise DiscoveryConflict("Wait for Discover Song Order before changing settings.")
            saved = self._service.snapshot().playback_api
            if (
                settings.playback_api.song_order,
                settings.playback_api.captured_version,
                settings.playback_api.captured_at,
            ) != (saved.song_order, saved.captured_version, saved.captured_at):
                raise ValueError("Song order can only be changed by Discover Song Order.")
            config = settings.playback_api
            ConnectionOptions(config.enabled, config.host, config.port, config.auto_scan)
            self._service.save(settings)
            await self._reconfigure(self._service.effective_runtime_settings())

    async def reconfigure(self, settings: Settings) -> None:
        async with self._lifecycle:
            if self.discovery == "running":
                raise DiscoveryConflict("Wait for Discover Song Order before changing inputs.")
            await self._reconfigure(settings)

    async def _reconfigure(self, settings: Settings) -> None:
        same_input = (
            self.settings.integration_modes.midi_source == settings.integration_modes.midi_source
            and self.settings.midi == settings.midi
            and self.settings.playback_api == settings.playback_api
        )
        if same_input:
            return
        if (
            self.midi is not None
            and self.settings.integration_modes.midi_source is MidiSource.REAL
            and settings.integration_modes.midi_source is MidiSource.REAL
            and settings.midi.enabled
            and self.settings.midi.enabled
            and self._running
        ):
            await self.midi.reconfigure(settings.midi)
            self.settings = settings.model_copy(deep=True)
            return
        async with self._dispatch_lock:
            await self._stop_selected()
            self.settings = settings.model_copy(deep=True)
            self.midi = None
            self.client = None
            if self._running:
                await self._start_selected()

    def _observe(
        self, generation: int, status: PlaybackStatus, events: tuple[PlaybackEvent, ...]
    ) -> None:
        if not self._active or generation != self._generation:
            return
        heartbeat = status.heartbeat
        fresh = heartbeat is not None and heartbeat is not self._heartbeat
        if fresh:
            self._heartbeat = heartbeat
            self._serial += 1
            self._fresh_at = time.monotonic()
        if not status.connected:
            self._heartbeat = None
            self.mapper.discard_pending()
        if fresh or not status.connected:
            self._changed.set()
        observations = self.mapper.observe(heartbeat if fresh else None, events)
        self._history.extend(MonitorEntry(item.event, item.discovery) for item in observations)
        if status.connected != self._connection:
            self._connection = status.connected
            self._put(
                (
                    generation,
                    ConnectionPayload(
                        integration="midi",
                        status=ConnectionStatus.CONNECTED
                        if status.connected
                        else ConnectionStatus.DISCONNECTED,
                        detail=status.last_error
                        or (
                            f"Playback connected at {status.host}:{status.port}"
                            if status.connected
                            else None
                        ),
                    ),
                )
            )
        for item in observations:
            self._put((generation, item))

    def _put(self, item: tuple[int, Observation | ConnectionPayload]) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Never replay overflow later. Monitor remains independently bounded.
            self.discovery_error = "Playback event queue full; an event was dropped."

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                async with self._dispatch_lock:
                    if not self._active or item[0] != self._generation:
                        continue
                    payload = item[1]
                    if isinstance(payload, ConnectionPayload):
                        await self.event_bus.publish(
                            new_event(
                                EventType.CONNECTION_CHANGED, source="playback_api", payload=payload
                            )
                        )
                    elif self.status.connected:
                        await self.mapper.dispatch(payload)
            except Exception:
                self.discovery_error = "Playback action processing failed."

    async def health(self) -> PluginHealth:
        if self.midi is not None and not self.selected:
            return await self.midi.health()
        # Health describes the lifecycle supervisor, not external connectivity.
        status = PluginStatus.RUNNING if self._running else PluginStatus.STOPPED
        if self._consumer is not None and self._consumer.done() and self._running:
            status = PluginStatus.ERROR
        return PluginHealth(
            name=self.name,
            version=self.version,
            status=status,
            last_error=self.discovery_error or self.status.last_error,
        )

    async def find(self) -> PlaybackStatus:
        async with self._lifecycle:
            if self.discovery == "running":
                raise DiscoveryConflict("Wait for Discover Song Order before scanning.")
            if not self.selected or not self.settings.playback_api.enabled:
                raise DiscoveryConflict("Select and enable Playback API before scanning.")
            # Restart the selected receive lifecycle: its finder validates heartbeat,
            # preserves manual override/local-first policy, and reconnects to the result.
            async with self._dispatch_lock:
                await self._stop_selected()
                await self._start_selected()
            return self.status

    def _check_discovery(self, version: int | None = None) -> Heartbeat:
        heartbeat = self._heartbeat
        if not self._active or not self.status.connected or heartbeat is None:
            raise DiscoveryConflict(
                "Playback is disconnected. Enable remote connections and connect first."
            )
        if heartbeat.playing:
            raise DiscoveryConflict("Stop Playback before discovering song order.")
        if heartbeat.setlist_version is None:
            raise DiscoveryConflict("Playback has not supplied a valid setlist version.")
        if version is not None and heartbeat.setlist_version != version:
            raise DiscoveryConflict("Playback setlist changed during discovery; try again.")
        return heartbeat

    async def _step(self, direction: Literal["previous", "next"], version: int) -> int:
        before = self._check_discovery(version).song_id
        if self.progress >= self._max_steps:
            raise DiscoveryConflict("Song order discovery exceeded the 200-step safety limit.")
        client = self.client
        assert client is not None
        serial = self._serial
        await client._discovery_step(direction)
        self.progress += 1
        deadline = time.monotonic() + self._step_timeout
        while True:
            current = self._check_discovery(version)
            if current.song_id != before:
                return current.song_id
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # At a nonwrapping end, Playback continues sending fresh unchanged
                # heartbeats. Silence is NOT evidence of an end.
                if (
                    self._serial >= serial + 2
                    and time.monotonic() - self._fresh_at < self._step_timeout / 2
                ):
                    return before
                raise DiscoveryConflict("Playback heartbeat timed out during song order discovery.")
            self._changed.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)

    async def discover_song_order(self) -> None:
        async with self._lifecycle:
            if self.discovery == "running":
                raise DiscoveryConflict("Song order discovery is already running.")
            heartbeat = self._check_discovery()
            async with self._dispatch_lock:
                self.mapper.set_discovery(True)
                self.discovery = "running"
                self.progress = 0
                self.discovery_error = None
                self._discovery_task = asyncio.current_task()
        original = heartbeat.song_id
        version = heartbeat.setlist_version
        assert version is not None
        try:
            current = original
            visited = {current}
            while True:
                previous = await self._step("previous", version)
                if previous == current:
                    break
                if previous in visited:
                    raise DiscoveryConflict("Playback navigation wrapped; order discovery aborted.")
                visited.add(previous)
                current = previous
            order = [current]
            while True:
                following = await self._step("next", version)
                if following == current:
                    break
                if following in order:
                    raise DiscoveryConflict("Playback navigation wrapped; order discovery aborted.")
                order.append(following)
                current = following
            if original not in order:
                raise DiscoveryConflict("Original Playback selection was lost during discovery.")
            for expected in reversed(order[order.index(original) : -1]):
                if await self._step("previous", version) != expected:
                    raise DiscoveryConflict("Could not restore the original Playback selection.")
            self._check_discovery(version)
            saved = self.settings.playback_api.model_copy(
                update={
                    "song_order": order,
                    "captured_version": version,
                    "captured_at": datetime.now(UTC),
                }
            )
            self._service.save(
                self._service.snapshot().model_copy(update={"playback_api": saved}, deep=True)
            )
            self.settings.playback_api = saved
            self.mapper.install_order(tuple(order), version)
            self.mapper.observe(self._heartbeat, ())
            self.discovery = "done"
        except asyncio.CancelledError:
            self.discovery = "failed"
            self.discovery_error = (
                "Song order discovery cancelled; no result saved. Check Playback selection."
            )
            raise
        except Exception as exc:
            self.discovery = "failed"
            self.discovery_error = f"{exc} No result saved; check Playback selection."
        finally:
            self.mapper.set_discovery(False)
            self._discovery_task = None
