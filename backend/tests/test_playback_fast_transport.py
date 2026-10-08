from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from stagepilot.core.actions import ActionOutcome
from stagepilot.core.events import ActionName
from stagepilot.core.state import StateStore
from stagepilot.plugins.playback_api.arbiter import ArbitratedDispatcher, PlaybackSourceArbiter
from stagepilot.plugins.playback_api.mapping import PlaybackMapper
from stagepilot.plugins.playback_api.normalizer import Normalizer, PlaybackEvent
from test_playback_api_transport import heartbeat


def command(kind: str, **body: object) -> str:
    return json.dumps({kind: body})


def prepared(*, fast: bool = True, playing: bool = False, position: float = 0) -> Normalizer:
    norm = Normalizer(fast_transport=fast)
    norm.feed(heartbeat(position=position, playing=playing), 0)
    return norm


def types(events: tuple[PlaybackEvent, ...]) -> list[str]:
    return [event.type for event in events]


@pytest.mark.parametrize("fast", [False, True])
async def test_select_while_playing_stops_old_song_timer_once(fast: bool) -> None:
    norm = prepared(fast=fast, playing=True, position=10)
    dispatcher = AsyncMock()
    mapper = PlaybackMapper(dispatcher, StateStore())
    mapper.install_order((101, 202), 1)
    mapper.observe(norm.heartbeat, ())
    selected = norm.feed(command("setlistSelectSong", setlistSongID=202), 1)
    assert types(selected) == (["song.selected", "song.stopped"] if fast else ["song.selected"])
    batches = [(norm.heartbeat, selected)]
    confirmed = norm.feed(heartbeat(song=202), 2)
    batches.append((norm.heartbeat, confirmed))
    stopped = [event for _, batch in batches for event in batch if event.type == "song.stopped"]
    assert len(stopped) == 1
    assert stopped[0].song_id == 101 and stopped[0].reason == "song-selected"
    assert stopped[0].provisional is fast
    for hb, events in batches:
        for observation in mapper.observe(hb, events):
            await mapper.dispatch(observation)
    dispatcher.dispatch.assert_awaited_once_with(ActionName.STOP_TIMER, source="playback_api")
    dispatcher.dispatch_song_position.assert_not_awaited()


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("continues", [False, True])
def test_natural_end_remains_auto_advance(fast: bool, continues: bool) -> None:
    norm = prepared(fast=fast, playing=True, position=20)
    events = norm.feed(heartbeat(song=202, playing=continues), 1)
    assert types(events) == ["song.ended", "song.changed"] + (["song.started"] if continues else [])
    assert events[1].reason == "auto-advance"


def test_expired_select_does_not_misclassify_natural_end() -> None:
    norm = prepared(fast=False, playing=True)
    norm.feed(command("setlistSelectSong", setlistSongID=202), 1)
    assert types(norm.feed(heartbeat(song=202), 6)) == ["song.ended", "song.changed"]


@pytest.mark.parametrize("position,kind", [(0, "song.started"), (10, "song.resumed")])
def test_immediate_play_confirmation_and_noop(position: float, kind: str) -> None:
    norm = prepared(position=position)
    events = norm.feed(command("transportPlay", playing=True), 0.1)
    assert types(events) == [kind] and events[0].provisional
    assert norm.feed(command("transportPlay", playing=True), 0.2) == ()
    assert norm.feed(heartbeat(position=position + 0.5, playing=True), 0.6) == ()
    assert norm.feed(command("transportPlay", playing=True), 0.7) == ()


def test_quick_play_pause_play_pause_emits_four_events_without_heartbeat() -> None:
    norm = prepared()
    events: list[PlaybackEvent] = []
    for timestamp, playing in [(0.1, True), (0.2, False), (0.3, True), (0.4, False)]:
        events.extend(norm.feed(command("transportPlay", playing=playing), timestamp))
    assert [event.type for event in events] == [
        "song.started",
        "song.paused",
        "song.resumed",
        "song.paused",
    ]
    assert all(event.provisional for event in events)
    assert norm.feed(heartbeat(), 1) == ()
    assert norm.feed(heartbeat(), 4) == ()


@pytest.mark.parametrize("playing", [False, True])
def test_return_immediate_confirmation_and_restart(playing: bool) -> None:
    norm = prepared(playing=playing, position=10)
    events = norm.feed(command("transportReturnToStart"), 0.1)
    assert types(events) == ["song.stopped"] and events[0].position == 0 and events[0].provisional
    assert norm.feed(heartbeat(), 1) == ()
    assert types(norm.feed(command("transportPlay", playing=True), 1.1)) == ["song.started"]


