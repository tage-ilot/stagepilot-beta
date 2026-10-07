"""Every Keychain read can raise a macOS password prompt: keep reads minimal."""

from __future__ import annotations

from pathlib import Path

import pytest

from stagepilot.core import settings as settings_module
from stagepilot.core.settings import (
    KeyringCredentialStore,
    SettingsFileStore,
    SettingsService,
)


class FakeKeyring:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.reads = 0

    def get_password(self, service: str, account: str) -> str | None:
        self.reads += 1
        return self.value

    def set_password(self, service: str, account: str, secret: str) -> None:
        self.value = secret

    def delete_password(self, service: str, account: str) -> None:
        self.value = None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring("secret-value")
    monkeypatch.setattr(settings_module, "keyring", fake)
    return fake


def test_repeated_reads_hit_the_keychain_once(fake: FakeKeyring) -> None:
    store = KeyringCredentialStore()
    assert [store.get_secret() for _ in range(5)] == ["secret-value"] * 5
    assert fake.reads == 1


def test_cache_follows_our_own_writes_and_removals(fake: FakeKeyring) -> None:
    store = KeyringCredentialStore()
    store.set_secret("new")
    assert store.get_secret() == "new"
    store.remove_secret()
    assert store.get_secret() is None
    assert fake.reads == 0


def test_no_keychain_read_when_planning_center_is_not_configured(
    fake: FakeKeyring, tmp_path: Path
) -> None:
    service = SettingsService(
        SettingsFileStore(tmp_path / "settings.json"), KeyringCredentialStore()
    )
    service.load()
    assert fake.reads == 0
    service.load()
    assert fake.reads == 0


def test_configured_planning_center_reads_once_per_launch(
    fake: FakeKeyring, tmp_path: Path
) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version": 2, "planning_center": {"app_id": "abc"}}')
    service = SettingsService(SettingsFileStore(path), KeyringCredentialStore())
    service.load()
    service.load()
    assert fake.reads == 1
