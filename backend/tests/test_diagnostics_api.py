from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stagepilot.core.config import Settings
from stagepilot.main import create_app
from stagepilot.remote_bootstrap import DesktopBootstrapStore

SEND_URL = "/api/v1/diagnostics/send"


class _FakeBootstrap:
    def __init__(
        self,
        *,
        enrolled: bool = True,
        trusted: bool = True,
        raises: bool = False,
    ) -> None:
        self.trusted_origins = frozenset(
            {"https://control.example"} if trusted else {"https://other.example"}
        )
        self._raises = raises
        self._active = (
            SimpleNamespace(
                control_plane_origin="https://control.example",
                installation_id="abcd1234",
            )
            if enrolled
            else None
        )

    def state(self) -> object:
        if self._raises:
            raise RuntimeError("boom")
        return SimpleNamespace(active=self._active)

    def credential(self, metadata: object) -> str:
        return "spi_abcd1234." + "s" * 43


def _app_with_bootstrap(bootstrap: object | None) -> FastAPI:
    app = create_app(Settings())
    app.state.remote_manager = (
        SimpleNamespace(bootstrap=bootstrap) if bootstrap is not None else None
    )
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://stagepilot.local")


def test_bundle_too_large_returns_413() -> None:
    app = _app_with_bootstrap(cast(DesktopBootstrapStore, _FakeBootstrap()))
    with _client(app) as client:
        oversized = "x" * (2 * 1024 * 1024 + 1)
        response = client.post(SEND_URL, json={"bundle": oversized})
    assert response.status_code == 413


def test_no_remote_manager_returns_503() -> None:
    app = create_app(Settings())
    # No remote_manager attribute set at all (never enrolled / bootstrap not wired).
    with _client(app) as client:
        response = client.post(SEND_URL, json={"bundle": "log contents"})
    assert response.status_code == 503


def test_remote_manager_without_bootstrap_returns_503() -> None:
    app = _app_with_bootstrap(None)
    with _client(app) as client:
        response = client.post(SEND_URL, json={"bundle": "log contents"})
    assert response.status_code == 503


def test_bootstrap_state_raises_returns_503() -> None:
    app = _app_with_bootstrap(cast(DesktopBootstrapStore, _FakeBootstrap(raises=True)))
    with _client(app) as client:
        response = client.post(SEND_URL, json={"bundle": "log contents"})
    assert response.status_code == 503


def test_unenrolled_installation_returns_503() -> None:
    app = _app_with_bootstrap(cast(DesktopBootstrapStore, _FakeBootstrap(enrolled=False)))
    with _client(app) as client:
        response = client.post(SEND_URL, json={"bundle": "log contents"})
    assert response.status_code == 503


def test_untrusted_control_plane_origin_returns_503() -> None:
    app = _app_with_bootstrap(cast(DesktopBootstrapStore, _FakeBootstrap(trusted=False)))
    with _client(app) as client:
        response = client.post(SEND_URL, json={"bundle": "log contents"})
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("upstream_status", "expected_status"),
    [(201, 200), (429, 429), (500, 502)],
)
def test_forwarding_path_statuses(
    monkeypatch: pytest.MonkeyPatch, upstream_status: int, expected_status: int
) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(upstream_status, json={"ok": True})

    real_async_client = httpx.AsyncClient

    def fake_async_client(
        *,
        base_url: str = "",
        timeout: float | None = None,
        trust_env: bool = True,
        follow_redirects: bool = False,
        **_kwargs: object,
    ) -> httpx.AsyncClient:
        return real_async_client(
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
            follow_redirects=follow_redirects,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)

    app = _app_with_bootstrap(cast(DesktopBootstrapStore, _FakeBootstrap()))
    with _client(app) as client:
        response = client.post(SEND_URL, json={"bundle": "log contents"})

    assert response.status_code == expected_status
    assert seen["url"] == "https://control.example/v1/installations/abcd1234/diagnostics"
    assert seen["authorization"] == "Bearer spi_abcd1234." + "s" * 43
    assert seen["content_type"] == "application/octet-stream"
    assert seen["body"] == b"log contents"
    if expected_status == 200:
        assert response.json() == {"ok": True, "message": "Logs sent to the developer."}
