"""Async lifecycle over blocking stdlib discovery/receive, with capped reconnect."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from stagepilot.core.logging import get_logger

from .find import (
    SCAN_BLOCK_DETAIL,
    DiscoveryResult,
    DiscoverySource,
    classify_exception,
    find_playback,
    saved_address_blocked,
)
from .normalizer import Heartbeat, Normalizer, PlaybackEvent
from .ws import WebSocket, WSClosed

_LOG = get_logger("playback")


@dataclass(frozen=True)
class ConnectionOptions:
    enabled: bool = True
    host_override: str | None = None
    port: int = 8080
    auto_scan: bool = True
    fast_transport: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("invalid Playback port")
        if self.host_override is not None and (
            not self.host_override or any(c in self.host_override for c in "\r\n /?#")
        ):
            raise ValueError("invalid Playback host")


@dataclass(frozen=True)
class PlaybackStatus:
    connected: bool = False
    host: str | None = None
    port: int = 8080
    source: DiscoverySource | None = None
    last_error: str | None = None
    heartbeat: Heartbeat | None = None
    setlist_id: int | str | None = None


Observer = Callable[[PlaybackStatus, tuple[PlaybackEvent, ...]], None]
Finder = Callable[[ConnectionOptions], DiscoveryResult | None]
SocketFactory = Callable[[str, int, float], WebSocket]


async def _join_worker(
    task: asyncio.Task[None], on_cancel: Callable[[], None] = lambda: None
) -> None:
    """Cancellation cannot abandon a blocking worker; report it after joining."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            on_cancel()
        except Exception:
            break
    if cancelled:
        with suppress(Exception):
            task.result()
        raise asyncio.CancelledError
    task.result()


def _find(options: ConnectionOptions) -> DiscoveryResult | None:
    return find_playback(
        host_override=options.host_override, port=options.port, auto_scan=options.auto_scan
    )


