from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from stagepilot.remote_bootstrap import DesktopBootstrapStore
from stagepilot.remote_desktop import DesktopRemoteManager
from stagepilot.remote_files import read_desired
from stagepilot.remote_provider import ProviderError
from stagepilot.services.remote_auth import RemoteRole, RemoteStore

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


def verified_store(path: Path, credentials: MemoryCredentials) -> DesktopBootstrapStore:
    return DesktopBootstrapStore(
        path,
        credentials,
        trusted_origins=TEST_ORIGINS,
    )


def bundle_payload(
    installation_id: str = "a" * 32,
) -> dict[str, object]:
    return {
        "installationId": installation_id,
        "hostname": f"sp-{installation_id}.remote.example.com",
        "installationCredential": f"spi_{installation_id}.{'s' * 43}",
    }


class FakeControlPlane:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.generation = ""
        self.revoked = False
        self.reject_credential = False
        self.fail_revoke = False
        self.fail_reenroll = False
        self.reenrolled = False
        self.reactivated = False
        self.offline = False
        self.enrollment_nonce = ""
        # Models the real control plane's per-nonce idempotency: the same
        # enrollment nonce always maps back to the same installation, and a
        # revoked installation reactivated via a replayed nonce comes back
        # with a fresh credential (a new "generation" of the same identity).
        self._nonce_to_id: dict[str, str] = {}
        self._credential_generation = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("test outage")
        if request.url.path.endswith("/v1/installations/enroll"):
            nonce = str(json.loads(request.content)["nonce"])
            self.enrollment_nonce = nonce
            installation_id = self._nonce_to_id.get(nonce)
            if installation_id is None:
                if not self._nonce_to_id:
                    # First-ever enrollment in this fake uses the fixture's
                    # seeded installation id so existing assertions keep
                    # matching a known value.
                    installation_id = str(self.payload["installationId"])
                else:
                    # A genuinely new/unseen nonce (e.g. after a local
                    # identity discard for "Regenerate Remote link") gets a
                    # brand-new installation id from the control plane, not
                    # a reuse of a previous installation's identity.
                    installation_id = uuid4().hex
                self._nonce_to_id[nonce] = installation_id
            elif self.revoked:
                # Reactivation of a previously revoked installation: same
                # durable id/hostname, brand-new credential.
                self.revoked = False
                self._credential_generation += 1
            credential = f"spi_{installation_id}." + "s" * 43
            if self._credential_generation:
                credential = f"spi_{installation_id}." + f"g{self._credential_generation}".ljust(
                    43, "s"
                )
            self.payload = {
                "installationId": installation_id,
                "hostname": f"sp-{installation_id}.remote.example.com",
                "installationCredential": credential,
            }
            return httpx.Response(201, json=self.payload)
        if self.reject_credential:
            return httpx.Response(401, json={"error": "unauthorized"})
        expected = "Bear" + f"er {self.payload['installationCredential']}"
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.url.path.endswith("/provision"):
            body: dict[str, Any] = json.loads(request.content)
            self.generation = str(body["generation"])
            return httpx.Response(
                200,
                json={
                    "installationId": self.payload["installationId"],
                    "hostname": self.payload["hostname"],
                    "generation": self.generation,
                    "phase": "provisioned",
                    "tunnelToken": "desktop-installation-cloudflared-token",
                },
            )
        if request.url.path.endswith("/reconcile"):
            return httpx.Response(
                200,
                json={
                    "installationId": self.payload["installationId"],
                    "hostname": self.payload["hostname"],
                    "generation": self.generation,
                    "phase": "provisioned",
                    "tunnelToken": "desktop-installation-cloudflared-token",
                },
            )
        if request.url.path.endswith("/revoke"):
            if self.fail_revoke:
                raise httpx.ConnectError("test outage")
            self.revoked = True
            self.generation = ""
            return httpx.Response(
                200,
                json={
                    "installationId": self.payload["installationId"],
                    "hostname": self.payload["hostname"],
                    "phase": "disabled",
                    "revoked": True,
                },
            )
        if request.url.path.endswith("/reenroll"):
            if self.fail_reenroll:
                raise httpx.ConnectError("test outage")
            self.reenrolled = True
            new_id = uuid4().hex
            self.payload = {
                "installationId": new_id,
                "hostname": f"sp-{new_id}.remote.example.com",
                "installationCredential": f"spi_{new_id}." + "r" * 43,
            }
            self.revoked = False
            self.generation = ""
            return httpx.Response(201, json=self.payload)
        if request.url.path.endswith("/reactivate"):
            self.reactivated = True
            self._credential_generation += 1
            suffix = f"a{self._credential_generation}".ljust(43, "a")
            credential = f"spi_{self.payload['installationId']}." + suffix
            self.payload = {**self.payload, "installationCredential": credential}
            self.revoked = False
            self.generation = ""
            return httpx.Response(200, json=self.payload)
        raise AssertionError(f"unexpected route {request.url.path}")


