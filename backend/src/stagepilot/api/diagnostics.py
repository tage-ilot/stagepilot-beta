"""Local-backend proxy for \"Send logs to developer\".

The frontend/desktop shell never talks to the control plane directly -- it
has no installation id or bearer credential anywhere in `frontend/src` (see
`frontend/src/diagnostics.ts`). Those only exist on this installation's
enrolled identity (`DesktopBootstrapStore`, `app.state.remote_manager.bootstrap`),
the same identity `services.critical_alerts.send_control_plane_alert` already
uses to forward critical alerts. This route reuses that identity to forward a
diagnostic bundle collected by the Tauri `collect_diagnostic_bundle` command
to the control plane's `POST /v1/installations/:id/diagnostics` route.

A machine without an active enrollment (no Remote Access provisioned) simply
cannot send logs to the developer this way; this is surfaced to the user as a
503, not a silent failure -- mirroring `send_control_plane_alert`'s own
no-op-when-unenrolled behavior but turning it into a visible error here since
this route is a direct user action, not a background alert.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from stagepilot.remote_bootstrap import DesktopBootstrapStore

router = APIRouter(prefix="/api/v1/diagnostics")

logger = logging.getLogger(__name__)

# Mirrors the 2MB client/server caps agreed in the parallel tasks that built
# the Tauri bundle collector (t_40dd46f5) and the control-plane upload route
# (t_48cff096); reject oversized bundles locally before spending a network
# round-trip on a request the control plane will reject anyway.
MAX_BUNDLE_BYTES = 2 * 1024 * 1024


class DiagnosticsSendRequest(BaseModel):
    bundle: str


class DiagnosticsSendResponse(BaseModel):
    ok: bool
    message: str


def _bootstrap(request: Request) -> DesktopBootstrapStore | None:
    manager = getattr(request.app.state, "remote_manager", None)
    return getattr(manager, "bootstrap", None) if manager is not None else None


@router.post("/send", response_model=DiagnosticsSendResponse)
async def send_diagnostics(
    payload: DiagnosticsSendRequest, request: Request
) -> DiagnosticsSendResponse:
    if len(payload.bundle.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise HTTPException(status_code=413, detail="Diagnostic bundle is too large to send.")

    bootstrap = _bootstrap(request)
    if bootstrap is None:
        raise HTTPException(
            status_code=503,
            detail="Sending logs to the developer requires Remote Access to be set up on this "
            "installation.",
        )

    try:
        active = bootstrap.state().active
    except Exception as exc:  # pragma: no cover - mirrors send_control_plane_alert's guard
        raise HTTPException(
            status_code=503, detail="Sending logs to the developer is unavailable right now."
        ) from exc
    if active is None:
        raise HTTPException(
            status_code=503,
            detail="Sending logs to the developer requires Remote Access to be set up on this "
            "installation.",
        )
    if active.control_plane_origin not in bootstrap.trusted_origins:
        raise HTTPException(
            status_code=503, detail="Sending logs to the developer is unavailable right now."
        )

    try:
        credential = bootstrap.credential(active)
        async with httpx.AsyncClient(
            base_url=active.control_plane_origin,
            timeout=30.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"/v1/installations/{active.installation_id}/diagnostics",
                headers={
                    "authorization": f"Bearer {credential}",
                    "content-type": "application/octet-stream",
                },
                content=payload.bundle.encode("utf-8"),
            )
        if response.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="Too many diagnostic uploads recently; try again later.",
            )
        response.raise_for_status()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "diagnostics upload could not be forwarded to the control plane", exc_info=True
        )
        raise HTTPException(
            status_code=502, detail="Logs could not be sent to the developer. Try again shortly."
        ) from exc

    return DiagnosticsSendResponse(ok=True, message="Logs sent to the developer.")