def test_return_at_stopped_start_is_noop() -> None:
    assert prepared().feed(command("transportReturnToStart"), 0.1) == ()


async def test_rejected_return_from_paused_mid_song_reverts_and_restores_armed() -> None:
    norm = prepared(position=10)
    dispatcher = AsyncMock()
    mapper = PlaybackMapper(dispatcher, StateStore())
    mapper.install_order((101,), 1)
    batches = [(norm.heartbeat, norm.feed(command("transportReturnToStart"), 0.1))]
    assert norm.feed(heartbeat(position=10), 1) == ()
    events = norm.feed(heartbeat(position=10), 3.2)
    assert types(events) == ["transport.reverted"]
    batches.append((norm.heartbeat, events))
    for hb, batch in batches:
        for observation in mapper.observe(hb, batch):
            await mapper.dispatch(observation)
    dispatcher.dispatch.assert_awaited_once_with(ActionName.STOP_TIMER, source="playback_api")
    assert types(norm.feed(command("transportPlay", playing=True), 3.3)) == ["song.resumed"]


@pytest.mark.parametrize("playing", [False, True])
async def test_delayed_successful_return_stops_timer_once(playing: bool) -> None:
    norm = prepared(playing=playing, position=10)
    dispatcher = AsyncMock()
    mapper = PlaybackMapper(dispatcher, StateStore())
    mapper.install_order((101,), 1)
    events = norm.feed(command("transportReturnToStart"), 0.1)
    for observation in mapper.observe(norm.heartbeat, events):
        await mapper.dispatch(observation)
    assert norm.feed(heartbeat(position=11 if playing else 10, playing=playing), 1) == ()
    assert norm.feed(heartbeat(), 3.2) == ()
    assert norm.feed(heartbeat(), 4) == ()
    dispatcher.dispatch.assert_awaited_once_with(ActionName.STOP_TIMER, source="playback_api")
    assert types(norm.feed(command("transportPlay", playing=True), 4.1)) == ["song.started"]


def test_return_then_play_before_heartbeat_uses_reset_position() -> None:
    norm = prepared(playing=True, position=10)
    norm.feed(command("transportReturnToStart"), 0.1)
    assert norm.feed(command("transportReturnToStart"), 0.2) == ()
    events = norm.feed(command("transportPlay", playing=True), 0.3)
    assert types(events) == ["song.started"] and events[0].position == 0
    assert types(norm.feed(heartbeat(playing=True), 1)) == ["position.jump"]
    # The old heartbeat position moved; this is monitor-only, not another start.


def test_select_then_play_before_heartbeat_starts_new_song_once() -> None:
    norm = prepared(playing=True, position=10)
    assert types(norm.feed(command("setlistSelectSong", setlistSongID=202), 0.1)) == [
        "song.selected",
        "song.stopped",
    ]
    events = norm.feed(command("transportPlay", playing=True), 0.2)
    assert types(events) == ["song.started"] and events[0].song_id == 202
    assert norm.feed(heartbeat(song=202, playing=True), 1) == ()


@pytest.mark.parametrize("raw", [command("transportPlay"), command("transportPlay", playing=1)])
def test_invalid_play_is_not_a_command(raw: str) -> None:
    assert prepared().feed(raw, 0.1) == ()


@pytest.mark.parametrize("fast", [False, True])
def test_no_provisional_transport_before_snapshot(fast: bool) -> None:
    norm = Normalizer(fast_transport=fast)
    assert norm.feed(command("transportPlay", playing=True), 0) == ()
    assert norm.feed(command("transportReturnToStart"), 0) == ()


async def test_start_confirm_dispatches_once_through_arbiter() -> None:
    norm = prepared()
    arbiter = PlaybackSourceArbiter()
    arbiter.observe_api(True, True)
    store = StateStore()
    dispatcher = AsyncMock()
    dispatcher.dispatch_song_position.return_value = ActionOutcome(True, "accepted")
    mapper = PlaybackMapper(ArbitratedDispatcher(arbiter, dispatcher, store), store)
    mapper.install_order((101, 202), 1)
    mapper.observe(norm.heartbeat, ())
    for raw, ts in [(command("transportPlay", playing=True), 0.1), (heartbeat(playing=True), 1)]:
        events = norm.feed(raw, ts)
        for observation in mapper.observe(norm.heartbeat, events):
            await mapper.dispatch(observation)
    dispatcher.dispatch_song_position.assert_awaited_once_with(1, source="playback_api")
    dispatcher.dispatch.assert_not_awaited()


