from typing import get_args
from unittest.mock import AsyncMock

import pytest

from stagepilot.core.events import ActionName
from stagepilot.core.state import StateStore
from stagepilot.models.state import ApplicationState
from stagepilot.plugins.playback_api.mapping import PlaybackMapper
from stagepilot.plugins.playback_api.normalizer import EventType, Heartbeat, PlaybackEvent


def setup_mapper() -> tuple[PlaybackMapper, AsyncMock, StateStore]:
    dispatcher = AsyncMock()
    store = StateStore(ApplicationState())
    mapper = PlaybackMapper(dispatcher, store)
    mapper.install_order((900, 17, 42), 8)
    return mapper, dispatcher, store


async def send(mapper: PlaybackMapper, kind: EventType, song: int = 17, version: int = 8) -> None:
    events = mapper.observe(
        Heartbeat(song, 0, True, False, version), (PlaybackEvent(kind, 1, song),)
    )
    for event in events:
        await mapper.dispatch(event)


async def test_known_id_is_index_not_opaque_number() -> None:
    mapper, dispatcher, _ = setup_mapper()
    await send(mapper, "song.started")
    dispatcher.dispatch_song_position.assert_awaited_once_with(2, source="playback_api")
    dispatcher.dispatch.assert_not_awaited()


@pytest.mark.parametrize("kind", ["song.paused", "song.stopped"])
async def test_stops_even_when_stale(kind: EventType) -> None:
    mapper, dispatcher, _ = setup_mapper()
    await send(mapper, kind, song=999, version=9)
    assert mapper.stale
    dispatcher.dispatch.assert_awaited_once_with(ActionName.STOP_TIMER, source="playback_api")
    dispatcher.dispatch_song_position.assert_not_awaited()


@pytest.mark.parametrize(
    "kind",
    [
        kind
        for kind in get_args(EventType)
        if kind not in {"song.started", "song.paused", "song.stopped"}
    ],
)
async def test_other_events_are_monitor_only(kind: EventType) -> None:
    mapper, dispatcher, _ = setup_mapper()
    await send(mapper, kind)
    dispatcher.dispatch.assert_not_awaited()
    dispatcher.dispatch_song_position.assert_not_awaited()


@pytest.mark.parametrize("song,version", [(999, 8), (17, 9)])
async def test_invalid_batch_is_stale_before_start_and_never_silently_repairs(
    song: int, version: int
) -> None:
    mapper, dispatcher, _ = setup_mapper()
    await send(mapper, "song.started", song, version)
    await send(mapper, "song.started")
    assert mapper.stale
    dispatcher.dispatch_song_position.assert_not_awaited()
    mapper.install_order((900, 17, 42), 8)
    await send(mapper, "song.started")
    assert not mapper.stale
    dispatcher.dispatch_song_position.assert_awaited_once()


async def test_natural_transition_dispatches_only_following_start() -> None:
    mapper, dispatcher, _ = setup_mapper()
    batch = mapper.observe(
        Heartbeat(17, 0, True, False, 8),
        (
            PlaybackEvent("song.ended", 1, 900),
            PlaybackEvent("song.changed", 1, 17, previous_song_id=900, continues_playing=True),
            PlaybackEvent("song.started", 1, 17),
        ),
    )
    for event in batch:
        await mapper.dispatch(event)
    dispatcher.dispatch_song_position.assert_awaited_once_with(2, source="playback_api")
    dispatcher.dispatch.assert_not_awaited()


async def test_reconnect_snapshot_revalidates_without_action() -> None:
    mapper, dispatcher, _ = setup_mapper()
    await send(mapper, "state.snapshot")
    assert not mapper.stale
    mapper.observe(None, ())  # Disconnect is not invalidation.
    await send(mapper, "state.snapshot")
    assert not mapper.stale
    dispatcher.dispatch_song_position.assert_not_awaited()


@pytest.mark.parametrize("kind", ["song.started", "song.paused", "song.stopped"])
async def test_discovery_queue_cannot_escape_suppression(kind: EventType) -> None:
    mapper, dispatcher, _ = setup_mapper()
    pending = mapper.observe(Heartbeat(17, 0, True, False, 8), (PlaybackEvent(kind, 1, 17),))
    mapper.set_discovery(True)
    during = mapper.observe(Heartbeat(17, 0, True, False, 8), (PlaybackEvent(kind, 2, 17),))
    assert during[0].discovery
    mapper.set_discovery(False)
    for event in pending + during:
        await mapper.dispatch(event)
    dispatcher.dispatch.assert_not_awaited()
    dispatcher.dispatch_song_position.assert_not_awaited()


@pytest.mark.parametrize("order", [(), (17, 17), (True, 17)])
def test_invalid_order_rejected(order: tuple[int, ...]) -> None:
    mapper, _, _ = setup_mapper()
    with pytest.raises(ValueError):
        mapper.install_order(order, 8)


async def test_active_song_restarts() -> None:
    mapper, dispatcher, _ = setup_mapper()
    # State shape is supplied by the application, not inferred from timer status.
    mapper._state_store = AsyncMock()
    mapper._state_store.snapshot.return_value = AsyncMock(
        current_song=object(), current_song_index=1
    )
    await send(mapper, "song.started")
    dispatcher.dispatch.assert_awaited_once_with(ActionName.RESTART_CURRENT, source="playback_api")
    dispatcher.dispatch_song_position.assert_not_awaited()


async def test_install_at_discovery_end_validates_next_heartbeat() -> None:
    mapper, dispatcher, _ = setup_mapper()
    mapper.set_discovery(True)
    mapper.install_order((900, 17, 42), 8)
    mapper.set_discovery(False)
    await send(mapper, "song.started")
    assert not mapper.stale
    dispatcher.dispatch_song_position.assert_awaited_once_with(2, source="playback_api")


async def test_missing_version_and_unknown_observation_revoke_trust() -> None:
    mapper, dispatcher, _ = setup_mapper()
    mapper.observe(Heartbeat(17, 0, True, False, None), ())
    await send(mapper, "song.started")
    assert mapper.stale
    mapper.install_order((900, 17, 42), 8)
    mapper.observe(None, (PlaybackEvent("song.selected", 1, 999),))
    await send(mapper, "song.started")
    assert mapper.stale
    dispatcher.dispatch_song_position.assert_not_awaited()


async def test_start_rechecks_after_async_snapshot() -> None:
    mapper, dispatcher, _ = setup_mapper()
    store = AsyncMock()
    mapper._state_store = store

    async def snapshot() -> ApplicationState:
        mapper.set_discovery(True)
        mapper.set_discovery(False)
        return ApplicationState()

    store.snapshot.side_effect = snapshot
    await send(mapper, "song.started")
    dispatcher.dispatch_song_position.assert_not_awaited()
