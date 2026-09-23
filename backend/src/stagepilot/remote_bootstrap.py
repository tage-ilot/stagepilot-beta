"""Native credential storage and transparent private-beta enrollment."""

from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from stagepilot.remote_files import atomic_write
from stagepilot.remote_provider import ProviderError

INSTALLATION_SCHEMA = "org.stagepilot.private-beta-installation"
DEFAULT_CONTROL_PLANE_ORIGIN = (
    "https://stagepilot-beta-control-plane.stagepilot-illuminary-beta.workers.dev"
)
DEFAULT_REMOTE_PORT = 18766
TRUSTED_CONTROL_PLANE_ORIGINS = frozenset(
    {"https://stagepilot-beta-control-plane.stagepilot-illuminary-beta.workers.dev"}
)
_HOSTNAME = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_INSTALLATION_ID = re.compile(r"^[a-f0-9]{8}$|^[a-f0-9]{16}$|^[a-f0-9]{32}$")
_CREDENTIAL = re.compile(r"^spi_([a-f0-9]{8}|[a-f0-9]{16}|[a-f0-9]{32})\.([A-Za-z0-9_-]{32,})$")


class RemoteCredentialStore(Protocol):
    def get(self, installation_id: str) -> str | None: ...

    def set(self, installation_id: str, credential: str) -> None: ...

    def delete(self, installation_id: str) -> None: ...