def manager_fixture(
    tmp_path: Path,
) -> tuple[DesktopRemoteManager, MemoryCredentials, FakeControlPlane, dict[str, object]]:
    payload = bundle_payload()
    credentials = MemoryCredentials()
    store = verified_store(tmp_path / "remote/bootstrap.json", credentials)
    fake = FakeControlPlane(payload)
    store.ensure_enrolled(
        control_plane_origin="https://control.example.com",
        transport=httpx.MockTransport(fake),
    )
    binary = tmp_path / "resources/cloudflared"
    binary.parent.mkdir()
    binary.write_bytes(b"test binary")
    manager = DesktopRemoteManager(
        tmp_path / "remote",
        binary,
        bootstrap_store=store,
        transport=httpx.MockTransport(fake),
        control_plane_origin="https://control.example.com",
    )
    return manager, credentials, fake, payload


def test_first_enable_transparently_enrolls_and_keeps_credential_native(tmp_path: Path) -> None:
    payload = bundle_payload()
    credentials = MemoryCredentials()
    store = DesktopBootstrapStore(
        tmp_path / "remote/bootstrap.json",
        credentials,
        trusted_origins=TEST_ORIGINS,
    )
    binary = tmp_path / "resources/cloudflared"
    binary.parent.mkdir()
    binary.write_bytes(b"test binary")
    fake = FakeControlPlane(payload)
    manager = DesktopRemoteManager(
        tmp_path / "remote",
        binary,
        bootstrap_store=store,
        transport=httpx.MockTransport(fake),
        control_plane_origin="https://control.example.com",
    )

    enabled = manager.enable()

    assert enabled["provisioned"] is True
    active = store.state().active
    assert active is not None
    assert active.installation_id == payload["installationId"]
    assert credentials.get(active.installation_id) == payload["installationCredential"]
    serialized = store.path.read_text(encoding="utf-8")
    assert str(payload["installationCredential"]) not in serialized
    # The enrollment nonce is the durable installation identity and is
    # deliberately retained (not cleared) after a successful enrollment so
    # that a later disable -> enable cycle can replay it and reprovision the
    # SAME hostname instead of minting a brand-new installation.
    assert store.state().enrollment_nonce == fake.enrollment_nonce
    assert active.bundle_id == active.installation_id


def test_new_16_char_installation_id_validates_alongside_legacy_32_char(tmp_path: Path) -> None:
    # New enrollments mint a 16-hex-char installation id (sp-<16 hex chars>
    # hostname); existing 32-char installations must keep validating too.
    payload = bundle_payload("c" * 16)
    credentials = MemoryCredentials()
    store = DesktopBootstrapStore(
        tmp_path / "remote/bootstrap.json",
        credentials,
        trusted_origins=TEST_ORIGINS,
    )
    binary = tmp_path / "resources/cloudflared"
    binary.parent.mkdir()
    binary.write_bytes(b"test binary")
    fake = FakeControlPlane(payload)
    manager = DesktopRemoteManager(
        tmp_path / "remote",
        binary,
        bootstrap_store=store,
        transport=httpx.MockTransport(fake),
        control_plane_origin="https://control.example.com",
    )

    enabled = manager.enable()

    assert enabled["provisioned"] is True
    active = store.state().active
    assert active is not None
    assert active.installation_id == "c" * 16
    assert active.hostname == f"sp-{'c' * 16}.remote.example.com"
    assert credentials.get(active.installation_id) == payload["installationCredential"]


