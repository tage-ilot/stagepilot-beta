from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from stagepilot.core.config import (
    IntegrationModes,
    MidiSource,
    MidiTransport,
    PlaybackApiSettings,
    Settings,
)
from stagepilot.core.settings import (
    MemoryCredentialStore,
    PersistentSettings,
    SettingsFileStore,
    SettingsService,
)


def test_new_defaults() -> None:
    assert Settings().integration_modes.midi_source is MidiSource.PLAYBACK_API
    saved = PersistentSettings()
    assert saved.schema_version == 2
    assert saved.playback_api == PlaybackApiSettings(
        enabled=True, host=None, port=8080, auto_scan=True
    )
    assert saved.to_runtime(None).playback_api == saved.playback_api


@pytest.mark.parametrize("source", ["real", "simulated"])
@pytest.mark.parametrize("transport", ["local", "network"])
def test_v1_migration_preserves_values_and_is_idempotent(
    tmp_path: Path, source: str, transport: str
) -> None:
    original = PersistentSettings().model_dump(mode="json")
    original["schema_version"] = 1
    original.pop("playback_api")
    original.pop("network_midi")
    original["integration_modes"]["midi_source"] = source
    original["midi"].update(
        enabled=True,
        transport=transport,
        input_name="Example MIDI",
        channel=7,
        note=83,
        debounce_ms=432,
    )
    original["midi"]["mappings"]["stop_timer"] = 127
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    store = SettingsFileStore(path)
    migrated = store.load()
    assert migrated is not None
    assert migrated.integration_modes.midi_source is MidiSource.PLAYBACK_API
    assert migrated.midi.model_dump(mode="json") == original["midi"]
    for key in ("lights", "planning_center", "timezone", "propresenter", "onboarding"):
        assert migrated.model_dump(mode="json")[key] == original[key]
    assert original["schema_version"] == 1
    encoded = path.read_bytes()
    assert json.loads(encoded)["schema_version"] == 2
    assert store.load() == migrated
    assert path.read_bytes() == encoded
    assert PersistentSettings.model_validate(migrated.model_dump()) == migrated


@pytest.mark.parametrize("source", list(MidiSource))
def test_explicit_v2_selection_and_playback_fields_survive_restart(
    tmp_path: Path, source: MidiSource
) -> None:
    path = tmp_path / "settings.json"
    store = SettingsFileStore(path)
    saved = PersistentSettings(
        integration_modes=IntegrationModes(midi_source=source),
        playback_api=PlaybackApiSettings(
            enabled=False,
            host="192.0.2.10",
            port=8090,
            auto_scan=False,
            song_order=[101, 202],
            captured_version=11,
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    saved.midi.enabled = True
    saved.midi.transport = MidiTransport.NETWORK
    saved.network_midi.socket_path = "/example/midi.sock"
    store.save(saved)
    service = SettingsService(store, MemoryCredentialStore(), environ={})
    runtime = service.load()
    assert runtime.integration_modes.midi_source is source
    assert runtime.midi.enabled is (source is MidiSource.REAL)
    assert runtime.playback_api == saved.playback_api
    assert runtime.network_midi == saved.network_midi
    assert service.snapshot() == saved
    assert service.effective_snapshot().midi.enabled is True
    service.save(service.snapshot())
    assert store.load() == saved


def test_runtime_round_trip_retains_network_configuration() -> None:
    runtime = Settings()
    runtime.network_midi.socket_path = "/example/midi.sock"
    runtime.midi.enabled = True
    saved = PersistentSettings.from_runtime(runtime)
    assert saved.midi.enabled is True
    assert saved.to_runtime(None).network_midi == runtime.network_midi


@pytest.mark.parametrize("version", [0, 3, True, "1"])
def test_unsupported_schema_rejected(version: object) -> None:
    with pytest.raises(ValidationError):
        PersistentSettings.model_validate({"schema_version": version})


@pytest.mark.parametrize(
    "host,expected", [("", None), ("  ", None), (" 192.0.2.10 ", "192.0.2.10")]
)
def test_manual_host_normalized(host: str, expected: str | None) -> None:
    assert PlaybackApiSettings(host=host).host == expected


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        PlaybackApiSettings(port=port)


def test_unversioned_file_migrates(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"integration_modes":{"midi_source":"real"}}', encoding="utf-8")
    saved = SettingsFileStore(path).load()
    assert saved is not None
    assert saved.integration_modes.midi_source is MidiSource.PLAYBACK_API
    assert json.loads(path.read_text())["schema_version"] == 2


@pytest.mark.parametrize("order", [[True], ["101"], [101, 101], list(range(201))])
def test_invalid_discovered_order_rejected(order: list[object]) -> None:
    with pytest.raises(ValidationError):
        PlaybackApiSettings.model_validate({"song_order": order})
