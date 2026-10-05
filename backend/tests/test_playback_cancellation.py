from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from stagepilot.core.state import StateStore
from stagepilot.plugins.playback_api.arbiter import ArbitratedDispatcher, PlaybackSourceArbiter
from stagepilot.plugins.playback_api.client import ConnectionOptions, PlaybackClient
from stagepilot.plugins.playback_api.mapping import PlaybackMapper
from stagepilot.plugins.playback_api.normalizer import Heartbeat, PlaybackEvent
from test_playback_integration import FakePlayback, application, controller, wait_for


@pytest.mark.parametrize("cancel", [False, True])
async def test_queued_navigation_rechecks_playing_and_joins_cancelled_send(
    cancel: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePlayback()
    async with application(fake) as (app, _):
        inputs = controller(app)
        assert inputs.client is not None and inputs.client._socket is not None
        ws = inputs.client._socket
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        original = ws._discovery_step

        def blocked(direction: Literal["previous", "next"], allowed: Callable[[], bool]) -> None:
            loop.call_soon_threadsafe(entered.set)
            original(direction, allowed)

        monkeypatch.setattr(ws, "_discovery_step", blocked)
        ws._write_lock.acquire()
        operation = asyncio.create_task(inputs.discover_song_order())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if cancel:
                operation.cancel()
                await asyncio.sleep(0)
                assert not operation.done()
                assert inputs.discovery == "running" and inputs.mapper.discovery
            fake.playing = True
            await wait_for(
                lambda: bool(inputs.status.heartbeat and inputs.status.heartbeat.playing)
            )
        finally:
            ws._write_lock.release()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            await operation
        assert inputs.discovery == "failed" and not inputs.mapper.discovery
        assert not fake.commands


async def test_cancelled_shutdown_joins_finder_and_allows_restart() -> None:
    entered = asyncio.Event()
    released = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def finder(_options: ConnectionOptions) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(2)
        finished.set()
        return None

    client = PlaybackClient(finder=finder, reconnect_delays=(15,))
    await client.start()
    await asyncio.wait_for(entered.wait(), 1)
    stopping = asyncio.create_task(client.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done() and not finished.is_set()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert finished.is_set() and client._task is None
    await client.start()
    assert client._task is not None and not client._task.cancelled()
    await asyncio.wait_for(client.stop(), 1)
    assert client._task is None


@pytest.mark.parametrize("invalid", ["version", "unknown", "discovery"])
async def test_arbitrated_restart_rechecks_mapping_after_second_snapshot(invalid: str) -> None:
    arbiter = PlaybackSourceArbiter()
    arbiter.observe_api(True, True)
    dispatcher = AsyncMock()
    store = AsyncMock(spec=StateStore)
    proxy = ArbitratedDispatcher(arbiter, dispatcher, store)
    mapper = PlaybackMapper(proxy, store)
    mapper.install_order((900, 17, 42), 8)
    proxy.api_mapping_revision = lambda: mapper.revision
    snapshots = 0

    async def snapshot() -> AsyncMock:
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            if invalid == "discovery":
                mapper.set_discovery(True)
                mapper.set_discovery(False)
            else:
                mapper.observe(
                    Heartbeat(
                        999 if invalid == "unknown" else 17,
                        0,
                        True,
                        False,
                        9 if invalid == "version" else 8,
                    ),
                    (),
                )
        return AsyncMock(current_song=object(), current_song_index=1)

    store.snapshot.side_effect = snapshot
    observations = mapper.observe(
        Heartbeat(17, 0, True, False, 8), (PlaybackEvent("song.started", 1, 17),)
    )
    await mapper.dispatch(observations[0])
    assert snapshots == 2
    dispatcher.dispatch.assert_not_awaited()
    dispatcher.dispatch_song_position.assert_not_awaited()