class NativeRemoteCredentialStore:
    """Use the authenticated Tauri broker for Credential Manager or Keychain."""

    def __init__(self, origin: str | None = None, authorization: str | None = None) -> None:
        self.origin = origin or os.environ.get("STAGEPILOT_CREDENTIAL_BROKER_ORIGIN", "")
        self.authorization = authorization or os.environ.get(
            "STAGEPILOT_CREDENTIAL_BROKER_TOKEN", ""
        )

    def _request(
        self, method: str, installation_id: str, credential: str | None = None
    ) -> httpx.Response:
        parsed = urlsplit(self.origin)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or not self.authorization
        ):
            raise ProviderError("The operating-system credential store is unavailable")
        try:
            return httpx.request(
                method,
                f"{self.origin}/v1/credentials/{installation_id}",
                headers={"Authorization": f"Bearer {self.authorization}"},
                content=credential or b"",
                timeout=3,
                trust_env=False,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("The operating-system credential store is unavailable") from exc

    def get(self, installation_id: str) -> str | None:
        response = self._request("GET", installation_id)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProviderError("The operating-system credential store is unavailable")
        return response.text

    def set(self, installation_id: str, credential: str) -> None:
        if self._request("PUT", installation_id, credential).status_code != 204:
            raise ProviderError("The installation credential could not be saved")

    def delete(self, installation_id: str) -> None:
        if self._request("DELETE", installation_id).status_code != 204:
            raise ProviderError("The installation credential could not be removed")


class BootstrapMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    schema_name: str = Field(alias="schema")
    version: int
    bundle_id: str = Field(alias="bundleId")
    control_plane_origin: str = Field(alias="controlPlaneOrigin")
    installation_id: Annotated[
        str, Field(pattern=r"^[a-f0-9]{8}$|^[a-f0-9]{16}$|^[a-f0-9]{32}$")
    ] = Field(alias="installationId")
    hostname: str
    remote_port: int = Field(alias="remotePort", ge=1024, le=65535)


class BootstrapState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    active: BootstrapMetadata | None = None
    # A disabled/revoked-but-known installation: kept (with its still-valid
    # installation credential) so a future genuine re-enable can prove
    # ownership via the authenticated reactivate path instead of burning the
    # anonymous per-network enrollment quota meant for first-time installs.
    retired: BootstrapMetadata | None = None
    enrollment_nonce: str | None = Field(default=None, alias="enrollmentNonce")
    legacy_consumed_bundle_ids: list[str] = Field(
        default_factory=list, alias="consumedBundleIds", exclude=True
    )


class DesktopBootstrapStore:
    def __init__(
        self,
        path: Path,
        credentials: RemoteCredentialStore | None = None,
        *,
        trusted_origins: frozenset[str] = TRUSTED_CONTROL_PLANE_ORIGINS,
    ) -> None:
        if not path.is_absolute():
            raise ValueError("Desktop bootstrap state path must be absolute")
        self.path = path
        self.credentials = credentials or NativeRemoteCredentialStore()
        self.trusted_origins = trusted_origins

    def ensure_enrolled(
        self,
        *,
        control_plane_origin: str = DEFAULT_CONTROL_PLANE_ORIGIN,
        remote_port: int = DEFAULT_REMOTE_PORT,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> BootstrapMetadata:
        """Transparently create and securely retain this installation identity."""

        if control_plane_origin not in self.trusted_origins:
            raise ProviderError("The enrollment service is not trusted")
        current = self.state()
        if current.active is not None:
            self.credential(current.active)
            return current.active
        if current.retired is not None:
            # A known prior installation (disabled or revoked) exists on
            # this machine: reactivate it via the authenticated path so we
            # never touch the anonymous per-network enrollment quota below.
            # A ProviderError here (e.g. genuinely exhausted mutation
            # budget, or the credential itself was hard-revoked) propagates
            # as-is; it must never silently fall through to a brand-new
            # anonymous enrollment, which would mint a second installation.
            return self.reactivate(current.retired, transport=transport)
        if current.enrollment_nonce is None:
            current.enrollment_nonce = str(UUID(bytes=os.urandom(16), version=4))
            self._write(current)
        response: httpx.Response | None = None
        with httpx.Client(
            base_url=control_plane_origin,
            timeout=20,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        ) as client:
            for attempt in range(3):
                try:
                    response = client.post(
                        "/v1/installations/enroll", json={"nonce": current.enrollment_nonce}
                    )
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        raise ProviderError(
                            "Could not reach the enrollment service. Check your Internet "
                            "connection and try again."
                        ) from exc
                else:
                    if response.status_code not in {429, 503} or attempt == 2:
                        break
                retry_after = 0
                if response is not None:
                    try:
                        retry_after = max(
                            0, min(300, int(response.headers.get("Retry-After", "0")))
                        )
                    except ValueError:
                        retry_after = 0
                sleep(max(float(retry_after), min(0.5 * (2**attempt) + random_value() * 0.25, 5.0)))
        if response is None:
            raise ProviderError(
                "Could not reach the enrollment service. Check your Internet connection and "
                "try again."
            )
        if response.status_code == 429:
            raise ProviderError(
                "This computer has reached its enrollment limit for now. Try again later."
            )
        if response.status_code not in {200, 201}:
            raise ProviderError("The enrollment service is unavailable; retry later")
        try:
            payload = response.json()
            installation_id = payload["installationId"]
            hostname = payload["hostname"]
            credential = payload["installationCredential"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("The enrollment response was invalid") from exc
        match = _CREDENTIAL.fullmatch(credential) if isinstance(credential, str) else None
        if (
            not isinstance(installation_id, str)
            or not _INSTALLATION_ID.fullmatch(installation_id)
            or match is None
            or match.group(1) != installation_id
            or not isinstance(hostname, str)
            or not _HOSTNAME.fullmatch(hostname)
            or hostname != f"sp-{installation_id}.{hostname.split('.', 1)[1]}"
        ):
            raise ProviderError("The enrollment response was not installation-bound")
        metadata = BootstrapMetadata.model_validate(
            {
                "schema": INSTALLATION_SCHEMA,
                "version": 1,
                "bundleId": installation_id,
                "controlPlaneOrigin": control_plane_origin,
                "installationId": installation_id,
                "hostname": hostname,
                "remotePort": remote_port,
            }
        )
        self.credentials.set(installation_id, credential)
        try:
            current.active = metadata
            # Deliberately retained (not cleared): this nonce is the durable
            # installation identity. The control plane treats a replayed
            # enrollment of this exact nonce as "the rightful owner is
            # re-enabling" and reprovisions the SAME hostname with a fresh
            # generation/credential instead of minting a new installation.
            # Clearing it here (as before) meant every disable->enable cycle
            # discarded the identity and got a brand-new "sp-<id>" hostname.
            self._write(current)
        except Exception:
            self.credentials.delete(installation_id)
            raise
        return metadata

    def state(self) -> BootstrapState:
        if not self.path.exists():
            return BootstrapState()
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 32_768:
                raise ValueError("Invalid bootstrap metadata file")
            return BootstrapState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProviderError("Bootstrap metadata is unavailable") from exc

    def credential(self, metadata: BootstrapMetadata) -> str:
        value = self.credentials.get(metadata.installation_id)
        match = _CREDENTIAL.fullmatch(value or "")
        if match is None or match.group(1) != metadata.installation_id:
            raise ProviderError("The installation credential is unavailable")
        return value or ""

    def reenroll(
        self,
        metadata: BootstrapMetadata,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> BootstrapMetadata:
        """Authenticated "Regenerate Remote link": mint a brand-new
        installation id/hostname while proving ownership of the current one
        via its installation credential.

        This deliberately does NOT go through `/v1/installations/enroll`
        (used only by unauthenticated, nonce-based first-time enrollment).
        That endpoint is guarded by a per-network anonymous-enrollment quota
        that protects the control plane from arbitrary Internet clients
        minting tunnels; a legitimate installation owner regenerating their
        link repeatedly would otherwise exhaust that quota and see Remote
        "break" with no recourse. The control plane atomically revokes the
        old installation and provisions the new one within a single
        authenticated request, so a failure here leaves the current
        identity/credential untouched and still usable.
        """

        if metadata.control_plane_origin not in self.trusted_origins:
            raise ProviderError("The enrollment service is not trusted")
        credential = self.credential(metadata)
        with httpx.Client(
            base_url=metadata.control_plane_origin,
            timeout=20,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        ) as client:
            try:
                response = client.post(
                    f"/v1/installations/{metadata.installation_id}/reenroll",
                    headers={"authorization": f"Bearer {credential}"},
                )
            except httpx.HTTPError as exc:
                raise ProviderError("The Remote link could not be regenerated") from exc
        if response.status_code not in {200, 201}:
            raise ProviderError("The Remote link could not be regenerated; try again")
        try:
            payload = response.json()
            installation_id = payload["installationId"]
            hostname = payload["hostname"]
            new_credential = payload["installationCredential"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("The re-enrollment response was invalid") from exc
        match = _CREDENTIAL.fullmatch(new_credential) if isinstance(new_credential, str) else None
        if (
            not isinstance(installation_id, str)
            or not _INSTALLATION_ID.fullmatch(installation_id)
            or match is None
            or match.group(1) != installation_id
            or not isinstance(hostname, str)
            or not _HOSTNAME.fullmatch(hostname)
            or hostname != f"sp-{installation_id}.{hostname.split('.', 1)[1]}"
        ):
            raise ProviderError("The re-enrollment response was not installation-bound")
        new_metadata = BootstrapMetadata.model_validate(
            {
                "schema": INSTALLATION_SCHEMA,
                "version": metadata.version,
                "bundleId": metadata.bundle_id,
                "controlPlaneOrigin": metadata.control_plane_origin,
                "installationId": installation_id,
                "hostname": hostname,
                "remotePort": metadata.remote_port,
            }
        )
        # Store the new credential before swapping `active` so a crash
        # between these two writes still leaves a usable (old) identity
        # recoverable by re-reading the still-valid old credential -- never
        # a state with neither credential present.
        self.credentials.set(installation_id, new_credential)
        state = self.state()
        state.active = new_metadata
        self._write(state)
        self.credentials.delete(metadata.installation_id)
        return new_metadata

    def finish_revoke(self, metadata: BootstrapMetadata, *, hard: bool = False) -> None:
        """Local bookkeeping after the caller has revoked the control-plane
        tunnel/DNS for `metadata`.

        By default (`hard=False`, the normal disable/revoke path) the
        installation credential itself is deliberately KEPT (not deleted):
        it stays cryptographically valid at the control plane until a
        reactivation bumps its credential generation, so retaining it here
        lets a genuine future re-enable use the authenticated
        `reactivate()` path below instead of burning the anonymous
        per-network enrollment quota meant for first-time/unknown installs.
        `metadata` moves from `active` to `retired`.

        `hard=True` is for when the control plane has already told us this
        exact credential is no longer accepted (`InstallationRevokedError`,
        e.g. an admin-forced recovery) -- there is nothing to reactivate
        with, so the credential is purged immediately rather than left
        around unusable.
        """

        if hard:
            self.credentials.delete(metadata.installation_id)
        state = self.state()
        if state.active is not None and state.active.installation_id == metadata.installation_id:
            state.active = None
            state.retired = None if hard else metadata
            # Note: enrollment_nonce is intentionally left in place. It is
            # the durable installation identity, not a revocable secret --
            # the control plane never returns it and clearing it here would
            # force every re-enable to mint a brand-new installation/hostname
            # (see ensure_enrolled()). The tunnel/DNS are still genuinely
            # revoked above and by the caller.
            self._write(state)

    def reactivate(
        self,
        metadata: BootstrapMetadata,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> BootstrapMetadata:
        """Re-enable a previously-known, disabled/revoked installation by
        proving ownership of it via its still-valid installation credential,
        instead of going through the anonymous, per-network-quota-limited
        `/v1/installations/enroll` route.

        This is the fix for "Enable fails after the network's anonymous
        enrollment quota was exhausted by the (now-fixed) regenerate bug":
        a machine that has a KNOWN prior installation identity (even
        disabled/revoked) can always get back in via this authenticated
        path, which is billed against the per-installation mutation budget,
        never the anonymous-enrollment abuse quota. A genuinely first-time
        install with no prior identity still has no credential to present
        here and must go through `ensure_enrolled()` -- the quota keeps
        protecting exactly the case it exists for.
        """

        if metadata.control_plane_origin not in self.trusted_origins:
            raise ProviderError("The enrollment service is not trusted")
        credential = self.credential(metadata)
        with httpx.Client(
            base_url=metadata.control_plane_origin,
            timeout=20,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        ) as client:
            try:
                response = client.post(
                    f"/v1/installations/{metadata.installation_id}/reactivate",
                    headers={"authorization": f"Bearer {credential}"},
                )
            except httpx.HTTPError as exc:
                raise ProviderError(
                    "Could not reach the enrollment service. Check your Internet "
                    "connection and try again."
                ) from exc
        if response.status_code == 429:
            raise ProviderError(
                "This computer has reached its enrollment limit for now. Try again later."
            )
        if response.status_code in {401, 403}:
            raise ProviderError(
                "This installation's credential was revoked. Contact beta support to "
                "recover this installation."
            )
        if response.status_code not in {200, 201}:
            raise ProviderError("The enrollment service is unavailable; retry later")
        try:
            payload = response.json()
            installation_id = payload["installationId"]
            hostname = payload["hostname"]
            new_credential = payload["installationCredential"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("The reactivation response was invalid") from exc
        match = _CREDENTIAL.fullmatch(new_credential) if isinstance(new_credential, str) else None
        if (
            not isinstance(installation_id, str)
            or not _INSTALLATION_ID.fullmatch(installation_id)
            or match is None
            or match.group(1) != installation_id
            or not isinstance(hostname, str)
            or not _HOSTNAME.fullmatch(hostname)
            or hostname != f"sp-{installation_id}.{hostname.split('.', 1)[1]}"
            or installation_id != metadata.installation_id
            or hostname != metadata.hostname
        ):
            raise ProviderError("The reactivation response was not installation-bound")
        new_metadata = BootstrapMetadata.model_validate(
            {
                "schema": INSTALLATION_SCHEMA,
                "version": metadata.version,
                "bundleId": metadata.bundle_id,
                "controlPlaneOrigin": metadata.control_plane_origin,
                "installationId": installation_id,
                "hostname": hostname,
                "remotePort": metadata.remote_port,
            }
        )
        self.credentials.set(installation_id, new_credential)
        state = self.state()
        state.retired = None
        state.active = new_metadata
        self._write(state)
        return new_metadata

    def discard_identity(self, metadata: BootstrapMetadata) -> None:
        """Force a genuinely new installation/hostname on the next enrollment.

        Unlike `finish_revoke` (which deliberately keeps the enrollment
        nonce so a disable/enable cycle reprovisions the SAME installation),
        this clears the nonce too, so the next `ensure_enrolled` call mints a
        brand-new installation id and hostname. Used for an explicit
        "Regenerate Remote link" -- the previous credential/tunnel/DNS are
        revoked by the caller first; this only retires the local identity
        record so a fresh one is issued.
        """

        self.credentials.delete(metadata.installation_id)
        state = self.state()
        if state.active is not None and state.active.installation_id == metadata.installation_id:
            state.active = None
        if state.retired is not None and state.retired.installation_id == metadata.installation_id:
            state.retired = None
        if state.enrollment_nonce is not None:
            state.enrollment_nonce = None
        self._write(state)

    def _write(self, state: BootstrapState) -> None:
        atomic_write(self.path, state.model_dump_json(by_alias=True))
