"""Tests for the connector-heartbeat-stale -> webhook alert (WhatsApp relay).

These tests never make a real network call: the HTTP POST is mocked via
``httpx.MockTransport`` (matching the pattern used by remote_control tests
in this repo), so no WhatsApp message is ever sent during the test run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from stagepilot.remote_health_alert import build_alert_payload, check_and_alert, send_alert


def _write_status(path: Path, *, state: str, checked_at: float) -> None:
    path.write_text(json.dumps({"state": state, "checked_at": checked_at}), encoding="utf-8")


def test_stale_heartbeat_triggers_alert(tmp_path: Path) -> None:
    status_path = tmp_path / "connector-status.json"
    now = time.time()
    _write_status(status_path, state="connected", checked_at=now - 60.0)

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_sender(webhook_url: str, payload: dict[str, object]) -> None:
        calls.append((webhook_url, payload))

    result = check_and_alert(
        status_path,
        max_age_seconds=10.0,
        webhook_url="http://127.0.0.1:9/hermes/whatsapp-relay",
        now=now,
        sender=fake_sender,
    )

    assert result.healthy is False
    assert result.detail == "connector status heartbeat is stale"
    assert len(calls) == 1
    webhook_url, payload = calls[0]
    assert webhook_url == "http://127.0.0.1:9/hermes/whatsapp-relay"
    assert payload["event"] == "connector_heartbeat_stale"
    assert payload["state"] == "connected"
    assert payload["age_seconds"] == pytest.approx(60.0, abs=0.5)
    # Never leak the webhook URL (or any secret) inside the alert payload itself.
    assert "webhook_url" not in payload
    assert "url" not in payload


def test_fresh_heartbeat_does_not_trigger_alert(tmp_path: Path) -> None:
    status_path = tmp_path / "connector-status.json"
    now = time.time()
    _write_status(status_path, state="connected", checked_at=now - 2.0)

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_sender(webhook_url: str, payload: dict[str, object]) -> None:
        calls.append((webhook_url, payload))

    result = check_and_alert(
        status_path,
        max_age_seconds=10.0,
        webhook_url="http://127.0.0.1:9/hermes/whatsapp-relay",
        now=now,
        sender=fake_sender,
    )

    assert result.healthy is True
    assert calls == []


def test_stale_heartbeat_without_webhook_configured_does_not_call_sender(tmp_path: Path) -> None:
    status_path = tmp_path / "connector-status.json"
    now = time.time()
    _write_status(status_path, state="reconnecting", checked_at=now - 60.0)

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_sender(webhook_url: str, payload: dict[str, object]) -> None:
        calls.append((webhook_url, payload))

    result = check_and_alert(
        status_path,
        max_age_seconds=10.0,
        webhook_url=None,
        now=now,
        sender=fake_sender,
    )

    assert result.healthy is False
    assert calls == []


def test_send_alert_posts_expected_payload_via_mock_transport() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = {"event": "connector_heartbeat_stale", "state": "connected", "age_seconds": 42.0}

    send_alert("http://127.0.0.1:9/relay", payload, client=client)

    assert seen["url"] == "http://127.0.0.1:9/relay"
    assert seen["json"] == payload


def test_send_alert_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        send_alert("http://127.0.0.1:9/relay", {"event": "x"}, client=client)


def test_build_alert_payload_has_no_secrets() -> None:
    from stagepilot.remote_health import StatusCheck

    payload = build_alert_payload(
        StatusCheck(False, "connected", 55.5, "connector status heartbeat is stale")
    )

    assert payload == {
        "source": "stagepilot-remote-connector",
        "event": "connector_heartbeat_stale",
        "state": "connected",
        "age_seconds": 55.5,
        "detail": "connector status heartbeat is stale",
    }
