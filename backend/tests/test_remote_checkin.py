"""Always-on lightweight check-in: decoupled from the Remote Access toggle.

Covers:
- the lightweight check-in enrolls and reaches status without Remote
  Access ever being enabled;
- deviceName capture/validation (valid, too long, empty, non-string,
  control chars);
- that running the lightweight check-in path never triggers tunnel/DNS
  provisioning (provision/reconcile/enable/disable/revoke are never
  called) -- only enroll + GET status.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from stagepilot.remote_bootstrap import DesktopBootstrapStore, sanitize_device_name
from stagepilot.remote_desktop import DesktopRemoteManager

TEST_ORIGINS = frozenset({"https://control.example.com"})


class MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, installation_id: str) -> str | None:
        return self.values.get(installation_id)

    def set(self, installation_id: str, credential: str) -> None:
        self.values[installation_id] = credential

    def delete(self, installation_id: str) -> None:
        self.values.pop(installation_id, None)


class CheckinOnlyControlPlane:
    """Only understands enroll + GET status -- any other route fails the test.

    This is the whole point of the "no provisioning" assertion: if the
    lightweight check-in path ever called provision/reconcile/disable/
    revoke, this fake would raise AssertionError instead of silently
    succeeding.
    """

    def __init__(self) -> None:
        self.installation_id = ""
        self.hostname = ""
        self.credential = ""
        self.enroll_calls = 0
        self.status_calls = 0
        self.last_enroll_device_name: str | None = None
        self.last_status_device_name: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/v1/installations/enroll"):
            self.enroll_calls += 1
            self.last_enroll_device_name = payload.get("deviceName")
            if not self.installation_id:
                self.installation_id = uuid4().hex
                self.hostname = f"sp-{self.installation_id}.remote.example.com"
                self.credential = f"spi_{self.installation_id}." + "s" * 43
            return httpx.Response(
                201,
                json={
                    "installationId": self.installation_id,
                    "hostname": self.hostname,
                    "installationCredential": self.credential,
                },
            )
        if request.url.path.endswith("/status") and request.method == "GET":
            self.status_calls += 1
            self.last_status_device_name = payload.get("deviceName")
            return httpx.Response(
                200,
                json={
                    "installationId": self.installation_id,
                    "hostname": self.hostname,
                    "phase": "disabled",
                    "pendingActions": [],
                },
            )
        raise AssertionError(
            f"Unexpected control-plane route hit by the lightweight check-in: "
            f"{request.method} {request.url.path}"
        )


def build_manager(tmp_path: Path, fake: CheckinOnlyControlPlane) -> DesktopRemoteManager:
    binary = tmp_path / "resources/cloudflared"
    binary.parent.mkdir()
    binary.write_bytes(b"test binary")
    store = DesktopBootstrapStore(
        tmp_path / "remote/bootstrap.json",
        MemoryCredentials(),
        trusted_origins=TEST_ORIGINS,
    )
    return DesktopRemoteManager(
        tmp_path / "remote",
        binary,
        bootstrap_store=store,
        transport=httpx.MockTransport(fake),
        control_plane_origin="https://control.example.com",
    )


def test_lightweight_checkin_enrolls_without_remote_access_enabled(tmp_path: Path) -> None:
    fake = CheckinOnlyControlPlane()
    manager = build_manager(tmp_path, fake)

    assert manager.feature.intent().enabled is False
    manager.lightweight_checkin()

    assert fake.enroll_calls == 1
    assert fake.status_calls == 1
    assert manager.bootstrap.state().active is not None
    # Remote Access intent is completely untouched by the lightweight path.
    assert manager.feature.intent().enabled is False


def test_lightweight_checkin_sends_sanitized_device_name(tmp_path: Path) -> None:
    fake = CheckinOnlyControlPlane()
    manager = build_manager(tmp_path, fake)
    manager.device_name = "  My-Desktop\x07.local  "

    manager.lightweight_checkin()

    assert fake.last_enroll_device_name == "My-Desktop.local"
    assert fake.last_status_device_name == "My-Desktop.local"


def test_lightweight_checkin_omits_invalid_device_name(tmp_path: Path) -> None:
    fake = CheckinOnlyControlPlane()
    manager = build_manager(tmp_path, fake)
    manager.device_name = None

    manager.lightweight_checkin()

    assert fake.last_enroll_device_name is None
    assert fake.last_status_device_name is None


def test_lightweight_checkin_repeated_runs_never_call_provisioning_routes(
    tmp_path: Path,
) -> None:
    """Running the check-in repeatedly (as run()'s loop does) still only
    ever hits enroll/status -- never provision/reconcile/disable/revoke.
    The fake raises AssertionError on any other route, so this test fails
    loudly if a future change accidentally wires provisioning into the
    unconditional check-in path.
    """

    fake = CheckinOnlyControlPlane()
    manager = build_manager(tmp_path, fake)

    for _ in range(3):
        manager.lightweight_checkin()

    # ensure_enrolled() is idempotent once an active identity exists locally
    # (see DesktopBootstrapStore.ensure_enrolled), so only the first call
    # actually hits the enroll route; every call still hits status.
    assert fake.enroll_calls == 1
    assert fake.status_calls == 3


@pytest.mark.asyncio
async def test_run_loop_checks_in_even_when_remote_access_never_enabled(
    tmp_path: Path,
) -> None:
    fake = CheckinOnlyControlPlane()
    manager = build_manager(tmp_path, fake)
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    try:
        for _ in range(100):
            if fake.enroll_calls >= 1 and fake.status_calls >= 1:
                break
            await asyncio.sleep(0.02)
        assert fake.enroll_calls >= 1
        assert fake.status_calls >= 1
        assert manager.feature.intent().enabled is False
    finally:
        stop.set()
        await task


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("my-laptop.local", "my-laptop.local"),
        ("  padded  ", "padded"),
        ("a" * 300, "a" * 253),
        ("bad\x07name\x00here", "badnamehere"),
        ("", None),
        ("   ", None),
        (None, None),
        (123, None),
        (["not", "a", "string"], None),
    ],
)
def test_sanitize_device_name(raw: object, expected: str | None) -> None:
    assert sanitize_device_name(raw) == expected  # type: ignore[arg-type]
