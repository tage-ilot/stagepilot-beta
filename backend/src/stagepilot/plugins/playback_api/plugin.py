"""Selected input lifecycle, bounded observations and explicit order discovery."""

from __future__ import annotations

import asyncio
import threading
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
from stagepilot.core.settings import PersistentSettings, SettingsFileError, SettingsService
from stagepilot.core.state import StateStore
from stagepilot.models.state import ConnectionStatus, PluginHealth, PluginStatus
from stagepilot.plugins.midi_playback import MidiPlaybackPlugin
from stagepilot.plugins.playback_api.arbiter import (
    ArbitratedDispatcher,
    PlaybackConnection,
    PlaybackSourceArbiter,
)
from stagepilot.plugins.playback_api.client import (
    ConnectionOptions,
    Observer,
    PlaybackClient,
    PlaybackStatus,
)
from stagepilot.plugins.playback_api.find import ScanCandidate, scan_candidates
from stagepilot.plugins.playback_api.mapping import Observation, PlaybackMapper
from stagepilot.plugins.playback_api.normalizer import Heartbeat, PlaybackEvent

ClientFactory = Callable[[ConnectionOptions, Observer], PlaybackClient]
MidiFactory = Callable[[Settings], MidiPlaybackPlugin]
Scanner = Callable[..., list[ScanCandidate]]

NOT_FOUND_REASON = (
    "Couldn't find Playback. Check that Playback is open, Remote Connections is on, "
    "and both computers are on the same network."
)
UNREACHABLE_REASON = "Playback isn't reachable. In Playback, turn on Remote Connections."


