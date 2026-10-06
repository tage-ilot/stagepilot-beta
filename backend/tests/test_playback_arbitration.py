from dataclasses import dataclass, replace
from unittest.mock import AsyncMock

import pytest

from stagepilot.core.actions import ActionOutcome
from stagepilot.core.events import ActionName
from stagepilot.core.state import StateStore
from stagepilot.plugins.midi_playback.models import MidiMessage
from stagepilot.plugins.playback_api.arbiter import ArbitratedDispatcher, PlaybackSourceArbiter
from stagepilot.plugins.playback_api.normalizer import Heartbeat, PlaybackEvent
from test_midi_config_api import FakeMidiBackend
from test_playback_integration import FakePlayback, application, controller, wait_for


@dataclass
class Clock:
    now: float = 0

    def __call__(self) -> float:
        return self.now


def stable_api(arbiter: PlaybackSourceArbiter, clock: Clock) -> None:
    for _ in range(4):
        arbiter.observe_api(True, True)
        clock.now += 1


@pytest.mark.parametrize(
    "api,midi,owner",
    [
        (False, False, "none"),
        (True, False, "playback_api"),
        (False, True, "midi"),
        (True, True, "playback_api"),
    ],
)
def test_single_owner_and_connection(api: bool, midi: bool, owner: str) -> None:
    clock = Clock()
    arbiter = PlaybackSourceArbiter(clock=clock)
    arbiter.midi_connected = midi
    if api:
        stable_api(arbiter, clock)
    result = arbiter.snapshot()
    assert result.active_source == owner
    assert result.connected == (api or midi)
    assert result.sources["playback_api"].connected is api
    assert result.sources["midi"].connected is midi


def test_immediate_failover_stable_failback_and_flapping() -> None:
    clock = Clock()
    arbiter = PlaybackSourceArbiter(clock=clock)
    arbiter.midi_connected = True
    stable_api(arbiter, clock)
    assert arbiter.allows("playback_api")
    arbiter.observe_api(False, False)
    assert arbiter.allows("midi")
    arbiter.observe_api(True, True)
    assert arbiter.allows("midi")
    clock.now += 1
    arbiter.observe_api(False, False)
    arbiter.observe_api(True, True)
    assert arbiter.allows("midi")
    stable_api(arbiter, clock)
    assert arbiter.allows("playback_api")
    clock.now += 5
    assert arbiter.allows("midi")
    assert not arbiter.snapshot().sources["playback_api"].connected


def test_single_heartbeat_is_not_stable_failback_and_midi_loss_does_not_wait() -> None:
    clock = Clock()
    arbiter = PlaybackSourceArbiter(clock=clock)
    arbiter.midi_connected = True
    arbiter.observe_api(True, True)
    clock.now = 3
    assert arbiter.allows("midi")
    arbiter.midi_connected = False
    assert arbiter.allows("playback_api")


@pytest.mark.parametrize("action", ["position", "restart", "stop", "start_next"])
async def test_cross_source_dedupe_and_expiry(action: str) -> None:
    clock = Clock()
    arbiter = PlaybackSourceArbiter(clock=clock)
    arbiter.observe_api(True, True)
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = ActionOutcome(True, "accepted")
    dispatcher.dispatch_song_position.return_value = ActionOutcome(True, "accepted")
    proxy = ArbitratedDispatcher(arbiter, dispatcher, StateStore())
    if action in ("position", "start_next"):
        assert (await proxy.dispatch_song_position(1, source="playback_api")).accepted
    else:
        name = ActionName.STOP_TIMER if action == "stop" else ActionName.RESTART_CURRENT
        assert (await proxy.dispatch(name, source="playback_api")).accepted
    arbiter.midi_connected = True
    arbiter.observe_api(False, False)
    if action == "position":
        result = await proxy.dispatch_song_position(1, source="midi_playback")
    else:
        name = {
            "restart": ActionName.RESTART_CURRENT,
            "stop": ActionName.STOP_TIMER,
            "start_next": ActionName.START_NEXT,
        }[action]
        result = await proxy.dispatch(name, source="midi_playback")
    assert not result.accepted and "duplicate cross-source" in result.message
    clock.now += 2
    assert (await proxy.dispatch_song_position(1, source="midi_playback")).accepted