def test_new_8_char_installation_id_validates_alongside_legacy_16_and_32_char(
    tmp_path: Path,
) -> None:
    # Newest enrollments mint an 8-hex-char installation id (sp-<8 hex
    # chars> hostname); both older 16-char and 32-char installations must
    # keep validating unchanged.
    payload = bundle_payload("d" * 8)
    credentials = MemoryCredentials()
    store = DesktopBootstrapStore(
        tmp_path / "remote/bootstrap.json",
        credentials,
        trusted_origins=TEST_ORIGINS,
    )
    binary = tmp_path / "resources/cloudflared"
    binary.parent.mkdir()
    binary.write_bytes(b"test binary")
    fake = FakeControlPlane(payload)
    manager = DesktopRemoteManager(
        tmp_path / "remote",
        binary,
        bootstrap_store=store,
        transport=httpx.MockTransport(fake),
        control_plane_origin="https://control.example.com",
    )

    enabled = manager.enable()

    assert enabled["provisioned"] is True
    active = store.state().active
    assert active is not None
    assert active.installation_id == "d" * 8
    assert active.hostname == f"sp-{'d' * 8}.remote.example.com"
    assert credentials.get(active.installation_id) == payload["installationCredential"]


def test_enrollment_honors_retry_after_with_backoff_and_jitter(tmp_path: Path) -> None:
    payload = bundle_payload()
    credentials = MemoryCredentials()
    store = DesktopBootstrapStore(
        tmp_path / "remote/bootstrap.json",
        credentials,
        trusted_origins=TEST_ORIGINS,
    )
    attempts = 0
    sleeps: list[float] = []

    def enrollment(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "limited"})
        return httpx.Response(201, json=payload)

    metadata = store.ensure_enrolled(
        control_plane_origin="https://control.example.com",
        transport=httpx.MockTransport(enrollment),
        sleep=sleeps.append,
        random_value=lambda: 0.5,
    )

    assert metadata.installation_id == payload["installationId"]
    assert attempts == 3
    assert sleeps == [2.0, 2.0]


def test_desktop_disable_then_enable_reprovisions_same_hostname_new_generation(
    tmp_path: Path,
) -> None:
    manager, credentials, fake, payload = manager_fixture(tmp_path)

    enabled = manager.enable()
    desired = read_desired(manager.desired_path)
    active = manager.bootstrap.state().active
    assert enabled["provisioned"] is True
    assert desired.enabled and desired.generation == manager.feature.intent().generation
    assert active is not None
    assert manager.bootstrap.credential(active) == payload["installationCredential"]
    original_installation_id = active.installation_id
    original_hostname = active.hostname
    original_credential = manager.bootstrap.credential(active)

    restarted = DesktopRemoteManager(
        manager.root,
        manager.cloudflared_binary,
        bootstrap_store=manager.bootstrap,
        transport=httpx.MockTransport(fake),
        control_plane_origin="https://control.example.com",
    )
    restarted.reconcile_control()
    assert restarted.feature.intent().enabled
    assert read_desired(restarted.desired_path).enabled

    disabled = restarted.disable()
    assert fake.revoked
    assert disabled["provisioned"] is False
    assert not read_desired(restarted.desired_path).enabled
    assert not restarted.installation_dir.joinpath("connector.token").exists()
    # The installation credential is deliberately KEPT locally (not
    # deleted) after a normal disable/revoke: it lets a genuine future
    # re-enable authenticate via `reactivate()` instead of the anonymous,
    # per-network-quota-limited enrollment route. It only stops being
    # presentable once the control plane's credential generation moves on
    # (e.g. after an actual reactivation or an admin-forced recovery).
    assert credentials.get(original_installation_id) is not None
    # Disable genuinely revokes the credential/tunnel, but the durable
    # installation identity (enrollment nonce) must survive locally so
    # re-enable can reprovision the SAME hostname.
    assert restarted.bootstrap.state().enrollment_nonce == fake.enrollment_nonce
    assert restarted.bootstrap.state().active is None
    assert restarted.bootstrap.state().retired is not None
    assert restarted.bootstrap.state().retired.installation_id == original_installation_id  # type: ignore[union-attr]

    reenabled = restarted.enable()
    assert reenabled["provisioned"] is True
    active_after = restarted.bootstrap.state().active
    assert active_after is not None
    # SAME hostname / installation id ...
    assert active_after.installation_id == original_installation_id
    assert active_after.hostname == original_hostname
    # ... but a NEW generation (fresh credential) was issued.
    new_credential = restarted.bootstrap.credential(active_after)
    assert new_credential != original_credential
    assert credentials.get(original_installation_id) == new_credential

    # A different owner replaying a different (unrelated) nonce must never
    # collide with this reactivated installation.
    other_payload = bundle_payload("f" * 32)
    other_credentials = MemoryCredentials()
    other_store = verified_store(tmp_path / "other/bootstrap.json", other_credentials)
    other_fake = FakeControlPlane(other_payload)
    other_manager = DesktopRemoteManager(
        tmp_path / "other",
        manager.cloudflared_binary,
        bootstrap_store=other_store,
        transport=httpx.MockTransport(other_fake),
        control_plane_origin="https://control.example.com",
    )
    other_enabled = other_manager.enable()
    assert other_enabled["provisioned"] is True
    other_active = other_store.state().active
    assert other_active is not None
    assert other_active.installation_id != original_installation_id


