"""Planning Center OAuth: PKCE, state/CSRF, storage, refresh, failure classes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from stagepilot.core.settings import MemoryCredentialStore
from stagepilot.planning_center_oauth import (
    OAUTH_SCOPE,
    REFRESH_MARGIN_SECONDS,
    ControlPlaneOAuthClient,
    OAuthTokens,
    OAuthTokenStore,
    PlanningCenterOAuthError,
    PlanningCenterOAuthService,
    build_authorize_url,
    build_state,
    code_challenge_for,
    flow_id_from_state,
    generate_code_verifier,
    proactive_refresh_loop,
)

CLIENT_ID = "test-client-id"


def token_payload(access: str, refresh: str, expires_in: int = 7200) -> dict[str, object]:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "scope": OAUTH_SCOPE,
        "token_type": "Bearer",
    }


def control_plane(handler: object) -> ControlPlaneOAuthClient:
    return ControlPlaneOAuthClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def service(handler: object) -> tuple[PlanningCenterOAuthService, OAuthTokenStore]:
    store = OAuthTokenStore(MemoryCredentialStore())
    return (
        PlanningCenterOAuthService(
            client_id=CLIENT_ID, tokens=store, control_plane=control_plane(handler)
        ),
        store,
    )


def test_pkce_verifier_and_s256_challenge_match_rfc7636() -> None:
    verifier = generate_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert code_challenge_for(verifier) == expected


def test_state_embeds_the_flow_id_and_is_unguessable() -> None:
    first = build_state("abcdef01")
    second = build_state("abcdef01")
    assert flow_id_from_state(first) == "abcdef01"
    assert first != second
    assert flow_id_from_state("no-separator") is None


def test_authorize_url_requests_only_the_services_scope_and_s256() -> None:
    url = build_authorize_url(CLIENT_ID, "state-1", "challenge-1")
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).netloc == "api.planningcenteronline.com"
    assert query["scope"] == ["services"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    # The desktop side appends redirect_uri: only it knows which of the
    # four pre-registered loopback ports was free.
    assert "redirect_uri" not in query


@pytest.mark.asyncio
async def test_sign_in_exchanges_the_code_and_stores_the_token_blob() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"path": request.url.path, **body})
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "ticket-1"})
        return httpx.Response(200, json=token_payload("access-1", "refresh-1"))

    oauth, store = service(handler)
    url, state = await oauth.start()
    assert flow_id_from_state(state) == "aabbccdd"
    assert "code_challenge=" in url

    tokens = await oauth.complete(
        state=state, code="the-code", redirect_uri="http://127.0.0.1:52847/callback"
    )
    assert tokens.access_token == "access-1"
    stored = store.load()
    assert stored is not None
    assert stored.refresh_token == "refresh-1"
    assert stored.needs_reconnect is False
    assert stored.expires_in() > 7000
    exchange = seen[-1]
    assert exchange["ticket"] == "ticket-1"
    assert exchange["redirect_uri"] == "http://127.0.0.1:52847/callback"
    assert isinstance(exchange["code_verifier"], str)


@pytest.mark.asyncio
async def test_a_mismatched_state_is_rejected_before_any_token_exchange() -> None:
    exchanges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "ticket-1"})
        exchanges.append(request.url.path)
        return httpx.Response(200, json=token_payload("access-1", "refresh-1"))

    oauth, store = service(handler)
    await oauth.start()
    with pytest.raises(PlanningCenterOAuthError) as error:
        await oauth.complete(
            state="attacker-state", code="c", redirect_uri="http://127.0.0.1:52847/callback"
        )
    assert error.value.permanent is True
    assert exchanges == []
    assert store.load() is None


@pytest.mark.asyncio
async def test_refresh_overwrites_the_stored_refresh_token_every_time() -> None:
    presented: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "ticket-1"})
        presented.append(json.loads(request.content)["refresh_token"])
        index = len(presented)
        return httpx.Response(200, json=token_payload(f"access-{index}", f"refresh-{index}"))

    oauth, store = service(handler)
    store.save(
        OAuthTokens(access_token="access-0", refresh_token="refresh-0", expires_at=time.time() + 60)
    )
    await oauth.refresh_now()
    await oauth.refresh_now()
    # Planning Center invalidates the previous refresh token on every
    # refresh, so each call must present the newest one, never the original.
    assert presented == ["refresh-0", "refresh-1"]
    stored = store.load()
    assert stored is not None
    assert (stored.access_token, stored.refresh_token) == ("access-2", "refresh-2")


@pytest.mark.asyncio
async def test_a_revoked_refresh_token_sets_the_explicit_needs_reconnect_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "ticket-1"})
        return httpx.Response(400, json={"error": "invalid_grant"})

    oauth, store = service(handler)
    store.save(OAuthTokens(access_token="a", refresh_token="r", expires_at=time.time() + 60))
    with pytest.raises(PlanningCenterOAuthError) as error:
        await oauth.refresh_now()
    assert error.value.permanent is True
    stored = store.load()
    assert stored is not None and stored.needs_reconnect is True
    assert oauth.status(connection_method="oauth").needs_reconnect is True
    assert oauth.status(connection_method="oauth").connected is False


@pytest.mark.asyncio
async def test_a_transient_failure_never_marks_the_connection_as_needing_reconnect() -> None:
    """The Remote Access lesson (t_460cf8dd/t_7bdec765): a blip is not a revocation."""

    for response in (
        httpx.Response(503, json={"error": "planning center unavailable"}),
        httpx.Response(429, json={"error": "oauth request rate limited"}),
    ):

        def handler(request: httpx.Request, response: httpx.Response = response) -> httpx.Response:
            if request.url.path.endswith("/flow"):
                return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "t"})
            return response

        oauth, store = service(handler)
        store.save(OAuthTokens(access_token="a", refresh_token="r", expires_at=time.time() + 60))
        with pytest.raises(PlanningCenterOAuthError) as error:
            await oauth.refresh_now()
        assert error.value.permanent is False
        stored = store.load()
        assert stored is not None and stored.needs_reconnect is False
        assert stored.refresh_token == "r"


@pytest.mark.asyncio
async def test_network_failure_is_transient() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    oauth, store = service(handler)
    store.save(OAuthTokens(access_token="a", refresh_token="r", expires_at=time.time() + 60))
    with pytest.raises(PlanningCenterOAuthError) as error:
        await oauth.refresh_now()
    assert error.value.permanent is False
    stored = store.load()
    assert stored is not None and stored.needs_reconnect is False


def test_refresh_is_due_ten_to_fifteen_minutes_before_the_two_hour_expiry() -> None:
    fresh = OAuthTokens(access_token="a", refresh_token="r", expires_at=time.time() + 7200)
    due = OAuthTokens(
        access_token="a",
        refresh_token="r",
        expires_at=time.time() + REFRESH_MARGIN_SECONDS - 30,
    )
    assert 600 <= REFRESH_MARGIN_SECONDS <= 900
    assert fresh.needs_refresh() is False
    assert due.needs_refresh() is True


@pytest.mark.asyncio
async def test_valid_access_token_refreshes_when_due_and_refuses_a_dead_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "t"})
        return httpx.Response(200, json=token_payload("fresh-access", "fresh-refresh"))

    oauth, store = service(handler)
    store.save(OAuthTokens(access_token="stale", refresh_token="r", expires_at=time.time() + 60))
    assert await oauth.valid_access_token() == "fresh-access"

    store.save(
        OAuthTokens(
            access_token="a",
            refresh_token="r",
            expires_at=time.time() + 7200,
            needs_reconnect=True,
        )
    )
    with pytest.raises(PlanningCenterOAuthError) as error:
        await oauth.valid_access_token()
    assert error.value.permanent is True


@pytest.mark.asyncio
async def test_proactive_loop_refreshes_a_due_token_then_stops_cleanly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "t"})
        return httpx.Response(200, json=token_payload("looped-access", "looped-refresh"))

    oauth, store = service(handler)
    store.save(OAuthTokens(access_token="a", refresh_token="r", expires_at=time.time() + 60))
    stop = asyncio.Event()
    task = asyncio.create_task(proactive_refresh_loop(oauth, stop, poll_seconds=0.01))
    for _ in range(200):
        await asyncio.sleep(0.01)
        current = store.load()
        if current is not None and current.access_token == "looped-access":
            break
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    stored = store.load()
    assert stored is not None and stored.access_token == "looped-access"


@pytest.mark.asyncio
async def test_proactive_loop_survives_a_permanent_failure_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flow"):
            return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "t"})
        return httpx.Response(400, json={"error": "invalid_grant"})

    oauth, store = service(handler)
    store.save(OAuthTokens(access_token="a", refresh_token="r", expires_at=time.time() + 60))
    stop = asyncio.Event()
    task = asyncio.create_task(proactive_refresh_loop(oauth, stop, poll_seconds=0.01))
    for _ in range(200):
        await asyncio.sleep(0.01)
        current = store.load()
        if current is not None and current.needs_reconnect:
            break
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert task.exception() is None
    stored = store.load()
    assert stored is not None and stored.needs_reconnect is True


def test_an_untrusted_control_plane_origin_is_refused() -> None:
    with pytest.raises(PlanningCenterOAuthError):
        ControlPlaneOAuthClient(origin="https://example.com")


def test_a_corrupt_token_blob_reads_as_not_connected() -> None:
    credentials = MemoryCredentialStore("not json at all")
    store = OAuthTokenStore(credentials)
    assert store.load() is None


@pytest.mark.asyncio
async def test_disconnect_clears_the_stored_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"flow_id": "aabbccdd", "ticket": "t"})

    oauth, store = service(handler)
    store.save(OAuthTokens(access_token="a", refresh_token="r", expires_at=time.time() + 60))
    oauth.disconnect()
    assert store.load() is None
    assert oauth.status(connection_method="manual").connected is False
