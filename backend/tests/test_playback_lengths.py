from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from stagepilot.core.config import PlaybackApiSettings
from stagepilot.core.settings import SettingsFileStore
from test_playback_integration import FakePlayback, application, controller, wait_for


def test_old_settings_file_loads_without_lengths(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 2, "playback_api": {"song_order": [9, 8]}}))
    saved = SettingsFileStore(path).load()
    assert saved is not None
    assert saved.playback_api.song_lengths == []
    assert saved.playback_api.lengths_measured_at is None


@pytest.mark.parametrize("lengths", [[1.0], [1.0, 2.0, 3.0], [float("nan"), None], [-1, None]])
def test_lengths_are_aligned_and_valid(lengths: list[float | None]) -> None:
    with pytest.raises(ValidationError):
        PlaybackApiSettings(song_order=[9, 8], song_lengths=lengths)


def test_order_replacement_clears_old_lengths() -> None:
    saved = PlaybackApiSettings(
        song_order=[9, 8], song_lengths=[123.4, None], lengths_measured_at=datetime.now(UTC)
    )
    replacement = saved.model_copy(update={"song_order": [8, 9]})
    assert replacement.song_lengths == []
    assert replacement.lengths_measured_at is None
    assert saved.model_copy(update={"host": "localhost"}).song_lengths == saved.song_lengths


async def test_no_effect_seek_saves_none_and_order_and_emits_no_actions() -> None:
    fake = FakePlayback(index=2, lengths=[279.53, None, 260.17])
    async with application(fake) as (app, client):
        inputs = controller(app)
        dispatcher = AsyncMock()
        inputs.mapper._dispatcher = dispatcher
        data = (
            await client.post("/api/v1/playback-api/discover-song-order", json={"confirm": True})
        ).json()
        assert data["discovery"] == "done"
        assert data["song_order"] == fake.songs
        assert data["song_lengths"] == fake.lengths
        assert data["progress"] >= 3 * len(fake.songs)
        assert fake.index == 2 and fake.position == 0
        await asyncio.sleep(0.15)
        dispatcher.dispatch.assert_not_awaited()
        dispatcher.dispatch_song_position.assert_not_awaited()
        events = (await client.get("/api/v1/playback-api/events")).json()["events"]
        own = [item for item in events if item["event"]["type"] in {"seek", "song.stopped"}]
        assert own and all(item["discovery"] for item in own)
        assert set(fake.commands) <= {
            "transportPreviousSong",
            "transportNextSong",
            "waveformSeek",
            "transportReturnToStart",
        }


@pytest.mark.parametrize("initial", [0, 1, 2])
async def test_cancel_mid_measurement_saves_no_partial_lengths(initial: int) -> None:
    fake = FakePlayback(index=initial, lengths=[None, 123, 234])
    async with application(fake) as (app, _):
        inputs = controller(app)
        before = app.state.runtime.settings_service.snapshot().playback_api
        task = asyncio.create_task(inputs.discover_song_order())
        await wait_for(lambda: "waveformSeek" in fake.commands)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app.state.runtime.settings_service.snapshot().playback_api == before
        assert fake.position == 0
        assert fake.index == initial
        assert not inputs.mapper.discovery


@pytest.mark.parametrize("change", ["version", "identity", "unknown"])
async def test_stale_order_clears_persisted_lengths(change: str) -> None:
    fake = FakePlayback(index=0)
    async with application(fake) as (app, client):
        inputs = controller(app)
        await fake.broadcast('{"contentLoadSetlist":{"setlistData":{"setlistID":122}}}')
        await wait_for(lambda: inputs.status.setlist_id == 122)
        await inputs.discover_song_order()
        assert inputs.settings.playback_api.song_lengths
        if change == "version":
            fake.version += 1
        elif change == "identity":
            await fake.broadcast('{"contentLoadSetlist":{"setlistData":{"setlistID":123}}}')
        else:
            fake.songs[0] = 12345
        await wait_for(lambda: inputs.mapper.stale)
        assert inputs.settings.playback_api.song_lengths == []
        saved = app.state.runtime.settings_service.snapshot().playback_api
        assert saved.song_lengths == [] and saved.lengths_measured_at is None
        data = (await client.get("/api/v1/playback-api/status")).json()
        assert data["song_lengths"] == [] and data["lengths_measured_at"] is None


async def test_scan_does_not_change_plan_or_timer_duration() -> None:
    fake = FakePlayback(index=0, lengths=[1.5, 2.5, 3.5])
    async with application(fake) as (app, client):
        before = (await client.get("/api/v1/state")).json()
        assert before["plan"] is not None
        expected = before["plan"]["songs"][0]["duration_seconds"]
        data = (
            await client.post("/api/v1/playback-api/discover-song-order", json={"confirm": True})
        ).json()
        assert data["song_lengths"] == [1.5, 2.5, 3.5]
        after = (await client.get("/api/v1/state")).json()
        assert after["plan"] == before["plan"]
        assert after["timer"] == before["timer"]
        assert data["plan_songs"][0]["duration_seconds"] == expected
        fake.playing = True
        await wait_for(
            lambda: app.state.runtime.state_store._state.timer.duration_seconds is not None
        )
        state = (await client.get("/api/v1/state")).json()
        assert state["timer"]["duration_seconds"] == expected
        assert expected != fake.lengths[0]


async def test_unexpected_measurement_failure_does_not_fail_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePlayback(index=0)
    async with application(fake) as (app, _):
        inputs = controller(app)
        assert inputs.client is not None
        monkeypatch.setattr(
            inputs.client, "measure_song_length", AsyncMock(side_effect=RuntimeError("read failed"))
        )
        await inputs.discover_song_order()
        assert inputs.discovery == "done"
        saved = app.state.runtime.settings_service.snapshot().playback_api
        assert saved.song_order == fake.songs
        assert saved.song_lengths == [None, None, None]
