"""Regression test: saving Planning Center settings must not block the event loop.

Prior to the fix, `update_planning_center_settings` called
`SettingsService.update_planning_center` (which calls into the synchronous
`keyring` library) directly on the request-handling coroutine. Since the
FastAPI app runs on a single asyncio event loop, any slow/blocking call made
without `asyncio.to_thread` freezes every other coroutine on that loop -
including the `/ws` state-stream handler - for the duration of the call.

This test simulates a slow credential-store backend (e.g. a native OS
keychain prompt) and asserts that other scheduled async work keeps making
progress concurrently with the settings save. Before the fix this test fails
(the counter barely advances during the blocking call); after the fix it
passes (the counter advances at its normal ticking rate throughout).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from stagepilot.core.settings import SettingsFileStore, SettingsService
from stagepilot.main import create_app

SLOW_SECRET_DELAY_SECONDS = 0.5
TICK_INTERVAL_SECONDS = 0.01


class SlowCredentialStore:
    """Mimics a native OS credential backend with real wall-clock latency."""

    def __init__(self, delay: float = SLOW_SECRET_DELAY_SECONDS) -> None:
        self._delay = delay
        self.secret: str | None = None

    def get_secret(self) -> str | None:
        return self.secret

    def set_secret(self, secret: str) -> None:
        time.sleep(self._delay)
        self.secret = secret

    def remove_secret(self) -> None:
        time.sleep(self._delay)
        self.secret = None


@pytest.mark.asyncio
async def test_planning_center_settings_save_does_not_block_event_loop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    credentials = SlowCredentialStore()
    service = SettingsService(SettingsFileStore(path), credentials)
    runtime_settings = service.load()
    app = create_app(runtime_settings, settings_service=service)

    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            ticks += 1

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ticker_task = asyncio.create_task(ticker())
        try:
            # Give the ticker a moment to start counting before the slow save.
            await asyncio.sleep(TICK_INTERVAL_SECONDS * 2)
            ticks_before = ticks

            response = await client.post(
                "/api/v1/planning-center/settings",
                json={
                    "app_id": "visible-app-id",
                    "service_type_id": "42",
                    "secret": "slow-secret",
                },
            )

            ticks_during = ticks - ticks_before
        finally:
            stop.set()
            ticker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ticker_task

    assert response.status_code == 200

    # The slow save takes SLOW_SECRET_DELAY_SECONDS. If it were run directly
    # on the event loop (unwrapped in a thread), no other coroutine -
    # including our ticker - could run during that window, so `ticks_during`
    # would stay near zero. Once the credential-store call is offloaded via
    # asyncio.to_thread, the ticker keeps firing at its ~10ms cadence
    # throughout the save.
    expected_minimum_ticks = int((SLOW_SECRET_DELAY_SECONDS / TICK_INTERVAL_SECONDS) * 0.5)
    assert ticks_during >= expected_minimum_ticks, (
        f"event loop appears blocked during Planning Center settings save: "
        f"only {ticks_during} ticks fired (expected >= {expected_minimum_ticks})"
    )
