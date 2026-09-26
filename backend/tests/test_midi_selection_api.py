from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

from stagepilot.core.config import MidiSettings, PlanningCenterSettings, Settings
from stagepilot.main import create_app
from stagepilot.models.api import (
    MidiCueSimulationResponse,
    MidiInputSelectionResponse,
    MidiInputsResponse,
)
from stagepilot.models.state import ApplicationState
from stagepilot.plugins.midi_playback.client import (
    MidiInputPortContract,
    MidiMessageCallback,
)
from stagepilot.plugins.midi_playback.models import MidiMessage
from stagepilot.plugins.planning_center.models import (
    PlanDiscoveryResult,
    PlanningCenterServiceType,
    PlanNotFoundResult,
)

CONFIGURED_INPUT = "Configured Playback"
BACKUP_INPUT = "Backup Playback"
TARGET_DATE = date(2030, 7, 14)


class FakeMidiPort:
    def __init__(self, name: str, callback: MidiMessageCallback) -> None:
        self.name = name
        self.callback = callback
        self._closed = False
        self.close_calls = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self.close_calls += 1
        self._closed = True

    def emit(
        self,
        *,
        note: int,
        message_type: Literal["note_on", "note_off"] = "note_on",
        velocity: int = 100,
    ) -> None:
        self.callback(
            MidiMessage(
                type=message_type,
                channel=1,
                note=note,
                velocity=velocity,
            )
        )


class FakeMidiBackend:
    def __init__(self, names: list[str]) -> None:
        self.names = list(names)
        self.opened_names: list[str] = []
        self.ports: list[FakeMidiPort] = []

    def set_names(self, names: list[str]) -> None:
        self.names = list(names)

    def list_input_names(self) -> list[str]:
        return list(self.names)

    def open_input(
        self,
        name: str,
        callback: MidiMessageCallback,
    ) -> MidiInputPortContract:
        if self.names.count(name) != 1:
            raise ValueError("The fake MIDI input must be uniquely available.")
        self.opened_names.append(name)
        port = FakeMidiPort(name, callback)
        self.ports.append(port)
        return port


class FakeMidiBackendFactory:
    def __init__(self, backend: FakeMidiBackend) -> None:
        self.backend = backend

    def __call__(self) -> FakeMidiBackend:
        return self.backend