def test_revoke_failure_closes_local_access_and_retries_after_restart(tmp_path: Path) -> None:
    manager, credentials, fake, payload = manager_fixture(tmp_path)
    manager.enable()
    fake.fail_revoke = True

    with pytest.raises(ProviderError):
        manager.disable()

    assert not read_desired(manager.desired_path).enabled
    assert credentials.get(str(payload["installationId"])) is not None
    fake.fail_revoke = False
    restarted = DesktopRemoteManager(
        manager.root,
        manager.cloudflared_binary,
        bootstrap_store=manager.bootstrap,
        transport=httpx.MockTransport(fake),
    )
    restarted.reconcile_control()
    assert fake.revoked
    assert restarted.bootstrap.state().active is None
    # See test_desktop_disable_then_enable_reprovisions_same_hostname_new_generation:
    # the credential is deliberately retained locally after revoke, to
    # support authenticated reactivation instead of anonymous re-enrollment.
    assert credentials.get(str(payload["installationId"])) is not None


@pytest.mark.asyncio
async def test_startup_reconciliation_retries_after_transient_outage(tmp_path: Path) -> None:
    manager, _, fake, _ = manager_fixture(tmp_path)
    manager.enable()
    restarted = DesktopRemoteManager(
        manager.root,
        manager.cloudflared_binary,
        bootstrap_store=manager.bootstrap,
        transport=httpx.MockTransport(fake),
    )
    fake.offline = True
    stop = asyncio.Event()
    task = asyncio.create_task(restarted.run(stop))
    try:
        await asyncio.sleep(0.2)
        assert restarted._connector_token is None
        fake.offline = False
        for _ in range(100):
            if restarted._connector_token is not None:
                break
            await asyncio.sleep(0.05)
        assert restarted._connector_token == "desktop-installation-cloudflared-token"
    finally:
        stop.set()
        await task


def test_revoked_or_expired_installation_is_retired_during_reconcile(tmp_path: Path) -> None:
    manager, credentials, fake, payload = manager_fixture(tmp_path)
    revoked_sessions: list[bool] = []
    manager.revoke_sessions = lambda: revoked_sessions.append(True)
    manager.enable()
    fake.reject_credential = True

    manager.reconcile_control()

    assert manager.bootstrap.state().active is None
    assert credentials.get(str(payload["installationId"])) is None
    assert not read_desired(manager.desired_path).enabled
    assert revoked_sessions == [True]
    assert manager._connector_token is None


