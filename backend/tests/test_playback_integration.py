from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from stagepilot.core.config import MidiSettings, PlaybackApiSettings, Settings
from stagepilot.core.events import ActionName
from stagepilot.core.settings import (
    MemoryCredentialStore,
    SettingsFileError,
    SettingsFileStore,
    SettingsService,
)
from stagepilot.main import create_app
from stagepilot.plugins.midi_playback.models import MidiMessage
from stagepilot.plugins.playback_api.client import ConnectionOptions, Observer, PlaybackClient
from stagepilot.plugins.playback_api.find import DiscoveryResult
from stagepilot.plugins.playback_api.normalizer import Heartbeat, PlaybackEvent
from stagepilot.plugins.playback_api.plugin import PlaybackInputPlugin
from test_midi_config_api import FakeMidiBackend
from test_playback_api_transport import frame, heartbeat


@dataclass
class FakePlayback:
    songs: list[int] = field(default_factory=lambda: [900, 17, 42])
    index: int = 1
    playing: bool = False
    position: float = 0
    version: int = 8
    commands: list[str] = field(default_factory=list)
    silent: bool = False
    disconnect_on_command: bool = False
    version_on_command: bool = False
    wrap: bool = False
    port: int = 0
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    writers: set[asyncio.StreamWriter] = field(default_factory=set)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        sender: asyncio.Task[None] | None = None
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode()
            headers = dict(line.split(": ", 1) for line in request.split("\r\n") if ": " in line)
            accepted = base64.b64encode(
                hashlib.sha1(
                    (
                        headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    ).encode(),
                    usedforsecurity=False,
                ).digest()
            ).decode()
            writer.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\n" + f"Sec-WebSocket-Accept: {accepted}\r\n"
                    "Sec-WebSocket-Protocol: pr-protocol\r\n\r\n"
                ).encode()
            )
            await writer.drain()

            async def send() -> None:
                while True:
                    if not self.silent:
                        writer.write(
                            frame(
                                heartbeat(
                                    self.songs[self.index],
                                    self.position,
                                    self.playing,
                                    self.version,
                                ).encode()
                            )
                        )
                        await writer.drain()
                    await asyncio.sleep(0.01)

            sender = asyncio.create_task(send())
            while True:
                header = await reader.readexactly(2)
                size = header[1] & 127
                assert header[1] & 128 and size < 126
                mask = await reader.readexactly(4)
                payload = await reader.readexactly(size)
                payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
                if header[0] & 15 != 1:
                    continue
                command = next(iter(json.loads(payload)))
                assert command in {"transportPreviousSong", "transportNextSong"}
                self.commands.append(command)
                if self.disconnect_on_command:
                    return
                if self.version_on_command:
                    self.version += 1
                delta = -1 if command == "transportPreviousSong" else 1
                self.index = (
                    (self.index + delta) % len(self.songs)
                    if self.wrap
                    else max(0, min(len(self.songs) - 1, self.index + delta))
                )
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if sender:
                sender.cancel()
                with suppress(asyncio.CancelledError, ConnectionError):
                    await sender
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self.serve(reader, writer))
        self.tasks.add(task)

    def client(self, options: ConnectionOptions, observer: Observer) -> PlaybackClient:
        return PlaybackClient(
            options,
            observer=observer,
            finder=lambda _: DiscoveryResult(
                "127.0.0.1", self.port, "loopback", Heartbeat(900, 0, False, False, 8)
            ),
            reconnect_delays=(0.02,),
            heartbeat_timeout=0.25,
        )


