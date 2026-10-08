from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from stagepilot.core.config import PlanningCenterSettings
from stagepilot.models.state import ConnectionStatus
from stagepilot.plugins.planning_center.client import PlanningCenterClient
from test_planning_center_lengths import Rig
from test_planning_center_plan_discovery import item_resource, plan_resource, plan_time_resource
from test_playback_integration import FakePlayback, application, controller, wait_for


class UpcomingRig(Rig):
    def __init__(self) -> None:
        super().__init__(count=3)
        today = datetime.now(UTC).replace(hour=18, minute=0, second=0, microsecond=0)
        self.times = {
            "past": today - timedelta(days=2),
            "plan": today,
            "later": today + timedelta(days=7),
        }
        self.no_plans = False
        self.ambiguous = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/service_types"):
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "ServiceType",
                            "id": "type",
                            "attributes": {"name": "Weekend", "sequence": 1},
                        },
                        {
                            "type": "ServiceType",
                            "id": "other",
                            "attributes": {"name": "Youth", "sequence": 2},
                        },
                    ]
                },
            )
        if path.endswith("/plans"):
            self.requests.append(request)
            plans = (
                []
                if self.no_plans
                else [
                    plan_resource(key, key, sort_date=value.isoformat())
                    for key, value in self.times.items()
                ]
            )
            if self.ambiguous:
                plans.append(
                    plan_resource("evening", "Evening", sort_date=self.times["plan"].isoformat())
                )
            return httpx.Response(200, json={"data": plans})
        if path.endswith("/plan_times"):
            self.requests.append(request)
            key = path.split("/")[-2]
            value = self.times.get(key, self.times["plan"] + timedelta(hours=2))
            return httpx.Response(200, json={"data": [plan_time_resource(key, value.isoformat())]})
        if path.endswith("/items"):
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [
                        item_resource("header", "Welcome", "header", 0),
                        *[
                            item_resource(
                                str(i), f"Song {i}", "song", i + 1, length=self.remote[str(i)]
                            )
                            for i in range(3)
                        ],
                    ]
                },
            )
        return super().handle(request)


async def attach(app: FastAPI, rig: UpcomingRig) -> None:
    inputs = controller(app)
    inputs.planning_center_lengths = rig.service
    rig.settings = inputs._service
    rig.service.settings = rig.settings
    rig.state = inputs.state_store
    rig.service.state = rig.state
    inputs.settings.planning_center.service_type_id = "type"
    await rig.state.mutate(
        lambda state: setattr(state, "planning_center_status", ConnectionStatus.CONNECTED)
    )
    await rig.state.mutate(lambda state: setattr(state, "plan", rig.plan.model_copy(deep=True)))


async def test_explicit_api_preview_category_confirm_restore_no_automatic_write() -> None:
    fake = FakePlayback(index=0, lengths=[101, 102, 151])
    async with application(fake) as (app, client):
        rig = UpcomingRig()
        await attach(app, rig)
        prefix = "/api/v1/playback-api/planning-center-lengths"
        assert (await client.post(prefix + "/preview")).status_code == 409
        await client.post("/api/v1/playback-api/discover-song-order", json={"confirm": True})
        assert not rig.requests
        categories = (await client.get(prefix + "/categories")).json()
        assert [c["id"] for c in categories] == ["type", "other"]
        choice = await client.put(prefix + "/category", json={"service_type_id": "type"})
        assert choice.status_code == 200
        assert rig.settings.snapshot().playback_api.planning_center_update_service_type_id == "type"
        preview = await client.post(prefix + "/preview")
        assert preview.status_code == 200, preview.text
        data = preview.json()
        assert data["plan_title"] == "plan"  # today, not past or later
        assert len(data["items"]) == 3  # header excluded
        assert all(r.method == "GET" for r in rig.requests)
        updated = await client.post(
            prefix + "/confirm", json={"token": data["token"], "confirm": True}
        )
        assert updated.status_code == 200, updated.text
        assert rig.remote == {"0": 101, "1": 102, "2": 151}
        assert updated.json()["planning_center_lengths"]["reload"] == "verified"
        assert updated.json()["song_lengths"] == fake.lengths
        rig.dispatcher.dispatch.assert_awaited_once()
        assert (await client.post(prefix + "/restore")).status_code == 400
        restored = await client.post(prefix + "/restore", json={"confirm": True})
        assert not restored.json()["planning_center_undo_available"]
        assert rig.remote == {"0": 100, "1": 100, "2": 100}


@pytest.mark.parametrize("reason", ["failed", "stale", "missing", "playing", "disconnected"])
async def test_api_unavailable_never_reads_or_writes(reason: str) -> None:
    fake = FakePlayback(index=0)
    async with application(fake) as (app, client):
        rig = UpcomingRig()
        await attach(app, rig)
        inputs = controller(app)
        await inputs.discover_song_order()
        if reason == "failed":
            inputs.discovery = "failed"
        elif reason == "stale":
            inputs.mapper.captured_version = -1
            inputs.mapper.observe(inputs._heartbeat, ())
        elif reason == "missing":
            inputs.settings.playback_api.song_lengths = [None] * len(fake.songs)
        elif reason == "playing":
            fake.playing = True
            await wait_for(
                lambda: bool(inputs.status.heartbeat and inputs.status.heartbeat.playing)
            )
        else:
            await rig.state.mutate(
                lambda state: setattr(
                    state, "planning_center_status", ConnectionStatus.DISCONNECTED
                )
            )
        response = await client.post("/api/v1/playback-api/planning-center-lengths/preview")
        assert response.status_code == 409
        assert not rig.requests