def test_missing_native_credential_fails_closed_without_reenrollment(tmp_path: Path) -> None:
    manager, credentials, _, payload = manager_fixture(tmp_path)
    credentials.delete(str(payload["installationId"]))

    status = manager.status()

    assert status["provisioned"] is True
    assert status["credential_available"] is False
    with pytest.raises(ProviderError, match="credential is unavailable"):
        manager.enable()
    assert manager.bootstrap.state().active is not None


def test_regenerate_revokes_old_identity_and_provisions_a_new_hostname(tmp_path: Path) -> None:
    manager, credentials, fake, payload = manager_fixture(tmp_path)
    manager.enable()
    original_installation_id = str(payload["installationId"])
    original_hostname = manager.bootstrap.state().active.hostname  # type: ignore[union-attr]

    result = manager.regenerate()

    # Old identity's tunnel/credential were revoked (not merely disabled)
    # via the authenticated re-enrollment path, not the anonymous quota.
    assert fake.reenrolled
    assert credentials.get(original_installation_id) is None

    # A genuinely new installation/hostname was provisioned.
    new_active = manager.bootstrap.state().active
    assert new_active is not None
    assert new_active.installation_id != original_installation_id
    assert new_active.hostname != original_hostname
    assert result["provisioned"] is True
    assert result["enabled"] is True

    # Regeneration is idempotent / restart-safe: calling again does not
    # error and produces another fresh identity without orphaning the
    # previous one either.
    second_installation_id = new_active.installation_id
    second_hostname = new_active.hostname
    manager.regenerate()
    third_active = manager.bootstrap.state().active
    assert third_active is not None
    assert third_active.installation_id != second_installation_id
    assert third_active.hostname != second_hostname
    assert credentials.get(second_installation_id) is None


def test_regenerate_cancel_path_makes_no_changes(tmp_path: Path) -> None:
    """The UI's Cancel button must never call regenerate(); this documents
    that plain status reads alone cannot mutate identity state."""

    manager, credentials, fake, payload = manager_fixture(tmp_path)
    manager.enable()
    before = manager.status()
    before_active = manager.bootstrap.state().active

    for _ in range(3):
        manager.status()

    after = manager.status()
    after_active = manager.bootstrap.state().active
    assert before == after
    assert before_active == after_active
    assert not fake.revoked
    assert credentials.get(str(payload["installationId"])) is not None


def test_regenerate_preserves_remote_users_in_the_identity_store(tmp_path: Path) -> None:
    """Remote Operators/Viewers live in the separate local identity SQLite
    store (services.remote_auth), not in the bootstrap/tunnel identity this
    module manages, so regenerating the installation hostname/credential
    must never touch or drop them."""

    manager, _, _, _ = manager_fixture(tmp_path)
    manager.enable()

    store = RemoteStore(tmp_path / "identity.sqlite3")
    store.bootstrap("operator@example.test", "a-strong-password-123")
    store.create_user("viewer@example.test", "another-strong-password-1", RemoteRole.VIEWER)
    before_users = store.users()
    assert len(before_users) == 2

    manager.regenerate()

    after_users = store.users()
    assert after_users == before_users


def test_regenerate_repeatedly_never_hits_anonymous_enrollment_endpoint(
    tmp_path: Path,
) -> None:
    """Regression test for the reported bug: regenerating several times in
    a row used to exhaust the control plane's per-network anonymous
    enrollment quota (ENROLLMENTS_PER_SOURCE) because it went back through
    the nonce-based /v1/installations/enroll route, leaving Remote broken
    with no recourse. Regenerate must instead use the authenticated
    reenroll path and never touch /enroll again after the first bootstrap."""

    manager, _credentials, fake, _payload = manager_fixture(tmp_path)
    manager.enable()
    enroll_calls = 0
    original_call = fake.__call__

    def counting_call(request: httpx.Request) -> httpx.Response:
        nonlocal enroll_calls
        if request.url.path.endswith("/v1/installations/enroll"):
            enroll_calls += 1
        return original_call(request)

    fake.__call__ = counting_call  # type: ignore[method-assign]

    seen_ids = {manager.bootstrap.state().active.installation_id}  # type: ignore[union-attr]
    for _ in range(5):
        result = manager.regenerate()
        assert result["enabled"] is True
        active = manager.bootstrap.state().active
        assert active is not None
        assert active.installation_id not in seen_ids
        seen_ids.add(active.installation_id)

    # Only the very first `enable()` call above used /enroll; none of the
    # five subsequent regenerations should have -- they must all be billed
    # against the authenticated per-installation mutation budget instead of
    # the anonymous-enrollment abuse quota that a real network could
    # exhaust after just 3 regenerations/24h.
    assert enroll_calls == 0


