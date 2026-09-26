from __future__ import annotations

import hashlib
import time
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stagepilot.core.config import (
    MidiNoteMappings,
    MidiSettings,
    PlanningCenterSettings,
    Settings,
    get_settings,
)
from stagepilot.core.midi import MidiCueName
from stagepilot.main import create_app
from stagepilot.models.api import (
    HealthResponse,
    MidiCueSimulationResponse,
    MidiInputsResponse,
    MidiMonitorResponse,
)
from stagepilot.models.state import (
    ApplicationState,
    ConnectionStatus,
    PluginStatus,
    ServicePlan,
    Song,
)
from stagepilot.plugins.midi_playback.client import (
    MidiInputPortContract,
    MidiMessageCallback,
)
from stagepilot.plugins.midi_playback.models import MidiMessage
from stagepilot.plugins.planning_center.models import (
    PlanDiscoveryResult,
    PlanLoadedResult,
    PlanningCenterPlanCandidate,
    PlanningCenterServiceType,
)

TARGET_DATE = date(2030, 7, 14)
MIDI_INPUT_NAME = "Playback MIDI"


class FakeMidiPort:
    def __init__(self, callback: MidiMessageCallback) -> None:
        self.callback = callback
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True


class FakeMidiBackend:
    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.opened_names: list[str] = []
        self.ports: list[FakeMidiPort] = []

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
        port = FakeMidiPort(callback)
        self.ports.append(port)
        return port


class RecordingMidiBackendFactory:
    def __init__(self, backend: FakeMidiBackend) -> None:
        self.backend = backend
        self.calls = 0

    def __call__(self) -> FakeMidiBackend:
        self.calls += 1
        return self.backend