async def test_rejected_action_does_not_poison_failover() -> None:
    clock = Clock()
    arbiter = PlaybackSourceArbiter(clock=clock)
    arbiter.observe_api(True, True)
    dispatcher = AsyncMock()
    dispatcher.dispatch_song_position.return_value = ActionOutcome(False, "no plan")
    proxy = ArbitratedDispatcher(arbiter, dispatcher, StateStore())
    await proxy.dispatch_song_position(1, source="playback_api")
    arbiter.midi_connected = True
    arbiter.observe_api(False, False)
    dispatcher.dispatch_song_position.return_value = ActionOutcome(True, "accepted")
    assert (await proxy.dispatch_song_position(1, source="midi_playback")).accepted


async def test_both_sources_invalid_order_keeps_api_owner_stops_and_monitor() -> None:
    fake = FakePlayback(index=0)
    midi = FakeMidiBackend(["Playback MIDI"])
    async with application(fake, midi=midi) as (app, client):
        inputs = controller(app)
        await wait_for(lambda: inputs.arbiter.midi_connected)
        # Deterministic elapsed stable-heartbeat time; no real-device or long wait.
        assert inputs.arbiter.stable_since is not None
        inputs.arbiter.stable_since -= 3
        await wait_for(lambda: inputs.connection.active_source == "playback_api")
        dispatcher = AsyncMock()
        dispatcher.dispatch.return_value = ActionOutcome(True, "accepted")
        dispatcher.dispatch_song_position.return_value = ActionOutcome(True, "accepted")
        inputs._dispatcher.dispatcher = dispatcher
        assert inputs.mapper.stale
        inputs._observe(
            inputs._generation,
            inputs.status,
            (PlaybackEvent("song.started", 1, 900), PlaybackEvent("song.paused", 1, 900)),
        )
        await wait_for(lambda: dispatcher.dispatch.await_count == 1)
        dispatcher.dispatch.assert_awaited_once_with(ActionName.STOP_TIMER, source="playback_api")
        dispatcher.dispatch_song_position.assert_not_awaited()
        midi.ports[-1].callback(MidiMessage(type="note_on", channel=1, note=112, velocity=1))
        await wait_for(lambda: bool(inputs.midi and inputs.midi._recent_messages))
        messages = (await client.get("/api/v1/midi/messages")).json()["messages"]
        assert messages[0]["detail"] == "ignored: Playback API active"
        status = (await client.get("/api/v1/playback-api/status")).json()
        state = (await client.get("/api/v1/state")).json()
        assert status["connected"] and status["active_source"] == "playback_api"
        assert status["stale"] and status["reason"].startswith("Connected to Playback")
        assert state["midi_status"] == "connected"
        assert (await client.get("/api/v1/playback-api/connection")).json()[
            "active_source"
        ] == "playback_api"
        dispatcher.dispatch_song_position.assert_not_awaited()


async def test_api_drop_midi_starts_despite_stale_api_order_and_no_global_error() -> None:
    fake = FakePlayback(index=0)
    midi = FakeMidiBackend(["Playback MIDI"])
    async with application(fake, midi=midi) as (app, client):
        inputs = controller(app)
        await wait_for(lambda: inputs.arbiter.midi_connected)
        assert inputs.mapper.stale
        assert inputs.client is not None
        await inputs.client.stop()
        await wait_for(lambda: inputs.connection.active_source == "midi")
        midi.ports[-1].callback(MidiMessage(type="note_on", channel=1, note=112, velocity=1))
        await wait_for(lambda: bool(inputs.midi and inputs.midi._recent_messages))
        status = (await client.get("/api/v1/playback-api/status")).json()
        state = (await client.get("/api/v1/state")).json()
        assert status["connected"] and status["active_source"] == "midi"
        assert status["last_error"] is None and state["midi_status"] == "connected"
        assert state["current_song_index"] == 0
        assert (await inputs.health()).last_error is None


