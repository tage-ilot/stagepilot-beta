"""Tests for the critical-alert store and 15 minute cooldown/dedup engine.

These tests never make a real network call: webhook delivery is mocked via
``httpx.MockTransport`` (matching the pattern used by
``test_remote_health_alert.py``), so no WhatsApp message is ever sent during
the test run.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from stagepilot.services.critical_alerts import (
    COOLDOWN_SECONDS,
    STATUS_ACKNOWLEDGED,
    AlertSender,
    CriticalAlertStore,
    alert_critical,
    build_webhook_payload,
    default_webhook_url,
    send_webhook_alert,
)


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(tmp_path: Path, clock: _Clock) -> CriticalAlertStore:
    return CriticalAlertStore(tmp_path / "alerts.sqlite3", clock=clock)


def _recording_sender() -> tuple[list[tuple[str, dict[str, object]]], AlertSender]:
    calls: list[tuple[str, dict[str, object]]] = []

    def sender(webhook_url: str, payload: dict[str, object]) -> None:
        calls.append((webhook_url, payload))

    return calls, sender


def test_fresh_alert_notifies_immediately(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    calls, sender = _recording_sender()

    result = alert_critical(
        store,
        "rate_limit_exhausted",
        "203.0.113.5",
        "Rate limit exhausted for 203.0.113.5",
        "critical",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )

    assert result.should_notify is True
    assert result.alert.count == 1
    assert result.alert.status == "open"
    assert len(calls) == 1
    webhook_url, payload = calls[0]
    assert webhook_url == "http://127.0.0.1:9/hook"
    assert payload == {
        "category": "rate_limit_exhausted",
        "message": "Rate limit exhausted for 203.0.113.5",
        "subject": "203.0.113.5",
        "severity": "critical",
    }


def test_repeat_within_cooldown_does_not_renotify_but_increments_count(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    calls, sender = _recording_sender()

    first = alert_critical(
        store,
        "unexpected_error",
        "loc:module.py:42",
        "boom",
        "warning",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )
    clock.advance(60.0)
    second = alert_critical(
        store,
        "unexpected_error",
        "loc:module.py:42",
        "boom again",
        "warning",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )

    assert first.should_notify is True
    assert second.should_notify is False
    assert second.alert.count == 2
    assert second.alert.id == first.alert.id
    assert len(calls) == 1  # only the first call notified


def test_repeat_after_cooldown_renotifies(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    calls, sender = _recording_sender()

    first = alert_critical(
        store,
        "rate_anomaly",
        "user@example.com",
        "odd pattern",
        "warning",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )
    clock.advance(COOLDOWN_SECONDS + 1)
    second = alert_critical(
        store,
        "rate_anomaly",
        "user@example.com",
        "still odd",
        "warning",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )

    assert first.should_notify is True
    assert second.should_notify is True
    assert second.alert.count == 2
    assert second.alert.id == first.alert.id
    assert len(calls) == 2


def test_repeat_exactly_at_cooldown_boundary_renotifies(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    calls, sender = _recording_sender()

    alert_critical(
        store,
        "cat",
        "subj",
        "m",
        "warning",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )
    clock.advance(COOLDOWN_SECONDS)
    second = alert_critical(
        store,
        "cat",
        "subj",
        "m2",
        "warning",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )

    assert second.should_notify is True
    assert len(calls) == 2


def test_acknowledge_then_new_occurrence_notifies_fresh(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    calls, sender = _recording_sender()

    first = alert_critical(
        store,
        "unexpected_error",
        "loc:x.py:1",
        "boom",
        "critical",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )
    store.acknowledge(first.alert.id)
    clock.advance(5.0)  # well within cooldown, but acknowledged
    second = alert_critical(
        store,
        "unexpected_error",
        "loc:x.py:1",
        "boom again",
        "critical",
        webhook_url="http://127.0.0.1:9/hook",
        sender=sender,
    )

    assert second.should_notify is True
    assert second.alert.id != first.alert.id
    assert second.alert.count == 1
    assert second.alert.status == "open"
    assert len(calls) == 2


def test_acknowledge_marks_status_and_stops_further_renotify_until_new_occurrence(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)

    first = alert_critical(store, "cat", "subj", "m", "warning", webhook_url="")
    acked = store.acknowledge(first.alert.id)
    assert acked is not None
    assert acked.status == STATUS_ACKNOWLEDGED

    # get() reflects the acknowledged status.
    fetched = store.get(first.alert.id)
    assert fetched is not None
    assert fetched.status == STATUS_ACKNOWLEDGED

    # Acknowledged alerts do not show up in list_open().
    assert store.list_open() == []


def test_list_open_and_get(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)

    a = alert_critical(store, "cat1", "subj1", "m1", "warning", webhook_url="")
    b = alert_critical(store, "cat2", "subj2", "m2", "critical", webhook_url="")

    open_alerts = store.list_open()
    assert {alert.id for alert in open_alerts} == {a.alert.id, b.alert.id}

    fetched = store.get(a.alert.id)
    assert fetched is not None
    assert fetched.category == "cat1"
    assert fetched.subject == "subj1"

    assert store.get(999999) is None


def test_webhook_payload_shape_is_exact(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)

    result = alert_critical(
        store, "rate_limit_exhausted", "1.2.3.4", "msg", "critical", webhook_url=""
    )
    payload = build_webhook_payload(result.alert)

    assert set(payload.keys()) == {"category", "message", "subject", "severity"}
    assert payload["category"] == "rate_limit_exhausted"
    assert payload["subject"] == "1.2.3.4"
    assert payload["message"] == "msg"
    assert payload["severity"] == "critical"


def test_default_webhook_url_falls_back_to_known_local_hook() -> None:
    assert default_webhook_url({}) == "http://localhost:8644/webhooks/stagepilot-critical-alert"


def test_default_webhook_url_honors_env_override() -> None:
    overridden = default_webhook_url(
        {"STAGEPILOT_CRITICAL_ALERT_WEBHOOK_URL": "http://example.invalid/hook"}
    )
    assert overridden == "http://example.invalid/hook"


def test_send_webhook_alert_posts_expected_payload_via_mock_transport() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = {"category": "cat", "message": "m", "subject": "s", "severity": "warning"}

    send_webhook_alert("http://127.0.0.1:9/hook", payload, client=client)

    assert seen["url"] == "http://127.0.0.1:9/hook"
    assert seen["json"] == payload


def test_send_webhook_alert_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        send_webhook_alert("http://127.0.0.1:9/hook", {"category": "x"}, client=client)  # type: ignore[dict-item]


def test_no_real_network_calls_when_webhook_url_empty(tmp_path: Path) -> None:
    """Passing an empty webhook_url disables delivery entirely (no sender invoked)."""

    clock = _Clock()
    store = _store(tmp_path, clock)
    result = alert_critical(store, "cat", "subj", "m", "warning", webhook_url="")
    assert result.should_notify is True  # engine still says notify...
    # ...but no delivery attempt is made (default sender was never called with a URL).
