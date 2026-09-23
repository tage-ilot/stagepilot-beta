"""Packaged-desktop Remote lifecycle and bundled connector supervision."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI

from stagepilot.remote_beta_control import BetaRemoteControl, InstallationRevokedError
from stagepilot.remote_bootstrap import (
    DEFAULT_CONTROL_PLANE_ORIGIN,
    DEFAULT_REMOTE_PORT,
    BootstrapMetadata,
    DesktopBootstrapStore,
)
from stagepilot.remote_connector import Connector
from stagepilot.remote_feature import RemoteFeature
from stagepilot.remote_files import BetaControlConfig, atomic_write, read_desired, safe_status
from stagepilot.remote_provider import InstallationPermanentlyRevokedError, ProviderError
from stagepilot.remote_runtime import attach_managed_remote


class DesktopRemoteManager:
    def __init__(
        self,
        root: Path,
        cloudflared_binary: Path,
        *,
        bootstrap_store: DesktopBootstrapStore | None = None,
        revoke_sessions: Callable[[], None] | None = None,
        lan_port: int = 8765,
        transport: httpx.BaseTransport | None = None,
        control_plane_origin: str = DEFAULT_CONTROL_PLANE_ORIGIN,
        remote_port: int = DEFAULT_REMOTE_PORT,
    ) -> None:
        if not root.is_absolute() or not cloudflared_binary.is_absolute():
            raise ValueError("Desktop Remote paths must be absolute")
        self.root = root
        self.cloudflared_binary = cloudflared_binary
        self.installation_dir = root / "connector"
        self.state_dir = root / "control"
        self.desired_path = self.installation_dir / "remote.json"
        self.connector_status_path = self.installation_dir / "connector-status.json"
        self._permanently_revoked_marker = self.state_dir / "revoked.json"
        self.feature = RemoteFeature(self.desired_path)
        self.bootstrap = bootstrap_store or DesktopBootstrapStore(root / "bootstrap.json")
        self.revoke_sessions = revoke_sessions
        self.lan_port = lan_port
        self.transport = transport
        self.control_plane_origin = control_plane_origin
        self.remote_port = remote_port
        self._connector_token: str | None = None

    def status(self) -> dict[str, object]:
        result = self.feature.status()
        try:
            active = self.bootstrap.state().active
            provisioned = active is not None
        except ProviderError:
            provisioned = False
            active = None
        try:
            credential_available = active is not None and bool(self.bootstrap.credential(active))
        except ProviderError:
            credential_available = False
        result.update(
            {
                "provisioned": provisioned,
                "credential_available": credential_available,
                "temporary_url": False,
                "permanently_revoked": self._permanently_revoked_marker.exists(),
            }
        )
        if not provisioned:
            result.update(
                {
                    "available": self.cloudflared_binary.is_file(),
                    "enabled": False,
                    "state": "off",
                    "url": None,
                    "message": (
                        "This private beta enrolls automatically when Remote Access is first "
                        "enabled."
                    ),
                }
            )
        return result

    def enable(self) -> dict[str, object]:
        metadata = self._active()
        result = self._apply(metadata, "enable")
        self._capture_connector_token(result)
        generation = str(result.get("generation", ""))
        if not generation:
            raise ProviderError("Remote provisioning did not return a generation")
        self.feature.set_managed_enabled(True, generation)
        return self.status()

    def reset_identity(self) -> dict[str, object]:
        """Explicit, visible recovery for a `retired` installation whose
        credential the control plane has permanently revoked (reactivate()
        returns 401/403 -- see `InstallationPermanentlyRevokedError`).

        There is nothing left to reactivate or reenroll with (both require
        presenting the now-dead credential), so this discards the local
        identity record entirely and lets the next enable attempt mint a
        brand-new installation via the anonymous `/v1/installations/enroll`
        path. This never runs silently: it is only reachable via an
        explicit user action from the UI once `enable()` has surfaced
        `InstallationPermanentlyRevokedError`, and it never touches local
        production state (Remote users, local StagePilot config).
        """

        retired = self.bootstrap.state().retired
        if retired is not None:
            self.bootstrap.discard_identity(retired)
        self.feature.set_managed_enabled(False)
        self._publish_off_status()
        self._permanently_revoked_marker.unlink(missing_ok=True)
        return self.status()

    def disable(self) -> dict[str, object]:
        self.feature.set_managed_enabled(False)
        self._clear_connector_credential()
        active = self.bootstrap.state().active
        if active is None:
            self._publish_off_status()
            return self.status()
        with suppress(InstallationRevokedError):
            self._apply(active, "revoke")
        self._retire_local(active)
        self._publish_off_status()
        return self.status()

    def regenerate(self) -> dict[str, object]:
        """Retire this installation identity and provision a new one,
        proving ownership of the old identity via an authenticated
        re-enrollment call instead of the anonymous enrollment endpoint.

        Idempotent and restart-safe: the previous tunnel/DNS record and
        installation credential are revoked before a brand-new installation
        id/hostname is enrolled, so no public resource from the old identity
        is left orphaned. Remote users (Operators/Viewers) live in the local
        identity store, which is untouched here, so they survive intact.
        Local production keeps running throughout -- only the managed Remote
        listener is reconciled onto the new identity.

        Root cause of the previous "Regenerate breaks Remote" bug: this
        method used to clear the local enrollment nonce and go back through
        `ensure_enrolled()` -> the anonymous, nonce-based
        `/v1/installations/enroll` endpoint. That endpoint is guarded by a
        per-network quota (`ENROLLMENTS_PER_SOURCE`, 3/24h) meant to stop
        arbitrary Internet clients from minting tunnels; a handful of
        regenerations from one network exhausted that quota and every
        further regeneration (and the fallback re-enable) was denied with
        429/503, leaving Remote off with no way back from the UI. The fix
        uses a new authenticated `/v1/installations/<id>/reenroll` control-
        plane route instead: the caller already proved ownership by
        presenting the current installation credential, so it is billed
        against the (much larger) authenticated mutation-rate budget, not
        the anonymous-enrollment abuse budget.
        """

        active = self.bootstrap.state().active
        was_enabled = self.feature.intent().enabled
        if active is None:
            # Nothing to regenerate from; behave like a first-time enable.
            self.feature.set_managed_enabled(False)
            self._publish_off_status()
            if was_enabled:
                self.enable()
            else:
                self._active()
            return self.status()
        try:
            new_active = self.bootstrap.reenroll(active, transport=self.transport)
        except ProviderError:
            # Reenroll failed atomically server-side (or the request never
            # reached the control plane) -- the old identity/credential are
            # untouched, so surface the error and leave the installation in
            # its previous, still-working state rather than tearing
            # anything down locally.
            raise
        self._clear_connector_credential()
        if self.revoke_sessions is not None:
            self.revoke_sessions()
        (self.state_dir / "state.json").unlink(missing_ok=True)
        self.feature.set_managed_enabled(False)
        self._publish_off_status()
        if was_enabled:
            self.enable()
        else:
            # Confirm the freshly re-enrolled identity is reachable/valid
            # without forcing Remote back on when the operator had it off.
            self.bootstrap.credential(new_active)
        return self.status()

    def reconcile_control(self) -> None:
        active = self.bootstrap.state().active
        if active is None:
            self.feature.set_managed_enabled(False)
            self._clear_connector_credential()
            self._publish_off_status()
            return
        try:
            result = self._apply(active, "reconcile")
        except InstallationRevokedError:
            self.feature.set_managed_enabled(False)
            self._retire_local(active, hard=True)
            self._publish_off_status()
            return
        if result.get("phase") == "enabled":
            self._capture_connector_token(result)
        phase = result.get("phase")
        if phase == "enabled":
            generation = str(result.get("generation", ""))
            self.feature.set_managed_enabled(True, generation)
        elif phase == "disabled":
            self.feature.set_managed_enabled(False)
            self._clear_connector_credential()
            # A persisted retiring state means provider revocation completed after restart.
            state_path = self.state_dir / "state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                state = {}
            if state.get("retire") is True:
                self._retire_local(active)
        self._publish_off_status_if_disabled()

    async def run(self, stop: asyncio.Event) -> None:
        # Persisted intent remains retryable and local production stays available.
        reconcile_at = 0.0
        reconcile_delay = 1.0
        connector = Connector(
            self.cloudflared_binary,
            self.desired_path,
            self.connector_status_path,
            self._metrics_port(),
            token_provider=self._connector_credential,
            token_path=self.root / "run/connector.token",
        )
        try:
            while not stop.is_set():
                if (
                    self.feature.intent().enabled
                    and self._connector_token is None
                    and time.monotonic() >= reconcile_at
                ):
                    try:
                        await asyncio.to_thread(self.reconcile_control)
                    except (OSError, ValueError, ProviderError):
                        reconcile_at = time.monotonic() + reconcile_delay
                        reconcile_delay = min(reconcile_delay * 2, 30)
                    else:
                        reconcile_delay = 1.0
                try:
                    await asyncio.to_thread(connector.step)
                    self._publish_connector_status()
                except (OSError, ValueError):
                    await asyncio.to_thread(connector.stop)
                    self._publish_reconnecting_status()
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.5)
        finally:
            await asyncio.to_thread(connector.stop)
            self._publish_off_status_if_disabled()

    def _active(self) -> BootstrapMetadata:
        try:
            active = self.bootstrap.state().active
            if active is None:
                # ensure_enrolled() already reads/validates the Keychain
                # credential internally for every path that has one to
                # check (the pre-existing `active` branch calls
                # self.credential(); the `retired` branch's reactivate()
                # reads it once before the reactivate POST). Only the
                # brand-new anonymous-enrollment path has no prior
                # credential to verify -- it just wrote one. Re-reading it
                # again right below would be a second, avoidable
                # Keychain/system-password prompt for the exact same
                # enable attempt, so we trust this return value rather
                # than reading a second time.
                active = self.bootstrap.ensure_enrolled(
                    control_plane_origin=self.control_plane_origin,
                    remote_port=self.remote_port,
                    transport=self.transport,
                )
            else:
                self.bootstrap.credential(active)
        except InstallationPermanentlyRevokedError:
            # Surface a durable, visible "needs reset" signal for status()
            # rather than letting the operator retry a doomed reactivation
            # forever; cleared by a successful _active() or reset_identity().
            atomic_write(self._permanently_revoked_marker, "{}")
            raise
        if self._permanently_revoked_marker.exists():
            self._permanently_revoked_marker.unlink(missing_ok=True)
        return active

    def _apply(self, metadata: BootstrapMetadata, action: str) -> dict[str, object]:
        config = BetaControlConfig(
            control_plane_url=metadata.control_plane_origin,
            installation_id=metadata.installation_id,
            hostname=metadata.hostname,
            credential_file=None,
            state_dir=self.state_dir,
            installation_dir=self.installation_dir,
            remote_port=metadata.remote_port,
            lan_port=self.lan_port,
        )
        with httpx.Client(
            base_url=config.control_plane_url,
            timeout=20,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            control = BetaRemoteControl(
                config,
                client,
                credential_provider=lambda: self.bootstrap.credential(metadata),
            )
            result = control.apply(action)
            if control.connector_token is not None:
                result["tunnel_token"] = control.connector_token
            return result

    def _capture_connector_token(self, result: dict[str, object]) -> None:
        self._connector_token = None
        token = result.pop("tunnel_token", None)
        if not isinstance(token, str) or len(token) < 20 or any(c.isspace() for c in token):
            raise ProviderError("Installation tunnel credential unavailable")
        self._connector_token = token

    def _clear_connector_credential(self) -> None:
        self._connector_token = None
        (self.installation_dir / "connector.token").unlink(missing_ok=True)
        (self.root / "run/connector.token").unlink(missing_ok=True)

    def _retire_local(self, metadata: BootstrapMetadata, *, hard: bool = False) -> None:
        self._clear_connector_credential()
        if self.revoke_sessions is not None:
            self.revoke_sessions()
        self.bootstrap.finish_revoke(metadata, hard=hard)
        (self.state_dir / "state.json").unlink(missing_ok=True)

    def _connector_credential(self) -> str:
        if self._connector_token is None:
            raise OSError("Installation tunnel credential unavailable")
        return self._connector_token

    def _metrics_port(self) -> int:
        active = self.bootstrap.state().active
        remote_port = active.remote_port if active is not None else self.remote_port
        candidate = remote_port + 1 if remote_port < 65535 else remote_port - 1
        return 18767 if candidate in {self.lan_port, remote_port} else candidate

    def _publish_connector_status(self) -> None:
        intent = self.feature.intent()
        desired = read_desired(self.desired_path)
        state = "off"
        url: str | None = None
        if intent.enabled:
            state = "enabling"
            if desired.enabled:
                try:
                    current = json.loads(self.connector_status_path.read_text(encoding="utf-8"))
                    if current.get("generation") == desired.generation:
                        candidate = current.get("state")
                        if candidate in {"connected", "reconnecting"}:
                            state = candidate
                except (OSError, ValueError, TypeError):
                    pass
                if state == "connected":
                    url = desired.public_origin
        safe_status(
            self.feature.status_path,
            state=state,
            available=self.cloudflared_binary.is_file(),
            checked_at=time.time(),
            generation=intent.generation,
            url=url,
            temporary_url=False,
        )

    def _publish_reconnecting_status(self) -> None:
        intent = self.feature.intent()
        safe_status(
            self.feature.status_path,
            state="reconnecting" if intent.enabled else "off",
            available=self.cloudflared_binary.is_file(),
            checked_at=time.time(),
            generation=intent.generation,
            url=None,
            temporary_url=False,
        )

    def _publish_off_status(self) -> None:
        safe_status(
            self.feature.status_path,
            state="off",
            available=self.cloudflared_binary.is_file(),
            checked_at=time.time(),
            generation=self.feature.intent().generation,
            url=None,
            temporary_url=False,
        )

    def _publish_off_status_if_disabled(self) -> None:
        if not self.feature.intent().enabled:
            self._publish_off_status()


def attach_desktop_remote(application: FastAPI, manager: DesktopRemoteManager) -> None:
    """Attach the dedicated listener and connector to the existing app lifespan."""

    attach_managed_remote(application, manager.desired_path, lan_port=manager.lan_port)
    application.state.remote_manager = manager
    original = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with original(app):
            stop = asyncio.Event()
            task = asyncio.create_task(manager.run(stop), name="stagepilot-desktop-remote")
            try:
                yield
            finally:
                stop.set()
                await task

    application.router.lifespan_context = lifespan