async def test_api_events_received_while_midi_owns_are_not_replayed_after_failback() -> None:
    fake = FakePlayback(index=0)
    midi = FakeMidiBackend(["Playback MIDI"])
    async with application(fake, saved=True, midi=midi) as (app, _):
        inputs = controller(app)
        await wait_for(lambda: inputs.arbiter.midi_connected)
        fake.silent = True
        inputs.arbiter.observe_api(False, False)
        status = replace(inputs.status, heartbeat=Heartbeat(900, 0, True, False, 8))
        inputs._observe(inputs._generation, status, (PlaybackEvent("song.started", 1, 900),))
        assert inputs.connection.active_source == "midi"
        assert inputs.arbiter.stable_since is not None
        inputs.arbiter.stable_since -= 3
        await inputs._publish_connection()
        assert inputs.arbiter.snapshot().active_source == "playback_api"
        assert (await app.state.runtime.state_store.snapshot()).current_song_index is None
        assert any(item.event.type == "song.started" for item in inputs.events)


async def test_health_uses_unified_connection_and_find_keeps_midi_open() -> None:
    fake = FakePlayback(index=0)
    midi = FakeMidiBackend(["Playback MIDI"])
    async with application(fake, midi=midi) as (app, client):
        inputs = controller(app)
        await wait_for(lambda: inputs.arbiter.midi_connected)
        assert (await client.get("/api/v1/health")).json()["status"] == "healthy"
        port = midi.ports[-1]
        assert (await client.post("/api/v1/playback-api/find")).status_code == 200
        await wait_for(lambda: inputs.scan_state != "scanning")
        assert not port.closed and inputs.arbiter.midi_connected
        assert (await client.get("/api/v1/health")).json()["status"] == "healthy"
        assert inputs.client is not None
        await inputs._stop_selected(keep_midi=True)
        assert (await client.get("/api/v1/health")).json()["status"] == "healthy"
        assert inputs.midi is not None
        await inputs.midi.stop()
        assert (await client.get("/api/v1/health")).json()["status"] == "degraded"
        status = (await client.get("/api/v1/playback-api/status")).json()
        assert not status["connected"] and status["active_source"] == "none"
        assert (await client.get("/api/v1/state")).json()["midi_status"] == "disconnected"


async def test_late_midi_queue_is_discarded_even_after_owner_changes_back() -> None:
    from stagepilot.plugins.midi_playback.plugin import _QueuedMidiMessage

    fake = FakePlayback(index=0)
    midi = FakeMidiBackend(["Playback MIDI"])
    async with application(fake, midi=midi) as (app, _):
        inputs = controller(app)
        await wait_for(lambda: inputs.arbiter.midi_connected)
        inputs.arbiter.observe_api(False, False)
        assert inputs.midi is not None
        queued = _QueuedMidiMessage(
            message=MidiMessage(type="note_on", channel=1, note=112, velocity=1),
            connection_id=inputs.midi._active_connection_id,
            simulated=False,
            ownership_revision=inputs._revision(),
        )
        inputs.arbiter.observe_api(True, True)
        assert inputs.arbiter.stable_since is not None
        inputs.arbiter.stable_since -= 3
        assert inputs.arbiter.allows("playback_api")
        inputs.arbiter.observe_api(False, False)
        result = await inputs.midi._process_message(queued)
        assert not result.accepted and "ownership changed" in result.message
        assert (await app.state.runtime.state_store.snapshot()).current_song_index is None
        inputs.midi._held_notes.add((1, 112, 1))
        queued.message = MidiMessage(type="note_off", channel=1, note=112, velocity=0)
        release = await inputs.midi._process_message(queued)
        assert not release.accepted and "release" in release.message
        assert not inputs.midi._held_notes