def test_enable_after_disable_reactivates_via_authenticated_path_not_anonymous_enroll(
    tmp_path: Path,
) -> None:
    """Regression test for the reported bug: a machine that already
    disabled/revoked Remote once must be able to Enable again via the
    authenticated `reactivate()` path, never falling back to the anonymous,
    per-network-quota-limited `/v1/installations/enroll` route -- even if
    that anonymous quota is fully exhausted for this network."""

    manager, credentials, fake, payload = manager_fixture(tmp_path)
    manager.enable()
    original_installation_id = str(payload["installationId"])
    manager.disable()
    assert manager.bootstrap.state().active is None
    assert manager.bootstrap.state().retired is not None

    enroll_calls = 0

    def counting_call(request: httpx.Request) -> httpx.Response:
        nonlocal enroll_calls
        if request.url.path.endswith("/v1/installations/enroll"):
            enroll_calls += 1
        return fake(request)

    manager.transport = httpx.MockTransport(counting_call)

    reenabled = manager.enable()

    assert fake.reactivated
    assert enroll_calls == 0
    assert reenabled["provisioned"] is True
    active = manager.bootstrap.state().active
    assert active is not None
    # SAME durable installation id/hostname -- a genuine re-enable on a
    # known machine, not a brand-new anonymous installation.
    assert active.installation_id == original_installation_id
    assert manager.bootstrap.state().retired is None
    assert credentials.get(original_installation_id) is not None


def test_genuinely_first_time_installation_still_uses_anonymous_enroll(
    tmp_path: Path,
) -> None:
    """A machine with no prior local installation identity (no `retired`,
    no `active`) has no installation credential to present, so it must
    still go through the anonymous, quota-limited enrollment route -- the
    reactivation fix must never weaken this guardrail."""

    payload = bundle_payload()
    credentials = MemoryCredentials()
    store = DesktopBootstrapStore(
        tmp_path / "remote/bootstrap.json",
        credentials,
        trusted_origins=TEST_ORIGINS,
    )
    binary = tmp_path / "resources/cloudflared"
    binary.parent.mkdir()
    binary.write_bytes(b"test binary")
    fake = FakeControlPlane(payload)
    enroll_calls = 0

    def counting_call(request: httpx.Request) -> httpx.Response:
        nonlocal enroll_calls
        if request.url.path.endswith("/v1/installations/enroll"):
            enroll_calls += 1
        return fake(request)

    manager = DesktopRemoteManager(
        tmp_path / "remote",
        binary,
        bootstrap_store=store,
        transport=httpx.MockTransport(counting_call),
        control_plane_origin="https://control.example.com",
    )

    enabled = manager.enable()

    assert enabled["provisioned"] is True
    assert enroll_calls == 1
    assert not fake.reactivated


def test_reactivate_surfaces_quota_exceeded_as_specific_provider_error(
    tmp_path: Path,
) -> None:
    """A 429 from the authenticated reactivate route must map to a specific,
    end-user-readable ProviderError message distinct from the generic
    "unavailable" case, and must never silently drop back to anonymous
    enrollment."""

    manager, _credentials, _fake, _payload = manager_fixture(tmp_path)
    manager.enable()
    manager.disable()

    def limited(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reactivate"):
            return httpx.Response(429, json={"error": "limited"})
        raise AssertionError(f"unexpected route {request.url.path}")

    manager.bootstrap = DesktopBootstrapStore(
        manager.bootstrap.path,
        manager.bootstrap.credentials,
        trusted_origins=TEST_ORIGINS,
    )

    with pytest.raises(ProviderError, match="enrollment limit"):
        manager.bootstrap.ensure_enrolled(
            control_plane_origin="https://control.example.com",
            transport=httpx.MockTransport(limited),
        )