class PlaybackClient:
    """Observer executes on the event loop, never the receive worker.

    Status is updated before delivering each complete heartbeat batch, so mapping
    validation can precede every event in that batch. Reconnect starts a fresh
    normalizer (snapshot only). This layer never dispatches StagePilot actions.
    """

    def __init__(
        self,
        options: ConnectionOptions | None = None,
        *,
        observer: Observer | None = None,
        finder: Finder = _find,
        socket_factory: SocketFactory = WebSocket,
        reconnect_delays: tuple[float, ...] = (1, 2, 4, 8, 15),
        heartbeat_timeout: float = 5.0,
        history_capacity: int = 100,
    ) -> None:
        if (
            not reconnect_delays
            or any(delay <= 0 for delay in reconnect_delays)
            or heartbeat_timeout <= 0
            or history_capacity < 1
        ):
            raise ValueError("Playback timeouts and capacities must be positive")
        self.options = options or ConnectionOptions()
        self._observer = observer
        self._finder = finder
        self._socket_factory = socket_factory
        self._delays = reconnect_delays
        self._heartbeat_timeout = heartbeat_timeout
        self._history: deque[PlaybackEvent] = deque(maxlen=history_capacity)
        self._status = PlaybackStatus(port=self.options.port)
        self._socket: WebSocket | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()

    @property
    def status(self) -> PlaybackStatus:
        return self._status

    @property
    def events(self) -> tuple[PlaybackEvent, ...]:
        return tuple(self._history)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._task is None and self.options.enabled:
                self._stopping.clear()
                self._task = asyncio.create_task(self._run(), name="playback-receive")

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stopping.set()
            if self._socket:
                self._socket.close()
            # Do not cancel to_thread work: wait for its bounded network timeout
            # so shutdown/reconfigure cannot leave an orphan receive/discovery.
            try:
                if self._task:
                    await _join_worker(self._task)
            finally:
                self._task = None
                self._status = PlaybackStatus(port=self.options.port)

    async def reconfigure(self, options: ConnectionOptions) -> None:
        await self.stop()
        self.options = options
        await self.start()

    async def find(self) -> DiscoveryResult | None:
        return await asyncio.to_thread(self._finder, self.options)

    def _publish(self, status: PlaybackStatus, events: tuple[PlaybackEvent, ...] = ()) -> None:
        self._status = status
        self._history.extend(events)
        if self._observer:
            # Input observers must not kill reconnect; integration owns logging.
            with suppress(Exception):
                self._observer(status, events)

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping.is_set():
            endpoint: DiscoveryResult | None = None
            valid_heartbeat = False
            try:
                endpoint = await self.find()
                if self._stopping.is_set():
                    break
                if endpoint is None:
                    raise WSClosed("Playback not found; remote connections may be off")
                ws = await asyncio.to_thread(
                    self._socket_factory, endpoint.host, endpoint.port, self._heartbeat_timeout
                )
                self._socket = ws
                norm = Normalizer(fast_transport=self.options.fast_transport)
                last_heartbeat = time.monotonic()
                while not self._stopping.is_set():
                    remaining = self._heartbeat_timeout - (time.monotonic() - last_heartbeat)
                    if remaining <= 0:
                        raise WSClosed("Playback heartbeat timed out")
                    raw = await asyncio.to_thread(ws.recv, remaining)
                    if raw is None:
                        raise WSClosed("Playback heartbeat timed out")
                    previous = norm.heartbeat
                    events = norm.feed(raw, time.monotonic())
                    if norm.heartbeat is not previous:
                        last_heartbeat = time.monotonic()
                        valid_heartbeat = True
                    if self._stopping.is_set():
                        break
                    self._publish(
                        PlaybackStatus(
                            valid_heartbeat,
                            endpoint.host,
                            endpoint.port,
                            endpoint.source,
                            heartbeat=norm.heartbeat,
                            setlist_id=norm.setlist_id,
                        ),
                        events,
                    )
            except (OSError, ValueError) as exc:
                if not self._stopping.is_set():
                    error_class, _detail, step = classify_exception(exc)
                    _LOG.info(
                        "playback_connect_failed",
                        error_class=error_class,
                        error_type=type(exc).__name__,
                        step=step,
                        attempt=attempt + 1,
                        source=endpoint.source if endpoint else None,
                        configured_host=bool(self.options.host_override),
                    )
                    blocked = False
                    if error_class in ("permission_denied", "no_route"):
                        blocked = await asyncio.to_thread(saved_address_blocked)
                    self._publish(
                        PlaybackStatus(
                            host=endpoint.host if endpoint else self.options.host_override,
                            port=self.options.port,
                            source=endpoint.source if endpoint else None,
                            last_error=SCAN_BLOCK_DETAIL
                            if blocked
                            else "Playback unavailable; remote connections may be off",
                        )
                    )
            finally:
                if self._socket:
                    self._socket.close()
                    self._socket = None
            if self._stopping.is_set():
                break
            if valid_heartbeat:
                attempt = 0
            delay = self._delays[min(attempt, len(self._delays) - 1)]
            attempt += 1
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)

    async def _discovery_step(self, direction: Literal["previous", "next"]) -> None:
        """Private integration hook; stopped-state check repeated before every send.

        Confirmation, exclusive operation, suppression and restoration are owned
        by the integration's discover_song_order(), not exposed as client controls.
        """
        heartbeat = self._status.heartbeat
        if not self._status.connected or self._socket is None:
            raise WSClosed("Playback is disconnected")
        if heartbeat is None or heartbeat.playing:
            raise ValueError("Stop Playback before discovering song order")
        socket = self._socket
        cancelled = threading.Event()

        def allowed() -> bool:
            status = self._status
            return (
                not cancelled.is_set()
                and not self._stopping.is_set()
                and self._socket is socket
                and status.connected
                and status.heartbeat is not None
                and not status.heartbeat.playing
            )

        worker = asyncio.create_task(asyncio.to_thread(socket._discovery_step, direction, allowed))
        await _join_worker(worker, cancelled.set)