async def test_no_upcoming_plan_plain_error_and_no_write() -> None:
    rig = UpcomingRig()
    rig.no_plans = True
    with pytest.raises(ValueError, match="No upcoming plan"):
        await rig.service.preview("type", [101, 102, 103], "scan")
    assert all(r.method == "GET" for r in rig.requests)
    rig.dispatcher.dispatch.assert_not_awaited()


async def test_preferred_service_time_uses_shared_selection() -> None:
    rig = UpcomingRig()
    rig.ambiguous = True
    # UTC 20:00 is 13:00 in configured America/Los_Angeles.
    config = rig.settings.snapshot()
    rig.settings.save(
        config.model_copy(
            update={
                "planning_center": config.planning_center.model_copy(
                    update={"preferred_service_time": "13:00"}
                )
            }
        )
    )
    preview = await rig.service.preview("type", [101, 102, 103], "scan")
    assert preview.plan_title == "Evening"
    assert all(r.method == "GET" for r in rig.requests)


@pytest.mark.parametrize("failure", [401, 403, 429, 500, "timeout"])
async def test_api_write_failure_keeps_scan_and_later_attempt_works(failure: int | str) -> None:
    fake = FakePlayback(index=0, lengths=[101, 102, 151])
    async with application(fake) as (app, client):
        rig = UpcomingRig()
        await attach(app, rig)
        inputs = controller(app)
        await inputs.discover_song_order()
        rig.failures["0"] = failure
        prefix = "/api/v1/playback-api/planning-center-lengths"
        preview = (await client.post(prefix + "/preview")).json()
        response = await client.post(
            prefix + "/confirm",
            json={"token": preview["token"], "confirm": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["planning_center_lengths"]["status"] == "failed"
        assert data["song_lengths"] == fake.lengths and data["discovery"] == "done"
        assert rig.settings.snapshot().playback_api.song_lengths == fake.lengths
        rig.dispatcher.dispatch.assert_not_awaited()
        rig.failures.clear()
        # Definite refusals retain no undo. Uncertain writes can be restored
        # safely first: unchanged items are skipped and the record is cleared.
        if rig.service.undo_available:
            await rig.service.restore()
        preview = (await client.post(prefix + "/preview")).json()
        response = await client.post(
            prefix + "/confirm",
            json={"token": preview["token"], "confirm": True},
        )
        assert response.json()["planning_center_lengths"]["status"] == "done"
        assert rig.remote == {"0": 101, "1": 102, "2": 151}


async def test_lengths_and_undo_cannot_be_forged_through_settings_and_choice_is_preserved() -> None:
    fake = FakePlayback(index=0)
    async with application(fake) as (app, client):
        rig = UpcomingRig()
        await attach(app, rig)
        inputs = controller(app)
        await inputs.discover_song_order()
        await client.put(
            "/api/v1/playback-api/planning-center-lengths/category",
            json={"service_type_id": "type"},
        )
        saved = (await client.get("/api/v1/settings")).json()["settings"]
        saved["playback_api"]["song_lengths"] = [1, 1, 1]
        assert (await client.put("/api/v1/settings", json=saved)).status_code == 400
        assert inputs.settings.playback_api.song_lengths == fake.lengths
        saved["playback_api"]["song_lengths"] = fake.lengths
        saved["playback_api"]["planning_center_update_service_type_id"] = "other"
        assert (await client.put("/api/v1/settings", json=saved)).status_code == 200
        assert rig.settings.snapshot().playback_api.planning_center_update_service_type_id == "type"
        await rig.update(rig.plan, [101, 102, 103])
        assert rig.service.undo_available
        saved = (await client.get("/api/v1/settings")).json()["settings"]
        saved["planning_center_length_undo"] = []
        assert (await client.put("/api/v1/settings", json=saved)).status_code == 200
        assert rig.service.undo_available


@pytest.mark.parametrize("code", [401, 403])
async def test_denied_preview_is_plain_and_read_only(code: int) -> None:
    fake = FakePlayback(index=0)
    async with application(fake) as (app, client):
        rig = UpcomingRig()
        await attach(app, rig)
        await controller(app).discover_song_order()

        def denied(request: httpx.Request) -> httpx.Response:
            rig.requests.append(request)
            return httpx.Response(code)

        rig.service.client_factory = lambda: PlanningCenterClient(
            PlanningCenterSettings(app_id="app", secret="secret"),
            transport=httpx.MockTransport(denied),
        )
        response = await client.post("/api/v1/playback-api/planning-center-lengths/preview")
        assert response.status_code == 403
        assert "can't edit this plan" in response.json()["detail"]
        assert all(r.method == "GET" for r in rig.requests)
        assert controller(app).discovery == "done"