async def wait_for(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        for _ in range(400):
            if predicate():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("Fake endpoint condition did not become true")


def controller(app: FastAPI) -> PlaybackInputPlugin:
    return cast(PlaybackInputPlugin, app.state.runtime.playback_input)


@asynccontextmanager
async def application(
    fake: FakePlayback,
    *,
    saved: bool = False,
    midi: FakeMidiBackend | None = None,
    settings_service: SettingsService | None = None,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    server = await asyncio.start_server(fake.accept, "127.0.0.1", 0)
    fake.port = server.sockets[0].getsockname()[1]
    settings = Settings(
        playback_api=PlaybackApiSettings(
            song_order=fake.songs if saved else [], captured_version=8 if saved else None
        ),
        midi=MidiSettings(enabled=True, input_name="Playback MIDI"),
    )
    app = create_app(
        settings_service.load() if settings_service else settings,
        settings_service=settings_service,
        playback_client_factory=fake.client,
        midi_backend_factory=(lambda: midi) if midi else None,
    )
    inputs = controller(app)
    inputs._step_timeout = 0.08
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client,
        ):
            await wait_for(lambda: inputs.status.connected)
            yield app, client
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(fake.writers):
            writer.close()
        for task in fake.tasks:
            task.cancel()
        await asyncio.gather(*fake.tasks, return_exceptions=True)


@pytest.mark.parametrize("initial", [0, 1, 2])
async def test_loopback_discovery_ends_restore_persist_and_suppress(initial: int) -> None:
    fake = FakePlayback(index=initial)
    async with application(fake) as (app, client):
        inputs = controller(app)
        dispatcher = AsyncMock()
        inputs.mapper._dispatcher = dispatcher
        operation = asyncio.create_task(
            client.post("/api/v1/playback-api/discover-song-order", json={"confirm": True})
        )
        await wait_for(lambda: inputs.discovery == "running")
        inputs._observe(
            inputs._generation,
            inputs.status,
            (
                PlaybackEvent("song.started", 1, 900),
                PlaybackEvent("song.paused", 1, 900),
                PlaybackEvent("song.stopped", 1, 900),
            ),
        )
        result = await operation
        assert result.status_code == 200
        data = result.json()
        assert data["discovery"] == "done"
        assert data["song_order"] == [900, 17, 42]
        assert data["captured_version"] == 8 and data["captured_at"]
        assert not data["stale"] and fake.index == initial
        await asyncio.sleep(0.03)
        dispatcher.dispatch.assert_not_awaited()
        dispatcher.dispatch_song_position.assert_not_awaited()
        events = (await client.get("/api/v1/playback-api/events")).json()["events"]
        assert any(item["discovery"] for item in events)
        persisted = app.state.runtime.settings_service.snapshot().playback_api
        assert persisted.song_order == fake.songs and persisted.captured_version == 8
        assert set(fake.commands) == {"transportPreviousSong", "transportNextSong"}


@pytest.mark.parametrize(
    "payload", [None, {}, {"confirm": False}, {"confirm": 1}, {"confirm": "true"}]
)
async def test_confirmation_is_server_enforced(payload: object) -> None:
    fake = FakePlayback()
    async with application(fake) as (_, client):
        assert (
            await client.post("/api/v1/playback-api/discover-song-order", json=payload)
        ).status_code == 400
        assert not fake.commands


async def test_conflicts_playing_disconnected_running_and_settings() -> None:
    fake = FakePlayback(playing=True)
    async with application(fake) as (app, client):
        inputs = controller(app)
        response = await client.post(
            "/api/v1/playback-api/discover-song-order", json={"confirm": True}
        )
        assert response.status_code == 409 and "Stop Playback" in response.text
        fake.playing = False
        await wait_for(
            lambda: inputs.status.heartbeat is not None and not inputs.status.heartbeat.playing
        )
        task = asyncio.create_task(inputs.discover_song_order())
        await wait_for(lambda: inputs.discovery == "running")
        assert (
            await client.post("/api/v1/playback-api/discover-song-order", json={"confirm": True})
        ).status_code == 409
        assert (await client.post("/api/v1/playback-api/find")).status_code == 409
        saved = app.state.runtime.settings_service.snapshot()
        assert (
            await client.put("/api/v1/settings", json=saved.model_dump(mode="json"))
        ).status_code == 409
        assert (
            await client.put("/api/v1/playback-api/settings", json={"enabled": False})
        ).status_code == 409
        assert app.state.runtime.settings_service.snapshot() == saved
        await task
        assert (
            await client.put("/api/v1/playback-api/settings", json={"enabled": False})
        ).status_code == 200
        assert (
            await client.post("/api/v1/playback-api/discover-song-order", json={"confirm": True})
        ).status_code == 409


@pytest.mark.parametrize("failure", ["disconnect", "silence", "version", "wrap", "limit"])
async def test_failure_never_persists_partial_result(failure: str) -> None:
    fake = FakePlayback()
    async with application(fake, saved=True) as (app, client):
        inputs = controller(app)
        before = app.state.runtime.settings_service.snapshot().playback_api
        fake.disconnect_on_command = failure == "disconnect"
        fake.silent = failure == "silence"
        fake.version_on_command = failure == "version"
        fake.wrap = failure == "wrap"
        if failure == "limit":
            inputs._max_steps = 1
        result = await client.post(
            "/api/v1/playback-api/discover-song-order", json={"confirm": True}
        )
        assert result.status_code == 200 and result.json()["discovery"] == "failed"
        assert result.json()["last_error"]
        assert app.state.runtime.settings_service.snapshot().playback_api == before
        assert not inputs.mapper.discovery


async def test_live_mapping_known_restart_stale_unknown_and_monitor_bound() -> None:
    fake = FakePlayback(index=0)
    async with application(fake, saved=True) as (app, client):
        inputs = controller(app)
        fake.playing = True
        await wait_for(lambda: app.state.runtime.state_store._state.current_song is not None)
        state = (await client.get("/api/v1/state")).json()
        assert state["current_song_index"] == 0
        fake.position = 3
        await asyncio.sleep(0.03)
        fake.playing = False
        await asyncio.sleep(0.03)
        fake.position = 0
        await asyncio.sleep(0.03)
        fake.playing = True
        await asyncio.sleep(0.03)
        state = (await client.get("/api/v1/state")).json()
        assert any(event["type"] == "song.restarted" for event in state["recent_events"])
        dispatcher = AsyncMock()
        inputs.mapper._dispatcher = dispatcher
        fake.index = 1
        await wait_for(lambda: dispatcher.dispatch_song_position.await_count == 1)
        dispatcher.dispatch_song_position.assert_awaited_once_with(2, source="playback_api")
        fake.version = 9
        fake.index = 2
        await wait_for(lambda: inputs.mapper.stale)
        await asyncio.sleep(0.03)
        assert dispatcher.dispatch_song_position.await_count == 1
        fake.playing = False
        await wait_for(lambda: dispatcher.dispatch.await_count == 1)
        dispatcher.dispatch.assert_awaited_with(ActionName.STOP_TIMER, source="playback_api")
        fake.songs[2] = 12345
        await asyncio.sleep(0.03)
        assert inputs.mapper.stale
        # Normal receive operations have never sent an application command.
        assert fake.commands == []
        from stagepilot.plugins.playback_api.normalizer import PlaybackEvent

        for i in range(150):
            inputs._observe(inputs._generation, inputs.status, (PlaybackEvent("seek", float(i)),))
        monitor = (await client.get("/api/v1/playback-api/events")).json()
        assert len(monitor["events"]) == monitor["capacity"] == 100
        assert monitor["events"][-1]["event"]["type"] == "seek"


async def test_reconnect_revalidates_and_source_switch_closes_old_listeners() -> None:
    fake = FakePlayback(index=0)
    midi = FakeMidiBackend(["Playback MIDI"])
    async with application(fake, saved=True, midi=midi) as (app, client):
        inputs = controller(app)
        dispatcher = AsyncMock()
        inputs.mapper._dispatcher = dispatcher
        for writer in tuple(fake.writers):
            writer.close()
        await wait_for(lambda: not inputs.status.connected)
        await wait_for(lambda: inputs.status.connected)
        assert not inputs.mapper.stale
        dispatcher.dispatch_song_position.assert_not_awaited()
        old_client = inputs.client
        assert old_client is not None
        old_observer = old_client._observer
        saved = (await client.get("/api/v1/settings")).json()["settings"]
        saved["integration_modes"]["midi_source"] = "real"
        result = await client.put("/api/v1/settings", json=saved)
        assert result.status_code == 200 and not result.json()["restart_required"]
        await wait_for(lambda: bool(midi.ports))
        assert not old_client.status.connected
        assert (await client.get("/api/v1/midi/inputs")).json()["enabled"]
        assert old_observer is not None
        from stagepilot.plugins.playback_api.normalizer import PlaybackEvent

        old_observer(old_client.status, (PlaybackEvent("song.started", 1, 900),))
        dispatcher.dispatch_song_position.assert_not_awaited()
        await client.post("/api/v1/midi/cue-simulation", json={"cue": "start_next"})
        assert (await client.get("/api/v1/state")).json()["current_song_index"] == 0
        old_port = midi.ports[-1]
        saved["integration_modes"]["midi_source"] = "playback_api"
        assert (await client.put("/api/v1/settings", json=saved)).status_code == 200
        assert old_port.closed
        await wait_for(lambda: inputs.status.connected)
        state_before = (await client.get("/api/v1/state")).json()["current_song_index"]
        old_port.callback(MidiMessage(type="note_on", channel=1, note=112, velocity=2))
        await asyncio.sleep(0.03)
        assert (await client.get("/api/v1/state")).json()["current_song_index"] == state_before
        assert (
            await client.post("/api/v1/midi/cue-simulation", json={"cue": "start_next"})
        ).status_code == 409


async def test_settings_override_disable_find_no_order_edit_or_controls() -> None:
    fake = FakePlayback()
    async with application(fake, saved=True) as (app, client):
        result = await client.put(
            "/api/v1/playback-api/settings",
            json={
                "host": "192.0.2.10",
                "port": 8081,
                "auto_scan": False,
            },
        )
        assert result.status_code == 200
        inputs = controller(app)
        assert inputs.client is not None
        assert inputs.client.options.host_override == "192.0.2.10"
        assert inputs.client.options.port == 8081 and not inputs.client.options.auto_scan
        assert (await client.post("/api/v1/playback-api/find")).status_code == 200
        assert (
            await client.put("/api/v1/playback-api/settings", json={"song_order": [1]})
        ).status_code == 422
        saved = (await client.get("/api/v1/settings")).json()["settings"]
        saved["playback_api"]["song_order"] = [1]
        assert (await client.put("/api/v1/settings", json=saved)).status_code == 400
        for command in ["play", "pause", "select", "seek", "confirm-song-order"]:
            path = f"/api/v1/playback-api/{command}"
            assert path not in app.openapi()["paths"]
            # A built dashboard's GET-only static catch-all returns 405; without
            # a dashboard, the same absent control route returns 404.
            assert (await client.post(path)).status_code in {404, 405}
        assert (
            await client.put("/api/v1/playback-api/settings", json={"host": "http://bad/"})
        ).status_code == 422
        assert fake.commands == []


async def test_shutdown_cancels_discovery_without_saving() -> None:
    fake = FakePlayback()
    async with application(fake) as (app, _):
        inputs = controller(app)
        fake.silent = True
        task = asyncio.create_task(inputs.discover_song_order())
        await wait_for(lambda: inputs.discovery == "running")
        await inputs.stop()
        assert task.cancelled()
        assert inputs.discovery == "failed" and not inputs.mapper.discovery
        assert inputs.client is not None and not inputs.client.status.connected
        assert inputs._consumer is None
        assert app.state.runtime.settings_service.snapshot().playback_api.song_order == []


async def test_default_status_remote_connections_diagnostic_and_disable() -> None:
    app = create_app(Settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        inputs = controller(app)
        await wait_for(lambda: inputs.status.last_error is not None)
        data = (await client.get("/api/v1/playback-api/status")).json()
        assert data["selected"] and data["enabled"] and not data["connected"]
        assert "remote connections may be off" in data["last_error"]
        assert data["song_order"] == [] and data["stale"]
        response = await client.put("/api/v1/playback-api/settings", json={"enabled": False})
        assert response.status_code == 200 and not response.json()["enabled"]
        assert not inputs.status.connected
        assert (await client.post("/api/v1/playback-api/find")).status_code == 409


async def test_persistence_failure_leaves_live_configuration_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePlayback()
    async with application(fake) as (app, client):
        inputs = controller(app)
        previous = inputs.client

        def fail_save(_: object) -> None:
            raise SettingsFileError("Settings could not be saved atomically.")

        monkeypatch.setattr(app.state.runtime.settings_service._store, "save", fail_save)
        result = await client.put("/api/v1/playback-api/settings", json={"enabled": False})
        assert result.status_code == 503
        assert inputs.client is previous and inputs.settings.playback_api.enabled
        result = await client.post(
            "/api/v1/playback-api/discover-song-order", json={"confirm": True}
        )
        assert result.json()["discovery"] == "failed" and inputs.mapper.song_order == ()


async def test_pending_start_dropped_across_disconnect_without_invalidating_order() -> None:
    fake = FakePlayback()
    async with application(fake, saved=True) as (app, _):
        inputs = controller(app)
        dispatcher = AsyncMock()
        inputs.mapper._dispatcher = dispatcher
        queued = inputs.mapper.observe(
            inputs.status.heartbeat, (PlaybackEvent("song.started", 1, 17),)
        )
        inputs.mapper.discard_pending()
        # First reconnect heartbeat validates, but cannot replay the old envelope.
        inputs.mapper.observe(Heartbeat(17, 0, False, False, 8), ())
        assert not inputs.mapper.stale
        for item in queued:
            await inputs.mapper.dispatch(item)
        dispatcher.dispatch_song_position.assert_not_awaited()


async def test_migrated_install_starts_playback_and_retains_midi_alternative(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "integration_modes": {
                    "service_source": "demo",
                    "midi_source": "real",
                    "timer_output": "simulated",
                },
                "midi": {
                    "enabled": True,
                    "input_name": "Playback MIDI",
                    "channel": 9,
                    "note": 110,
                    "debounce_ms": 321,
                },
            }
        )
    )
    service = SettingsService(SettingsFileStore(path), MemoryCredentialStore(), environ={})
    fake = FakePlayback()
    midi = FakeMidiBackend(["Playback MIDI"])
    async with application(fake, midi=midi, settings_service=service) as (app, client):
        data = (await client.get("/api/v1/playback-api/status")).json()
        assert data["selected"] and data["enabled"] and data["connected"]
        assert not midi.ports
        stored = json.loads(path.read_text())
        assert stored["schema_version"] == 2
        assert stored["midi"]["channel"] == 9 and stored["midi"]["debounce_ms"] == 321
        saved = (await client.get("/api/v1/settings")).json()["settings"]
        saved["integration_modes"]["midi_source"] = "real"
        assert (await client.put("/api/v1/settings", json=saved)).status_code == 200
        await wait_for(lambda: bool(midi.ports))
        assert controller(app).settings.midi.channel == 9
        assert controller(app).settings.midi.note == 110
