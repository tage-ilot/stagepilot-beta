"""Application fixtures never discover or contact real Playback devices."""

import pytest


@pytest.fixture(autouse=True)
def no_live_playback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit transport/discovery tests use injected fake loopback endpoints.
    # The default app finder otherwise scans the production LAN after migration.
    monkeypatch.setattr("stagepilot.plugins.playback_api.client.find_playback", lambda **_: None)
