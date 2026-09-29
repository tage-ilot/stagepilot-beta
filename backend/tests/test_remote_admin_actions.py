"""Installation-side handling of operator-queued fleet admin actions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from stagepilot.remote_beta_control import BetaRemoteControl
from stagepilot.remote_files import BetaControlConfig
from stagepilot.services.remote_auth import RemoteAuthError, RemoteStore

INSTALLATION = "a" * 16
CREDENTIAL = f"spi_{INSTALLATION}.signature-not-a-real-secret"


class FakeControlPlane:
    """Stands in for the Worker; records what the installation actually called."""

    def __init__(self, pending: list[dict[str, Any]] | None = None) -> None:
        self.pending = pending if pending is not None else []
        self.calls: list[tuple[str, str]] = []
        self.acked: list[str] = []
        self.approvals: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        self.authorizations.append(request.headers.get("Authorization"))
        payload = json.loads(request.content) if request.content else {}
        if path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "installationId": INSTALLATION,
                    "phase": "provisioned",
                    "pendingActions": self.pending,
                },
            )
        if path.endswith("/ack"):
            self.acked.append(path.split("/")[-2])
            return httpx.Response(200, json={"acknowledged": path.split("/")[-2]})
        if path.endswith("/approval-requests"):
            if request.method == "POST":
                record = {
                    "requestId": "req-1",
                    "installationId": INSTALLATION,
                    "reason": payload["reason"],
                    "status": "pending",
                }
                self.approvals.append(record)
                return httpx.Response(201, json=record)
            return httpx.Response(200, json={"requests": self.approvals})
        raise AssertionError(f"Unexpected control-plane route: {path}")


def build_control(
    tmp_path: Path,
    plane: FakeControlPlane,
    handlers: dict[str, Any] | None = None,
) -> BetaRemoteControl:
    config = BetaControlConfig(
        control_plane_url="https://control.example.com",
        installation_id=INSTALLATION,
        hostname=f"sp-{INSTALLATION}.remote.example.com",
        credential_file=None,
        state_dir=tmp_path / "state",
        installation_dir=tmp_path / "installation",
    )
    client = httpx.Client(base_url=config.control_plane_url, transport=httpx.MockTransport(plane))
    return BetaRemoteControl(
        config,
        client,
        credential_provider=lambda: CREDENTIAL,
        action_handlers=handlers or {},
        sleep=lambda _seconds: None,
    )


def action(kind: str = "rate_limit_reset", installation: str = INSTALLATION) -> dict[str, Any]:
    return {
        "actionId": f"{installation}:{kind}:0001",
        "installationId": installation,
        "kind": kind,
        "requestedAt": "2026-01-01T00:00:00.000Z",
    }


def test_no_pending_actions_applies_nothing(tmp_path: Path) -> None:
    plane = FakeControlPlane()
    applied: list[str] = []
    control = build_control(tmp_path, plane, {"rate_limit_reset": lambda: applied.append("x")})
    assert control.apply_pending_actions() == []
    assert applied == []
    assert plane.acked == []


def test_queued_reset_is_applied_then_acknowledged(tmp_path: Path) -> None:
    plane = FakeControlPlane([action()])
    applied: list[str] = []
    control = build_control(tmp_path, plane, {"rate_limit_reset": lambda: applied.append("reset")})
    assert control.apply_pending_actions() == ["rate_limit_reset"]
    assert applied == ["reset"]
    assert plane.acked == [f"{INSTALLATION}:rate_limit_reset:0001"]
    # The installation credential authenticates every call, including the ack.
    assert set(plane.authorizations) == {f"Bearer {CREDENTIAL}"}


def test_action_attributed_to_another_installation_is_ignored(tmp_path: Path) -> None:
    plane = FakeControlPlane([action(installation="b" * 16)])
    applied: list[str] = []
    control = build_control(tmp_path, plane, {"rate_limit_reset": lambda: applied.append("reset")})
    assert control.apply_pending_actions() == []
    assert applied == []
    assert plane.acked == []


def test_unknown_action_kind_is_ignored_and_not_acknowledged(tmp_path: Path) -> None:
    plane = FakeControlPlane([action(kind="something_new")])
    control = build_control(tmp_path, plane, {"rate_limit_reset": lambda: None})
    assert control.apply_pending_actions() == []
    assert plane.acked == []


def test_failed_handler_leaves_action_unacknowledged(tmp_path: Path) -> None:
    plane = FakeControlPlane([action()])

    def explode() -> None:
        raise RuntimeError("local reset failed")

    control = build_control(tmp_path, plane, {"rate_limit_reset": explode})
    with pytest.raises(RuntimeError):
        control.apply_pending_actions()
    # Not acked, so the operator's action is redelivered on the next poll.
    assert plane.acked == []


def test_approval_request_and_outcome_polling(tmp_path: Path) -> None:
    plane = FakeControlPlane()
    control = build_control(tmp_path, plane)
    created = control.request_approval("quota increase for a real user")
    assert created["status"] == "pending"
    assert created["reason"] == "quota increase for a real user"
    plane.approvals[0]["status"] = "approved"
    assert [item["status"] for item in control.approval_requests()] == ["approved"]


def test_reset_login_attempts_is_idempotent_and_clears_the_throttle(tmp_path: Path) -> None:
    store = RemoteStore(tmp_path / "remote.sqlite3")
    store.bootstrap("operator@example.com", "correct-horse-battery-staple")

    # Exhaust the per-email login throttle (limit 5 within the window).
    for _ in range(5):
        with pytest.raises(RemoteAuthError):
            store.login("operator@example.com", "wrong-password")
    with pytest.raises(RemoteAuthError) as exhausted:
        store.login("operator@example.com", "correct-horse-battery-staple")
    assert exhausted.value.status == 429

    store.reset_login_attempts()
    # A legitimate user can log in again immediately after the operator reset.
    assert store.login("operator@example.com", "correct-horse-battery-staple")

    # Idempotent: repeating the reset on already-clear state is a no-op and
    # leaves users/sessions intact.
    store.reset_login_attempts()
    store.reset_login_attempts()
    assert [user["email"] for user in store.users()] == ["operator@example.com"]
