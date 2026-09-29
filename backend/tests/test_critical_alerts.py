"""Tests for the critical-alert store and 15 minute cooldown/dedup engine.

These tests never make a real network call: control-plane forwarding uses
``httpx.MockTransport``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx

from stagepilot.remote_bootstrap import DesktopBootstrapStore
from stagepilot.services.critical_alerts import (
    COOLDOWN_SECONDS,
    STATUS_ACKNOWLEDGED,
    AlertSender,
    CriticalAlertStore,
    alert_critical,
    build_control_plane_payload,
    send_control_plane_alert,
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


def _dummy_bootstrap() -> DesktopBootstrapStore:
    return cast(DesktopBootstrapStore, object())


def _recording_sender() -> tuple[list[dict[str, object]], AlertSender]:
    calls: list[dict[str, object]] = []

    def sender(bootstrap: DesktopBootstrapStore, payload: dict[str, object]) -> bool:
        calls.append(payload)
        return True

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
        bootstrap=_dummy_bootstrap(),
        sender=sender,
    )

    assert result.should_notify is True
    assert result.alert.count == 1
    assert result.alert.status == "open"
    assert len(calls) == 1
    payload = calls[0]
    assert payload == {
        "category": "rate_limit_exhausted",
        "subject": "203.0.113.5",
        "message": "Rate limit exhausted for 203.0.113.5",
        "severity": "critical",
        "first_seen": "1970-01-12T13:46:40Z",
        "last_seen": "1970-01-12T13:46:40Z",
        "count": 1,
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
        bootstrap=_dummy_bootstrap(),
        sender=sender,
    )
    clock.advance(60.0)
    second = alert_critical(
        store,
        "unexpected_error",
        "loc:module.py:42",
        "boom again",
        "warning",
        bootstrap=_dummy_bootstrap(),
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
        bootstrap=_dummy_bootstrap(),
        sender=sender,
    )
    clock.advance(COOLDOWN_SECONDS + 1)
    second = alert_critical(
        store,
        "rate_anomaly",
        "user@example.com",
        "still odd",
        "warning",
        bootstrap=_dummy_bootstrap(),
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
        bootstrap=_dummy_bootstrap(),
        sender=sender,
    )
    clock.advance(COOLDOWN_SECONDS)
    second = alert_critical(
        store,
        "cat",
        "subj",
        "m2",
        "warning",
        bootstrap=_dummy_bootstrap(),
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
        bootstrap=_dummy_bootstrap(),
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
        bootstrap=_dummy_bootstrap(),
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

    first = alert_critical(store, "cat", "subj", "m", "warning")
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

    a = alert_critical(store, "cat1", "subj1", "m1", "warning")
    b = alert_critical(store, "cat2", "subj2", "m2", "critical")

    open_alerts = store.list_open()
    assert {alert.id for alert in open_alerts} == {a.alert.id, b.alert.id}

    fetched = store.get(a.alert.id)
    assert fetched is not None
    assert fetched.category == "cat1"
    assert fetched.subject == "subj1"

    assert store.get(999999) is None


def test_control_plane_payload_shape_is_exact(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)

    result = alert_critical(store, "rate_limit_exhausted", "1.2.3.4", "msg", "critical")
    payload = build_control_plane_payload(result.alert)

    assert payload == {
        "category": "rate_limit_exhausted",
        "subject": "1.2.3.4",
        "message": "msg",
        "severity": "critical",
        "first_seen": "1970-01-12T13:46:40Z",
        "last_seen": "1970-01-12T13:46:40Z",
        "count": 1,
    }


class _Bootstrap:
    def __init__(self, *, enrolled: bool = True) -> None:
        from types import SimpleNamespace

        self.trusted_origins = frozenset({"https://control.example"})
        self._active = (
            SimpleNamespace(
                control_plane_origin="https://control.example",
                installation_id="abcd1234",
            )
            if enrolled
            else None
        )

    def state(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(active=self._active)

    def credential(self, metadata: object) -> str:
        return "spi_abcd1234." + "s" * 43


def test_send_control_plane_alert_posts_with_installation_auth() -> None:
    import json

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload: dict[str, object] = {
        "category": "cat",
        "message": "m",
        "subject": "s",
        "severity": "warning",
        "first_seen": "2026-09-29T15:00:00Z",
        "last_seen": "2026-09-29T15:00:01Z",
        "count": 2,
    }
    bootstrap = cast(DesktopBootstrapStore, _Bootstrap())

    assert send_control_plane_alert(bootstrap, payload, client=client) is True
    assert seen["url"] == "https://control.example/v1/installations/abcd1234/alerts"
    assert seen["authorization"] == "Bearer spi_abcd1234." + "s" * 43
    assert seen["json"] == payload


def test_send_control_plane_alert_http_error_degrades_gracefully() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    bootstrap = cast(DesktopBootstrapStore, _Bootstrap())

    assert send_control_plane_alert(bootstrap, {"category": "x"}, client=client) is False


def test_unenrolled_installation_is_noop_without_network() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    bootstrap = cast(DesktopBootstrapStore, _Bootstrap(enrolled=False))

    assert send_control_plane_alert(bootstrap, {"category": "x"}, client=client) is False
    assert called is False


def test_alert_without_bootstrap_still_records_locally(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    result = alert_critical(store, "cat", "subj", "m", "warning")
    assert result.should_notify is True
    assert result.alert.count == 1