def default_scanner(**kwargs: object) -> list[ScanCandidate]:
    return scan_candidates(**kwargs)  # type: ignore[arg-type]


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
        scanner: Scanner = default_scanner,
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
        self._scanner = scanner
        self.scan_state: Literal["idle", "scanning", "found", "not_found"] = "idle"
        self.scan_candidates: list[ScanCandidate] = []
        self.scan_reason: str | None = None
        self.scan_current = 0
        self.scan_total = 0
        self._scan_task: asyncio.Task[None] | None = None
        self._scan_cancel = threading.Event()
        self.midi: MidiPlaybackPlugin | None = None
        if (
            settings.integration_modes.midi_source is not MidiSource.SIMULATED
            and settings.midi.enabled
        ):
            self.midi = midi_factory(settings)
        self.arbiter = PlaybackSourceArbiter()
        self._dispatcher = ArbitratedDispatcher(self.arbiter, dispatcher, state_store)
        self.mapper = PlaybackMapper(self._dispatcher, state_store)
        self._dispatcher.suppressed = lambda: self.mapper.discovery
        self._dispatcher.api_mapping_revision = lambda: self.mapper.revision
        self._ticker: asyncio.Task[None] | None = None
        self._published: tuple[bool, str] | None = None
        self._ownership_revision = 0
        self._wire_midi()
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
        self._connection_lock = asyncio.Lock()
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

    def _wire_midi(self) -> None:
        if self.midi is not None:
            self.midi._action_dispatcher = self._dispatcher
            self.midi.action_guard = self._midi_guard
            self.midi.ownership_revision = self._revision
            self.midi.connection_observer = self._midi_connection

    def _revision(self) -> int:
        self.arbiter.snapshot()
        return self.arbiter.revision

    def _midi_guard(self) -> str | None:
        if self.discovery == "running":
            return "ignored: song order discovery active"
        if self.arbiter.snapshot().active_source == "playback_api":
            return "ignored: Playback API active"
        return None

    async def _midi_connection(self, status: ConnectionStatus) -> None:
        self.arbiter.midi_connected = status is ConnectionStatus.CONNECTED
        await self._publish_connection()

    @property
    def connection(self) -> PlaybackConnection:
        result = self.arbiter.snapshot()
        # One plain sentence, owned here; the frontend never composes its own.
        if self.scan_state == "scanning":
            result.reason = "Looking for Playback…"
        elif result.active_source == "playback_api":
            host = self.status.host
            name = "this computer" if host == "127.0.0.1" else host
            result.reason = (
                f"Connected to Playback on {name}." if name else "Connected to Playback."
            )
        elif result.active_source == "none":
            if self.scan_state == "not_found":
                result.reason = NOT_FOUND_REASON
            elif self.selected and self.settings.playback_api.enabled:
                result.reason = (
                    f"Not connected. {UNREACHABLE_REASON}"
                    if self.status.last_error
                    else "Not connected. Looking for Playback…"
                )
            else:
                result.reason = "Not connected. Choose Scan Network to find Playback."
        return result

    async def _publish_connection(self) -> None:
        async with self._connection_lock:
            connection = self.connection
            if self._ownership_revision != self.arbiter.revision:
                self.mapper.discard_pending()
                self._ownership_revision = self.arbiter.revision
            key = (connection.connected, connection.reason)
            if key == self._published:
                return
            await self.event_bus.publish(
                new_event(
                    EventType.CONNECTION_CHANGED,
                    source="playback_connection",
                    payload=ConnectionPayload(
                        integration="midi",
                        status=ConnectionStatus.CONNECTED
                        if connection.connected
                        else ConnectionStatus.DISCONNECTED,
                        detail=connection.reason,
                    ),
                )
            )
            self._published = key

    async def _tick(self) -> None:
        while True:
            await self._publish_connection()
            await asyncio.sleep(0.05)

    async def start(self) -> None:
        async with self._lifecycle:
            if self._running:
                return
            self._running = True
            self._consumer = asyncio.create_task(self._consume(), name="playback-input-consumer")
            self._ticker = asyncio.create_task(self._tick(), name="playback-source-arbiter")
            await self._start_selected()

    async def _start_selected(self) -> None:
        if (
            self.settings.integration_modes.midi_source is not MidiSource.SIMULATED
            and self.settings.midi.enabled
        ):
            if self.midi is None:
                self.midi = self._midi_factory(self.settings)
                self._wire_midi()
            try:
                await self.midi.start()
            except RuntimeError:
                # Missing native MIDI capability must not stop the API listener.
                if not self.selected:
                    raise
        if self.selected and self.settings.playback_api.enabled:
            self._active = True
            generation = self._generation
            self.client = self._client_factory(
                self._options(), lambda status, events: self._observe(generation, status, events)
            )
            await self.client.start()

    async def _stop_selected(self, *, keep_midi: bool = False) -> None:
        self._active = False
        self._generation += 1
        self.mapper.discard_pending()
        self.arbiter.observe_api(False, False)
        await self._publish_connection()
        if self.client is not None:
            await self.client.stop()
        if self.midi is not None and not keep_midi:
            await self.midi.stop()
        self._heartbeat = None
        self._connection = False
        if not keep_midi:
            self.arbiter.midi_connected = False
        self._changed.set()
        while not self._queue.empty():
            self._queue.get_nowait()
        await self._publish_connection()

    async def stop(self) -> None:
        await self._cancel_scan()
        task = self._discovery_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        async with self._lifecycle:
            self._running = False
            if self._ticker is not None:
                self._ticker.cancel()
                with suppress(asyncio.CancelledError):
                    await self._ticker
                self._ticker = None
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
        self.arbiter.observe_api(status.connected, fresh)
        if self._ownership_revision != self.arbiter.revision:
            self.mapper.discard_pending()
            self._ownership_revision = self.arbiter.revision
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
            if self.arbiter.allows("playback_api"):
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
                        await self._publish_connection()
                    elif self.arbiter.allows("playback_api"):
                        await self._publish_connection()
                        await self.mapper.dispatch(payload)
            except Exception:
                self.discovery_error = "Playback action processing failed."

    async def health(self) -> PluginHealth:
        if self.midi is not None and not self.selected:
            return await self.midi.health()
        # Health describes the lifecycle supervisor, not external connectivity.
        status = PluginStatus.RUNNING if self._running else PluginStatus.STOPPED
        if self._running and any(
            task is not None and task.done() for task in (self._consumer, self._ticker)
        ):
            status = PluginStatus.ERROR
        return PluginHealth(
            name=self.name,
            version=self.version,
            status=status,
            last_error=None
            if self.connection.connected
            else self.discovery_error or self.status.last_error,
        )

    async def find(self, host: str | None = None) -> PlaybackStatus:
        """Start a scan. Turns Playback on and selects it first if needed."""
        if host is not None:
            host = host.strip() or None
            ConnectionOptions(True, host, self.settings.playback_api.port)
        if self.discovery == "running":
            raise DiscoveryConflict("Wait for Discover Song Order before scanning.")
        # Cancel before taking the lifecycle lock: the old scan may be waiting on it.
        await self._cancel_scan()
        async with self._lifecycle:
            self._scan_cancel = threading.Event()
            self.scan_state = "scanning"
            self.scan_candidates = []
            self.scan_reason = None
            self.scan_current = self.scan_total = 0
            self._scan_task = asyncio.create_task(self._scan(host), name="playback-scan")
        return self.status

    async def cancel_scan(self) -> None:
        await self._cancel_scan()
        if self.scan_state == "scanning":
            self.scan_state = "idle"
            self.scan_reason = "Scan cancelled."

    async def _cancel_scan(self) -> None:
        task = self._scan_task
        self._scan_cancel.set()
        if task is not None and task is not asyncio.current_task():
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._scan_task = None

    async def _adopt(self, host: str | None) -> None:
        """Select Playback, turn it on, and remember the address (one save)."""
        saved = self._service.snapshot()
        playback = saved.playback_api.model_copy(update={"enabled": True, "host": host})
        modes = saved.integration_modes.model_copy(update={"midi_source": MidiSource.PLAYBACK_API})
        # The caller holds the lifecycle lock, so use the lock-free internals.
        self._service.save(
            saved.model_copy(
                update={"playback_api": playback, "integration_modes": modes}, deep=True
            )
        )
        await self._reconfigure(self._service.effective_runtime_settings())

    async def _scan(self, host: str | None) -> None:
        cancel = self._scan_cancel
        config = self.settings.playback_api

        def progress(current: int, total: int) -> None:
            self.scan_current, self.scan_total = current, total

        try:
            found = await asyncio.to_thread(
                self._scanner,
                host_override=host,
                port=config.port,
                auto_scan=True,
                cancel=cancel,
                progress=progress,
            )
            if cancel.is_set():
                return
            self.scan_candidates = list(found)
            if not found:
                self.scan_state = "not_found"
                self.scan_reason = NOT_FOUND_REASON
                return
            if len(found) == 1:
                async with self._lifecycle:
                    if cancel.is_set() or self.discovery == "running":
                        return
                    await self._adopt(found[0].host)
            self.scan_state = "found"
        except SettingsFileError:
            self.scan_state = "not_found"
            self.scan_reason = "Found Playback but could not save the address."
        except Exception:
            self.scan_state = "not_found"
            self.scan_reason = NOT_FOUND_REASON

    def _check_discovery(self, version: int | None = None) -> Heartbeat:
        heartbeat = self._heartbeat
        if (
            not self._active
            or not self.arbiter.snapshot().sources["playback_api"].connected
            or heartbeat is None
        ):
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