async def test_unconfirmed_start_reverts_stops_timer_and_rearms() -> None:
    norm = prepared()
    dispatcher = AsyncMock()
    mapper = PlaybackMapper(dispatcher, StateStore())
    mapper.install_order((101,), 1)
    mapper.observe(norm.heartbeat, ())
    events = norm.feed(command("transportPlay", playing=True), 0.1)
    for observation in mapper.observe(norm.heartbeat, events):
        await mapper.dispatch(observation)
    assert norm.feed(heartbeat(), 1) == ()
    assert norm.feed(heartbeat(), 2) == ()
    events = norm.feed(heartbeat(), 3.1)
    assert types(events) == ["transport.reverted"]
    assert events[0].playing is False and events[0].reason == "song.started"
    for observation in mapper.observe(norm.heartbeat, events):
        await mapper.dispatch(observation)
    dispatcher.dispatch.assert_awaited_once_with(ActionName.STOP_TIMER, source="playback_api")
    assert types(norm.feed(command("transportPlay", playing=True), 3.2)) == ["song.started"]


def test_revert_requires_two_heartbeats_as_well_as_three_seconds() -> None:
    norm = prepared()
    norm.feed(command("transportPlay", playing=True), 0.1)
    assert norm.feed(heartbeat(), 4) == ()
    assert types(norm.feed(heartbeat(), 5)) == ["transport.reverted"]


async def test_unconfirmed_pause_revert_is_observed_only() -> None:
    norm = prepared(playing=True, position=10)
    norm.feed(command("transportPlay", playing=False), 0.1)
    norm.feed(heartbeat(position=11, playing=True), 1)
    events = norm.feed(heartbeat(position=13.1, playing=True), 3.1)
    assert types(events) == ["transport.reverted"]
    mapper = PlaybackMapper(AsyncMock(), StateStore())
    mapper.install_order((101,), 1)
    for observation in mapper.observe(norm.heartbeat, events):
        assert await mapper.dispatch(observation) is None


def test_fast_off_keeps_heartbeat_transport_edges() -> None:
    norm = prepared(fast=False)
    assert norm.feed(command("transportPlay", playing=True), 0.1) == ()
    assert types(norm.feed(heartbeat(playing=True), 1)) == ["song.started"]
    assert norm.feed(command("transportPlay", playing=False), 1.1) == ()
    assert types(norm.feed(heartbeat(position=1), 2)) == ["song.paused"]
    assert norm.feed(command("transportReturnToStart"), 2.1) == ()
    events = norm.feed(heartbeat(), 3)
    assert types(events) == ["song.stopped"]
    assert not events[0].provisional


@pytest.mark.parametrize(
    "kind",
    [
        "transportNextSong",
        "transportPreviousSong",
        "mixerInfiniteLoop",
        "mixerLoop",
        "mixerMuteMIDI",
        "setlistSelectSongTransition",
        "contentUpdateSetlist",
        "audioDeviceChanged",
        "mixerTrackVolume",
        "mixerTrackMute",
        "mixerTrackSolo",
        "transportNavigateToSongMapElementIndex",
    ],
)
def test_known_messages_do_not_become_unknown_or_actions(kind: str) -> None:
    assert prepared().feed(command(kind), 1) == ()


def test_truly_unknown_message_still_observed() -> None:
    events = prepared().feed(command("futureKind"), 1)
    assert types(events) == ["message.unknown"] and events[0].message_kind == "futureKind"


@pytest.mark.parametrize("identity", [7, "opaque-id"])
def test_load_tracks_identity_not_private_name(identity: int | str) -> None:
    norm = prepared()
    events = norm.feed(
        command(
            "contentLoadSetlist",
            setlistData={"setlistID": identity, "setlistName": "PRIVATE SERVICE PLAN"},
        ),
        1,
    )
    assert norm.setlist_id == identity and events == ()
    assert "PRIVATE SERVICE PLAN" not in repr(vars(norm))


def test_identity_change_stales_same_version_and_cannot_silently_repair() -> None:
    mapper = PlaybackMapper(AsyncMock(), StateStore())
    norm = prepared()
    mapper.install_order((101,), 1, 7)
    mapper.observe(norm.heartbeat, (), 7)
    assert not mapper.stale
    mapper.observe(None, (), 8)
    assert mapper.stale
    mapper.observe(norm.heartbeat, (), 7)
    assert mapper.stale
    mapper.install_order((101,), 1, 8)
    mapper.observe(norm.heartbeat, (), 8)
    assert not mapper.stale


def test_unknown_saved_identity_keeps_previous_validation() -> None:
    mapper = PlaybackMapper(AsyncMock(), StateStore())
    mapper.install_order((101,), 1)
    mapper.observe(prepared().heartbeat, (), 8)
    assert not mapper.stale