class EmptyPlanningCenterClient:
    service_type = PlanningCenterServiceType(
        id="42",
        name="Weekend Services",
        sequence=1,
    )

    async def list_service_types(self) -> list[PlanningCenterServiceType]:
        return [self.service_type]

    async def load_plan_for_date(
        self,
        service_type: PlanningCenterServiceType,
        target_date: date,
        timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        assert service_type == self.service_type
        assert timezone_name == "America/Los_Angeles"
        assert selected_plan_id is None
        assert lookahead_days == 30
        return PlanNotFoundResult(
            service_type=service_type,
            target_date=target_date,
        )

    async def load_plan_for_service_types(
        self,
        service_types: list[PlanningCenterServiceType],
        target_date: date,
        timezone_name: str,
        *,
        selected_plan_id: str | None = None,
        lookahead_days: int = 0,
    ) -> PlanDiscoveryResult:
        service_type = service_types[0]
        assert timezone_name == "America/Los_Angeles"
        return PlanNotFoundResult(
            service_type=service_type,
            target_date=target_date,
        )

    async def close(self) -> None:
        return None


class EmptyPlanningCenterFactory:
    def __call__(
        self,
        _settings: PlanningCenterSettings,
    ) -> EmptyPlanningCenterClient:
        return EmptyPlanningCenterClient()


def fixed_today(_timezone: ZoneInfo) -> date:
    return TARGET_DATE


def build_app(backend: FakeMidiBackend) -> FastAPI:
    settings = Settings(
        demo_mode=False,
        timezone="America/Los_Angeles",
        planning_center=PlanningCenterSettings(
            app_id="test-app-id",
            secret="test-secret",
            service_type_id="42",
        ),
        midi=MidiSettings(
            enabled=True,
            input_name=CONFIGURED_INPUT,
            debounce_ms=0,
        ),
    )
    return create_app(
        settings,
        planning_center_client_factory=EmptyPlanningCenterFactory(),
        planning_center_today_provider=fixed_today,
        midi_backend_factory=FakeMidiBackendFactory(backend),
    )


def input_id(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def get_inputs(client: TestClient) -> MidiInputsResponse:
    response = client.get("/api/v1/midi/inputs")
    assert response.status_code == 200
    return MidiInputsResponse.model_validate(response.json())


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for asynchronous MIDI selection work.")


def wait_for_snapshot(
    client: TestClient,
    *,
    selected_name: str | None,
    connected_name: str | None,
) -> MidiInputsResponse:
    latest = get_inputs(client)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        connected = next((value.name for value in latest.inputs if value.connected), None)
        if latest.selected_input_name == selected_name and connected == connected_name:
            return latest
        time.sleep(0.005)
        latest = get_inputs(client)
    raise AssertionError(
        "Timed out waiting for the selected and connected MIDI input snapshot; "
        f"latest was selected={latest.selected_input_name!r}."
    )


def midi_note_event_count(state: ApplicationState) -> int:
    return sum(event.type == "midi.note_received" for event in state.recent_events)


def test_refresh_selects_opaque_id_switches_promptly_and_rejects_old_callback() -> None:
    backend = FakeMidiBackend([CONFIGURED_INPUT])
    app = build_app(backend)

    with TestClient(app) as client:
        wait_until(lambda: len(backend.ports) == 1)
        old_port = backend.ports[0]
        backend.set_names([CONFIGURED_INPUT, BACKUP_INPUT])

        refresh_response = client.post("/api/v1/midi/inputs/refresh")
        assert refresh_response.status_code == 200
        refreshed = MidiInputsResponse.model_validate(refresh_response.json())
        backup = next(value for value in refreshed.inputs if value.name == BACKUP_INPUT)
        started_at = time.monotonic()

        selection_response = client.post(
            "/api/v1/midi/input-selection",
            json={"input_id": backup.id},
        )

        assert selection_response.status_code == 200
        selection = MidiInputSelectionResponse.model_validate(selection_response.json())
        assert selection.accepted is True
        assert selection.midi.configured_input_name == CONFIGURED_INPUT
        assert selection.midi.selected_input_name == BACKUP_INPUT
        assert backup.id == input_id(BACKUP_INPUT)
        wait_until(
            lambda: (
                old_port.closed
                and len(backend.ports) == 2
                and backend.ports[-1].name == BACKUP_INPUT
            ),
            timeout=0.75,
        )
        assert time.monotonic() - started_at < 0.75

        switched = wait_for_snapshot(
            client,
            selected_name=BACKUP_INPUT,
            connected_name=BACKUP_INPUT,
        )
        selected = next(value for value in switched.inputs if value.selected)
        assert switched.configured_input_name == CONFIGURED_INPUT
        assert selected.name == BACKUP_INPUT
        assert selected.connected is True
        assert app.state.runtime.settings_service.snapshot().midi.input_name == BACKUP_INPUT
        assert backend.opened_names == [CONFIGURED_INPUT, BACKUP_INPUT]
        assert old_port.close_calls == 1

        before = ApplicationState.model_validate(client.get("/api/v1/state").json())
        old_port.emit(note=112)
        backend.ports[-1].emit(note=112, velocity=105)

        latest = before

        def valid_callback_was_processed() -> bool:
            nonlocal latest
            latest = ApplicationState.model_validate(client.get("/api/v1/state").json())
            return latest.last_action == "stop_timer"

        wait_until(valid_callback_was_processed)
        assert midi_note_event_count(latest) == midi_note_event_count(before) + 1


def test_null_selection_disconnects_hardware_but_keeps_simulation_available() -> None:
    backend = FakeMidiBackend([CONFIGURED_INPUT])
    app = build_app(backend)

    with TestClient(app) as client:
        wait_until(lambda: len(backend.ports) == 1)
        port = backend.ports[0]

        response = client.post(
            "/api/v1/midi/input-selection",
            json={"input_id": None},
        )

        assert response.status_code == 200
        selection = MidiInputSelectionResponse.model_validate(response.json())
        assert selection.accepted is True
        assert selection.midi.configured_input_name == CONFIGURED_INPUT
        assert selection.midi.selected_input_name is None
        wait_until(lambda: port.closed, timeout=0.75)
        disconnected = wait_for_snapshot(
            client,
            selected_name=None,
            connected_name=None,
        )
        assert not any(value.selected or value.connected for value in disconnected.inputs)

        simulation_response = client.post(
            "/api/v1/midi/cue-simulation",
            json={"cue": "stop_timer"},
        )

        assert simulation_response.status_code == 200
        simulation = MidiCueSimulationResponse.model_validate(simulation_response.json())
        assert simulation.accepted is True
        assert simulation.state.last_action == "stop_timer"


def test_stale_selection_returns_conflict_and_retains_previous_session_choice() -> None:
    backend = FakeMidiBackend([CONFIGURED_INPUT, BACKUP_INPUT])
    app = build_app(backend)

    with TestClient(app) as client:
        wait_until(lambda: len(backend.ports) == 1)
        select_backup = client.post(
            "/api/v1/midi/input-selection",
            json={"input_id": input_id(BACKUP_INPUT)},
        )
        assert select_backup.status_code == 200
        wait_for_snapshot(
            client,
            selected_name=BACKUP_INPUT,
            connected_name=BACKUP_INPUT,
        )

        backend.set_names([CONFIGURED_INPUT])
        stale_response = client.post(
            "/api/v1/midi/input-selection",
            json={"input_id": input_id(BACKUP_INPUT)},
        )

        assert stale_response.status_code == 409
        assert stale_response.json() == {
            "detail": "The selected MIDI input is no longer available."
        }
        retained = get_inputs(client)
        assert retained.configured_input_name == CONFIGURED_INPUT
        assert retained.selected_input_name == BACKUP_INPUT


def test_ambiguous_duplicate_name_is_rejected_without_changing_selection() -> None:
    duplicate_name = "Duplicate Playback"
    backend = FakeMidiBackend([CONFIGURED_INPUT, duplicate_name, duplicate_name])
    app = build_app(backend)

    with TestClient(app) as client:
        wait_until(lambda: len(backend.ports) == 1)
        inputs = get_inputs(client)
        duplicate = next(value for value in inputs.inputs if value.name == duplicate_name)
        assert duplicate.ambiguous is True

        response = client.post(
            "/api/v1/midi/input-selection",
            json={"input_id": duplicate.id},
        )

        assert response.status_code == 409
        assert response.json() == {"detail": "The selected MIDI input name is ambiguous."}
        retained = get_inputs(client)
        assert retained.selected_input_name == CONFIGURED_INPUT
        configured = next(value for value in retained.inputs if value.name == CONFIGURED_INPUT)
        assert configured.selected is True
        assert configured.connected is True


def test_selection_requires_an_explicit_null_or_valid_opaque_id() -> None:
    backend = FakeMidiBackend([CONFIGURED_INPUT])
    app = build_app(backend)

    with TestClient(app) as client:
        missing = client.post("/api/v1/midi/input-selection", json={})
        malformed = client.post(
            "/api/v1/midi/input-selection",
            json={"input_id": "Configured Playback"},
        )

    assert missing.status_code == 422
    assert malformed.status_code == 422


def test_session_selection_resets_to_configuration_when_plugin_restarts() -> None:
    backend = FakeMidiBackend([CONFIGURED_INPUT, BACKUP_INPUT])
    app = build_app(backend)

    with TestClient(app) as first_client:
        wait_until(lambda: len(backend.ports) == 1)
        response = first_client.post(
            "/api/v1/midi/input-selection",
            json={"input_id": input_id(BACKUP_INPUT)},
        )
        assert response.status_code == 200
        wait_for_snapshot(
            first_client,
            selected_name=BACKUP_INPUT,
            connected_name=BACKUP_INPUT,
        )

    previous_port_count = len(backend.ports)
    with TestClient(app) as restarted_client:
        wait_until(lambda: len(backend.ports) > previous_port_count)
        restarted = wait_for_snapshot(
            restarted_client,
            selected_name=CONFIGURED_INPUT,
            connected_name=CONFIGURED_INPUT,
        )

        assert restarted.configured_input_name == CONFIGURED_INPUT
        assert restarted.selected_input_name == CONFIGURED_INPUT
        assert backend.opened_names[-1] == CONFIGURED_INPUT