class LoadedPlanningCenterClient:
    def __init__(self) -> None:
        self.closed = False
        self.service_type = PlanningCenterServiceType(
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
        assert target_date == TARGET_DATE
        assert timezone_name == "America/Los_Angeles"
        assert selected_plan_id is None
        assert lookahead_days == 30
        candidate = PlanningCenterPlanCandidate(
            id="plan-1",
            title="Sunday Worship",
            service_type_id=service_type.id,
            service_type_name=service_type.name,
            target_date=target_date,
            service_times=[datetime(2030, 7, 14, 16, 0, tzinfo=UTC)],
        )
        return PlanLoadedResult(
            candidate=candidate,
            plan=ServicePlan(
                id=candidate.id,
                title=candidate.title,
                date=target_date,
                service_type=service_type.name,
                songs=[
                    Song(
                        id="song-1",
                        title="Battle Belongs",
                        duration_seconds=281,
                        order=1,
                    )
                ],
            ),
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
        return await self.load_plan_for_date(
            service_types[0],
            target_date,
            timezone_name,
            selected_plan_id=selected_plan_id,
            lookahead_days=lookahead_days,
        )

    async def close(self) -> None:
        self.closed = True


class RecordingPlanningCenterFactory:
    def __init__(self, client: LoadedPlanningCenterClient) -> None:
        self.client = client
        self.calls = 0

    def __call__(self, _settings: PlanningCenterSettings) -> LoadedPlanningCenterClient:
        self.calls += 1
        return self.client


def fixed_today(_timezone: ZoneInfo) -> date:
    return TARGET_DATE


def production_settings(*, midi: MidiSettings | None = None) -> Settings:
    return Settings(
        demo_mode=False,
        timezone="America/Los_Angeles",
        planning_center=PlanningCenterSettings(
            app_id="test-app-id",
            secret="test-secret",
            service_type_id="42",
        ),
        midi=midi or MidiSettings(),
    )


def wait_for_midi_status(
    client: TestClient,
    expected: ConnectionStatus,
    *,
    timeout: float = 3.0,
) -> ApplicationState:
    deadline = time.monotonic() + timeout
    latest = ApplicationState()
    while time.monotonic() < deadline:
        response = client.get("/api/v1/state")
        assert response.status_code == 200
        latest = ApplicationState.model_validate(response.json())
        if latest.midi_status is expected:
            return latest
        time.sleep(0.01)
    raise AssertionError(
        f"MIDI status did not become {expected.value!r}; latest was {latest.midi_status.value!r}."
    )


def test_midi_settings_have_safe_disabled_defaults() -> None:
    midi = Settings().midi

    assert midi.enabled is False
    assert midi.input_name is None
    assert midi.channel == 1
    assert midi.note == 112
    assert midi.debounce_ms == 250
    assert midi.mappings.configured() == (
        (MidiCueName.START_NEXT, 100),
        (MidiCueName.RESTART_CURRENT, 101),
        (MidiCueName.PREVIOUS, 102),
        (MidiCueName.NEXT, 103),
        (MidiCueName.RELOAD_PLAN, 104),
        (MidiCueName.STOP_TIMER, 105),
    )


def test_environment_loads_every_midi_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    environment = {
        "STAGEPILOT_PCO_APP_ID": "",
        "STAGEPILOT_PCO_SECRET": "",
        "STAGEPILOT_MIDI_ENABLED": "yes",
        "STAGEPILOT_MIDI_INPUT_NAME": "  Playback MIDI  ",
        "STAGEPILOT_MIDI_CHANNEL": "16",
        "STAGEPILOT_MIDI_NOTE": "72",
        "STAGEPILOT_MIDI_START_NEXT_VELOCITY": "110",
        "STAGEPILOT_MIDI_RESTART_CURRENT_VELOCITY": "111",
        "STAGEPILOT_MIDI_PREVIOUS_VELOCITY": "112",
        "STAGEPILOT_MIDI_NEXT_VELOCITY": "113",
        "STAGEPILOT_MIDI_RELOAD_PLAN_VELOCITY": "114",
        "STAGEPILOT_MIDI_STOP_TIMER_VELOCITY": "115",
        "STAGEPILOT_MIDI_DEBOUNCE_MS": "0",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        midi = get_settings().midi
    finally:
        get_settings.cache_clear()

    assert midi.enabled is True
    assert midi.input_name == MIDI_INPUT_NAME
    assert midi.channel == 16
    assert midi.note == 72
    assert midi.debounce_ms == 0
    assert dict(midi.mappings.configured()) == {
        MidiCueName.START_NEXT: 110,
        MidiCueName.RESTART_CURRENT: 111,
        MidiCueName.PREVIOUS: 112,
        MidiCueName.NEXT: 113,
        MidiCueName.RELOAD_PLAN: 114,
        MidiCueName.STOP_TIMER: 115,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel", 0),
        ("channel", 17),
        ("debounce_ms", -1),
        ("debounce_ms", 2001),
    ],
)
def test_midi_channel_and_debounce_are_bounded(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        MidiSettings.model_validate({field: value})


@pytest.mark.parametrize("note", [-1, 128])
def test_midi_trigger_note_is_bounded(note: int) -> None:
    with pytest.raises(ValidationError):
        MidiSettings(note=note)


@pytest.mark.parametrize("velocity", [0, 128])
def test_midi_velocities_are_bounded(velocity: int) -> None:
    with pytest.raises(ValidationError):
        MidiNoteMappings(start_next=velocity)


def test_midi_velocities_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="distinct velocity"):
        MidiNoteMappings(start_next=100, next=100)


def test_disabled_midi_never_constructs_hardware_and_rejects_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stagepilot.api.routes._current_local_date",
        lambda _timezone_name: TARGET_DATE,
    )
    planning_center_client = LoadedPlanningCenterClient()
    midi_backend = FakeMidiBackend([MIDI_INPUT_NAME])
    midi_factory = RecordingMidiBackendFactory(midi_backend)
    app = create_app(
        production_settings(),
        planning_center_client_factory=RecordingPlanningCenterFactory(planning_center_client),
        planning_center_today_provider=fixed_today,
        midi_backend_factory=midi_factory,
    )

    with TestClient(app) as client:
        inputs_response = client.get("/api/v1/midi/inputs")
        monitor_response = client.get("/api/v1/midi/messages")
        simulation_response = client.post(
            "/api/v1/midi/cue-simulation",
            json={"cue": MidiCueName.START_NEXT},
        )
        health = HealthResponse.model_validate(client.get("/api/v1/health").json())

    inputs = MidiInputsResponse.model_validate(inputs_response.json())
    assert inputs_response.status_code == 200
    assert MidiMonitorResponse.model_validate(monitor_response.json()).messages == []
    assert inputs.enabled is False
    assert inputs.inputs == []
    assert inputs.mappings[MidiCueName.START_NEXT] == 100
    assert simulation_response.status_code == 409
    assert simulation_response.json() == {"detail": "The MIDI Playback plugin is disabled."}
    assert health.status == "healthy"
    assert [plugin.name for plugin in health.plugins] == ["lights", "planning_center"]
    assert midi_factory.calls == 0


def test_demo_mode_stays_hardware_free_even_when_midi_is_enabled() -> None:
    midi_backend = FakeMidiBackend([MIDI_INPUT_NAME])
    midi_factory = RecordingMidiBackendFactory(midi_backend)
    app = create_app(
        Settings(
            demo_mode=True,
            midi=MidiSettings(enabled=True, input_name=MIDI_INPUT_NAME),
        ),
        midi_backend_factory=midi_factory,
    )

    with TestClient(app) as client:
        inputs = MidiInputsResponse.model_validate(client.get("/api/v1/midi/inputs").json())
        health = HealthResponse.model_validate(client.get("/api/v1/health").json())

    assert inputs.enabled is False
    assert inputs.inputs == []
    assert [plugin.name for plugin in health.plugins] == ["lights", "demo"]
    assert midi_factory.calls == 0


def test_enabled_production_app_registers_midi_and_exposes_safe_discovery_and_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stagepilot.api.routes._current_local_date",
        lambda _timezone_name: TARGET_DATE,
    )
    planning_center_client = LoadedPlanningCenterClient()
    planning_center_factory = RecordingPlanningCenterFactory(planning_center_client)
    midi_backend = FakeMidiBackend(["Backup Controller", MIDI_INPUT_NAME])
    midi_factory = RecordingMidiBackendFactory(midi_backend)
    app = create_app(
        production_settings(
            midi=MidiSettings(
                enabled=True,
                input_name=MIDI_INPUT_NAME,
                channel=9,
                debounce_ms=0,
            )
        ),
        planning_center_client_factory=planning_center_factory,
        planning_center_today_provider=fixed_today,
        midi_backend_factory=midi_factory,
    )

    with TestClient(app) as client:
        wait_for_midi_status(client, ConnectionStatus.CONNECTED)
        inputs_response = client.get("/api/v1/midi/inputs")
        midi_backend.ports[0].callback(
            MidiMessage(type="note_on", channel=9, note=16, velocity=100)
        )
        monitor = MidiMonitorResponse(messages=[])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            monitor = MidiMonitorResponse.model_validate(client.get("/api/v1/midi/messages").json())
            if monitor.messages:
                break
            time.sleep(0.01)
        rejected_response = client.post(
            "/api/v1/midi/cue-simulation",
            json={"cue": MidiCueName.PREVIOUS},
        )
        accepted_response = client.post(
            "/api/v1/midi/cue-simulation",
            json={"cue": MidiCueName.START_NEXT},
        )
        health = HealthResponse.model_validate(client.get("/api/v1/health").json())

    assert inputs_response.status_code == 200
    inputs = MidiInputsResponse.model_validate(inputs_response.json())
    assert inputs.enabled is True
    assert inputs.channel == 9
    assert inputs.note == 112
    assert inputs.configured_input_name == MIDI_INPUT_NAME
    assert [value.name for value in inputs.inputs] == ["Backup Controller", MIDI_INPUT_NAME]
    selected = next(value for value in inputs.inputs if value.selected)
    assert selected.connected is True
    assert selected.ambiguous is False
    assert selected.id == hashlib.sha256(MIDI_INPUT_NAME.encode()).hexdigest()
    assert len(selected.id) == 64
    assert monitor.messages[0].note == 16
    assert monitor.messages[0].note_name == "E-1"
    assert monitor.messages[0].disposition.value == "unmapped"

    assert rejected_response.status_code == 200
    rejected = MidiCueSimulationResponse.model_validate(rejected_response.json())
    assert rejected.cue is MidiCueName.PREVIOUS
    assert rejected.accepted is False
    assert rejected.message == "Already at the first song."

    assert accepted_response.status_code == 200
    accepted = MidiCueSimulationResponse.model_validate(accepted_response.json())
    assert accepted.cue is MidiCueName.START_NEXT
    assert accepted.action.value == MidiCueName.START_NEXT.value
    assert accepted.accepted is True
    assert accepted.state.current_song is not None
    assert accepted.state.current_song.title == "Battle Belongs"

    assert health.status == "healthy"
    assert [plugin.name for plugin in health.plugins] == [
        "lights",
        "planning_center",
        "midi_playback",
    ]
    assert all(plugin.status is PluginStatus.RUNNING for plugin in health.plugins)
    assert midi_factory.calls == 1
    assert midi_backend.opened_names == [MIDI_INPUT_NAME]
    assert midi_backend.ports and all(port.closed for port in midi_backend.ports)
    assert planning_center_factory.calls == 1
    assert planning_center_client.closed is True


