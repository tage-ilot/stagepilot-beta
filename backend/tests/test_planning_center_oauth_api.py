"""HTTP surface for the Planning Center OAuth sign-in flow."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stagepilot.core.config import Settings
from stagepilot.core.settings import MemoryCredentialStore
from stagepilot.main import create_app
from stagepilot.planning_center_oauth import ControlPlaneOAuthClient, OAuthTokens

FLOW = {"flow_id": "aabbccdd", "ticket": "ticket-1"}
TOKENS = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "expires_in": 7200,
    "scope": "services",
    "token_type": "Bearer",
}


def app_with(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    store: MemoryCredentialStore,
) -> FastAPI:
    monkeypatch.setenv("STAGEPILOT_PCO_CLIENT_ID", "client-id-1")
    # A mock transport so no real control-plane call is ever made here.
    return create_app(
        Settings(),
        oauth_credential_store=store,
        oauth_control_plane=ControlPlaneOAuthClient(transport=httpx.MockTransport(handler)),
    )


def control_plane_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/flow"):
        return httpx.Response(201, json=FLOW)
    return httpx.Response(200, json=TOKENS)


def test_sign_in_round_trip_marks_the_installation_as_oauth_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryCredentialStore()
    application = app_with(monkeypatch, control_plane_handler, store)
    with TestClient(application) as client:
        started = client.post("/api/v1/planning-center/oauth/start")
        assert started.status_code == 200
        state = started.json()["state"]
        assert started.json()["authorize_url"].startswith(
            "https://api.planningcenteronline.com/oauth/authorize?"
        )
        assert "scope=services" in started.json()["authorize_url"]

        completed = client.post(
            "/api/v1/planning-center/oauth/callback",
            json={
                "state": state,
                "code": "the-code",
                "redirect_uri": "http://127.0.0.1:52847/callback",
            },
        )
        assert completed.status_code == 200
        assert completed.json()["connected"] is True
        assert completed.json()["connection_method"] == "oauth"
        assert completed.json()["needs_reconnect"] is False

        status = client.get("/api/v1/planning-center/status")
        assert status.status_code == 200
        assert status.json()["connection_method"] == "oauth"
        assert status.json()["oauth_connected"] is True

    # Secrets never leave through the API surface.
    assert "access-1" not in "\n".join((started.text, completed.text, status.text))
    assert store.secret is not None
    assert json.loads(store.secret)["refresh_token"] == "refresh-1"


def test_a_mismatched_callback_state_is_rejected_with_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryCredentialStore()
    application = app_with(monkeypatch, control_plane_handler, store)
    with TestClient(application) as client:
        client.post("/api/v1/planning-center/oauth/start")
        response = client.post(
            "/api/v1/planning-center/oauth/callback",
            json={
                "state": "not-the-state",
                "code": "the-code",
                "redirect_uri": "http://127.0.0.1:52847/callback",
            },
        )
    assert response.status_code == 401
    assert store.secret is None


def test_a_transient_control_plane_failure_returns_503_not_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blip must not be reported as "your connection is dead"."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json=FLOW)
        return httpx.Response(503, json={"error": "planning center unavailable"})

    store = MemoryCredentialStore()
    application = app_with(monkeypatch, handler, store)
    with TestClient(application) as client:
        state = client.post("/api/v1/planning-center/oauth/start").json()["state"]
        response = client.post(
            "/api/v1/planning-center/oauth/callback",
            json={
                "state": state,
                "code": "the-code",
                "redirect_uri": "http://127.0.0.1:52847/callback",
            },
        )
    assert response.status_code == 503


def test_disconnect_restores_the_manual_path_without_touching_the_pat_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryCredentialStore(
        OAuthTokens(
            access_token="a", refresh_token="r", expires_at=time.time() + 7200
        ).model_dump_json()
    )
    application = app_with(monkeypatch, control_plane_handler, store)
    with TestClient(application) as client:
        response = client.post("/api/v1/planning-center/oauth/disconnect")
        status = client.get("/api/v1/planning-center/oauth/status")
    assert response.status_code == 200
    assert response.json()["connection_method"] == "manual"
    assert response.json()["connected"] is False
    assert status.json()["connection_method"] == "manual"
    assert store.secret is None


def test_sign_in_is_unavailable_without_a_configured_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STAGEPILOT_PCO_CLIENT_ID", raising=False)
    application = create_app(Settings(), oauth_credential_store=MemoryCredentialStore())
    with TestClient(application) as client:
        response = client.post("/api/v1/planning-center/oauth/start")
        status = client.get("/api/v1/planning-center/oauth/status")
    assert response.status_code == 503
    assert status.status_code == 200
    assert status.json()["connected"] is False


def test_existing_manual_installations_report_the_manual_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additive: nothing changes for a PAT-connected installation."""

    monkeypatch.delenv("STAGEPILOT_PCO_CLIENT_ID", raising=False)
    application = create_app(Settings(), oauth_credential_store=MemoryCredentialStore())
    with TestClient(application) as client:
        status = client.get("/api/v1/planning-center/status")
    assert status.status_code == 200
    assert status.json()["connection_method"] == "manual"
    assert status.json()["oauth_connected"] is False
    assert status.json()["oauth_needs_reconnect"] is False
