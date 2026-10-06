"""Playback receive configuration; the sole control exception is confirmed discovery."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from stagepilot.api.remote_retry import RemoteRetryRoute
from stagepilot.core.config import MidiSource, PlaybackApiSettings
from stagepilot.core.settings import SettingsFileError
from stagepilot.plugins.playback_api.arbiter import PlaybackConnection, Source, SourceStatus
from stagepilot.plugins.playback_api.client import ConnectionOptions
from stagepilot.plugins.playback_api.normalizer import PlaybackEvent
from stagepilot.plugins.playback_api.plugin import DiscoveryConflict, PlaybackInputPlugin

router = APIRouter(prefix="/api/v1/playback-api", route_class=RemoteRetryRoute)


class PlaybackSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    host: str | None = None
    port: int = Field(default=8080, ge=1, le=65535)
    auto_scan: bool = True


class ScanCandidateModel(BaseModel):
    name: str
    host: str


class ScanResult(BaseModel):
    state: Literal["idle", "scanning", "found", "not_found"]
    candidates: list[ScanCandidateModel]
    reason: str | None
    current: int
    total: int


class FindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str | None = Field(default=None, max_length=255)


class PlaybackStatusResponse(BaseModel):
    scan: ScanResult
    selected: bool
    enabled: bool
    connected: bool
    active_source: Source
    sources: dict[str, SourceStatus]
    reason: str
    host: str | None
    port: int
    source: Literal["manual", "loopback", "lan"] | None
    last_error: str | None
    playing: bool
    setlist_cloud_version: int | None
    discovery: Literal["idle", "running", "failed", "done"]
    progress: int
    song_order: list[int]
    captured_version: int | None
    captured_at: datetime | None
    stale: bool
    settings: PlaybackSettingsRequest


class PlaybackMonitorEntry(BaseModel):
    event: PlaybackEvent
    discovery: bool
    at: datetime


class PlaybackEventsResponse(BaseModel):
    events: list[PlaybackMonitorEntry]
    capacity: int = 100


def _controller(request: Request) -> PlaybackInputPlugin:
    controller: PlaybackInputPlugin | None = request.app.state.runtime.playback_input
    if controller is None:
        raise HTTPException(409, "Playback API input is unavailable.")
    return controller


def status_response(controller: PlaybackInputPlugin) -> PlaybackStatusResponse:
    status = controller.status
    config = controller.settings.playback_api
    heartbeat = status.heartbeat
    connection = controller.connection
    return PlaybackStatusResponse(
        scan=ScanResult(
            state=controller.scan_state,
            candidates=[
                ScanCandidateModel(name=c.name, host=c.host) for c in controller.scan_candidates
            ],
            reason=controller.scan_reason,
            current=controller.scan_current,
            total=controller.scan_total,
        ),
        selected=controller.selected,
        enabled=config.enabled,
        connected=connection.connected,
        active_source=connection.active_source,
        sources=connection.sources,
        reason=connection.reason,
        host=status.host,
        port=status.port,
        source=status.source,
        last_error=controller.discovery_error
        or (None if connection.connected else status.last_error),
        playing=heartbeat.playing if heartbeat else False,
        setlist_cloud_version=heartbeat.setlist_version if heartbeat else None,
        discovery=controller.discovery,
        progress=controller.progress,
        song_order=list(controller.mapper.song_order),
        captured_version=controller.mapper.captured_version,
        captured_at=config.captured_at,
        stale=controller.mapper.stale,
        settings=PlaybackSettingsRequest(
            enabled=config.enabled, host=config.host, port=config.port, auto_scan=config.auto_scan
        ),
    )


@router.get("/status", response_model=PlaybackStatusResponse)
async def status(request: Request) -> PlaybackStatusResponse:
    await _controller(request)._publish_connection()
    return status_response(_controller(request))


@router.get("/connection", response_model=PlaybackConnection)
async def connection(request: Request) -> PlaybackConnection:
    controller = _controller(request)
    await controller._publish_connection()
    return controller.connection


@router.get("/events", response_model=PlaybackEventsResponse)
async def events(request: Request) -> PlaybackEventsResponse:
    return PlaybackEventsResponse(
        events=[
            PlaybackMonitorEntry(event=item.event, discovery=item.discovery, at=item.at)
            for item in _controller(request).events
        ]
    )


@router.post("/find", response_model=PlaybackStatusResponse)
async def find(request: Request, payload: object = Body(default=None)) -> PlaybackStatusResponse:
    """Scan, no setup needed: turns Playback on and selects it, then looks for it."""
    controller = _controller(request)
    try:
        body = FindRequest.model_validate(payload or {})
    except ValueError as exc:
        raise HTTPException(422, "Enter a host name or IP address.") from exc
    try:
        await controller.find(body.host)
    except DiscoveryConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return status_response(controller)


@router.post("/find/cancel", response_model=PlaybackStatusResponse)
async def cancel_find(request: Request) -> PlaybackStatusResponse:
    controller = _controller(request)
    await controller.cancel_scan()
    return status_response(controller)


@router.put("/settings", response_model=PlaybackStatusResponse)
async def settings(payload: PlaybackSettingsRequest, request: Request) -> PlaybackStatusResponse:
    controller = _controller(request)
    runtime = request.app.state.runtime
    if controller.discovery == "running":
        raise HTTPException(409, "Wait for Discover Song Order before changing settings.")
    try:
        ConnectionOptions(payload.enabled, payload.host, payload.port, payload.auto_scan)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    previous = runtime.settings_service.snapshot()
    updated = PlaybackApiSettings.model_validate(
        {**previous.playback_api.model_dump(), **payload.model_dump()}
    )
    # Turning Playback on is choosing it: one switch, no hidden second source setting.
    modes = previous.integration_modes
    if payload.enabled and modes.midi_source is not MidiSource.PLAYBACK_API:
        modes = modes.model_copy(update={"midi_source": MidiSource.PLAYBACK_API})
    try:
        await controller.save_settings(
            previous.model_copy(
                update={"playback_api": updated, "integration_modes": modes}, deep=True
            )
        )
    except SettingsFileError as exc:
        raise HTTPException(503, str(exc)) from exc
    except DiscoveryConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return status_response(controller)


@router.post("/discover-song-order", response_model=PlaybackStatusResponse)
async def discover(
    request: Request, payload: object = Body(default=None)
) -> PlaybackStatusResponse:
    if not isinstance(payload, dict) or payload.get("confirm") is not True:
        raise HTTPException(400, "Confirm Discover Song Order with {confirm: true}.")
    controller = _controller(request)
    try:
        await controller.discover_song_order()
    except DiscoveryConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return status_response(controller)