def test_saved_note_and_channel_reconfigure_the_running_midi_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stagepilot.api.routes._current_local_date",
        lambda _timezone_name: TARGET_DATE,
    )
    midi_backend = FakeMidiBackend([MIDI_INPUT_NAME])
    app = create_app(
        production_settings(
            midi=MidiSettings(
                enabled=True,
                input_name=MIDI_INPUT_NAME,
                debounce_ms=0,
            )
        ),
        planning_center_client_factory=RecordingPlanningCenterFactory(LoadedPlanningCenterClient()),
        planning_center_today_provider=fixed_today,
        midi_backend_factory=RecordingMidiBackendFactory(midi_backend),
    )

    with TestClient(app) as client:
        wait_for_midi_status(client, ConnectionStatus.CONNECTED)
        payload = client.get("/api/v1/settings").json()["settings"]
        payload["midi"]["channel"] = 7
        payload["midi"]["note"] = 124

        saved = client.put("/api/v1/settings", json=payload)
        inputs = MidiInputsResponse.model_validate(client.get("/api/v1/midi/inputs").json())

        port = midi_backend.ports[0]
        port.callback(MidiMessage(type="note_on", channel=7, note=112, velocity=100))
        port.callback(MidiMessage(type="note_on", channel=7, note=124, velocity=100))

        deadline = time.monotonic() + 2
        state = ApplicationState()
        monitor = MidiMonitorResponse(messages=[])
        while time.monotonic() < deadline:
            state = ApplicationState.model_validate(client.get("/api/v1/state").json())
            monitor = MidiMonitorResponse.model_validate(client.get("/api/v1/midi/messages").json())
            if state.current_song is not None and len(monitor.messages) == 2:
                break
            time.sleep(0.01)

    assert saved.status_code == 200
    assert saved.json()["restart_required"] is False
    assert saved.json()["settings"]["midi"]["channel"] == 7
    assert saved.json()["settings"]["midi"]["note"] == 124
    assert inputs.channel == 7
    assert inputs.note == 124
    assert state.current_song is not None
    assert state.current_song.title == "Battle Belongs"
    assert [message.disposition.value for message in monitor.messages] == [
        "dispatched",
        "unmapped",
    ]
    assert [(message.note, message.note_name) for message in monitor.messages] == [
        (124, "E8"),
        (112, "E7"),
    ]


def test_production_health_stays_degraded_until_configured_midi_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stagepilot.api.routes._current_local_date",
        lambda _timezone_name: TARGET_DATE,
    )
    midi_backend = FakeMidiBackend([])
    midi_factory = RecordingMidiBackendFactory(midi_backend)
    app = create_app(
        production_settings(
            midi=MidiSettings(enabled=True, input_name=MIDI_INPUT_NAME),
        ),
        planning_center_client_factory=RecordingPlanningCenterFactory(LoadedPlanningCenterClient()),
        planning_center_today_provider=fixed_today,
        midi_backend_factory=midi_factory,
    )

    with TestClient(app) as client:
        degraded = HealthResponse.model_validate(client.get("/api/v1/health").json())
        assert degraded.status == "degraded"
        midi_health = next(plugin for plugin in degraded.plugins if plugin.name == "midi_playback")
        assert midi_health.status is PluginStatus.STARTING

        midi_backend.names.append(MIDI_INPUT_NAME)
        wait_for_midi_status(client, ConnectionStatus.CONNECTED)
        healthy = HealthResponse.model_validate(client.get("/api/v1/health").json())

    assert healthy.status == "healthy"
    midi_health = next(plugin for plugin in healthy.plugins if plugin.name == "midi_playback")
    assert midi_health.status is PluginStatus.RUNNING
    assert midi_factory.calls == 1
