"""Tests for the sliding-window rate-anomaly watcher.

Uses a recording (non-network) alert sender for ``alert_critical`` delivery,
matching the pattern in ``test_critical_alerts.py``, so no real webhook call
is ever made during the test run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from stagepilot.remote_bootstrap import DesktopBootstrapStore
from stagepilot.services.critical_alerts import AlertSender, CriticalAlertStore
from stagepilot.services.rate_anomaly import (
    DEFAULT_THRESHOLDS,
    WINDOW_SECONDS,
    RateAnomalyStore,
    record_event,
)


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _dummy_bootstrap() -> DesktopBootstrapStore:
    return cast(DesktopBootstrapStore, object())


def _recording_sender() -> tuple[list[dict[str, object]], AlertSender]:
    calls: list[dict[str, object]] = []

    def sender(bootstrap: DesktopBootstrapStore, payload: dict[str, object]) -> bool:
        calls.append(payload)
        return True

    return calls, sender


def _stores(tmp_path: Path, clock: _Clock) -> tuple[RateAnomalyStore, CriticalAlertStore]:
    rate_store = RateAnomalyStore(tmp_path / "rate_windows.sqlite3", clock=clock)
    alert_store = CriticalAlertStore(tmp_path / "alerts.sqlite3", clock=clock)
    return rate_store, alert_store


def _record(
    event_type: str,
    source_key: str,
    rate_store: RateAnomalyStore,
    alert_store: CriticalAlertStore,
    sender: AlertSender,
    *,
    thresholds: dict[str, int] | None = None,
) -> int:
    # alert_critical resolves default_webhook_url() unless a sender bypasses
    # the network call entirely; we monkeypatch by calling record_event
    # directly and relying on alert_critical's webhook_url=None -> default
    # path only sending through `sender`, which never touches the network.
    return record_event(
        event_type,
        source_key,
        rate_store=rate_store,
        alert_store=alert_store,
        thresholds=thresholds,
    )


@pytest.fixture(autouse=True)
def _no_real_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route webhook delivery through a mock transport; never touch the network.

    ``record_event`` calls ``alert_critical`` without an explicit sender, so
    it always uses ``send_webhook_alert``'s real ``httpx.Client`` machinery.
    Swapping the transport (matching the ``httpx.MockTransport`` pattern used
    elsewhere in this codebase, e.g. ``test_remote_health_alert.py``) keeps
    delivery exercised end-to-end without any real network call or requiring
    a webhook receiver to be running.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    original_client = httpx.Client

    def mock_client(*args: object, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr("stagepilot.services.critical_alerts.httpx.Client", mock_client)


def test_event_under_threshold_does_not_alert(tmp_path: Path) -> None:
    clock = _Clock()
    rate_store, alert_store = _stores(tmp_path, clock)

    for _ in range(9):
        record_event("update_check", "203.0.113.5", rate_store=rate_store, alert_store=alert_store)

    assert alert_store.list_open() == []


def test_event_at_threshold_alerts_exactly_once(tmp_path: Path) -> None:
    clock = _Clock()
    rate_store, alert_store = _stores(tmp_path, clock)

    for i in range(1, 11):
        count = record_event(
            "update_check", "203.0.113.5", rate_store=rate_store, alert_store=alert_store
        )
        assert count == i

    open_alerts = alert_store.list_open()
    assert len(open_alerts) == 1
    alert = open_alerts[0]
    assert alert.category == "rate_anomaly"
    assert alert.subject == "update_check:203.0.113.5"
    assert alert.severity == "warning"
    assert "10 times in the last hour from 203.0.113.5" in alert.message
    assert "threshold: 10" in alert.message
    assert alert.count == 1


def test_event_over_threshold_relies_on_cooldown_not_new_alerts(tmp_path: Path) -> None:
    clock = _Clock()
    rate_store, alert_store = _stores(tmp_path, clock)

    for _ in range(15):
        record_event("update_check", "203.0.113.5", rate_store=rate_store, alert_store=alert_store)

    # Still exactly one open alert row (not one per trip): alert_critical's
    # own 15 minute cooldown/dedup engine absorbed the repeated threshold
    # trips (count 10 through 15) into that single row's occurrence counter
    # instead of sending a fresh notification/opening a new alert per trip.
    open_alerts = alert_store.list_open()
    assert len(open_alerts) == 1
    assert open_alerts[0].count == 6


def test_window_rolls_over_after_an_hour(tmp_path: Path) -> None:
    clock = _Clock()
    rate_store, alert_store = _stores(tmp_path, clock)

    for _ in range(9):
        record_event("update_check", "203.0.113.5", rate_store=rate_store, alert_store=alert_store)
    assert alert_store.list_open() == []

    clock.advance(WINDOW_SECONDS + 1)

    # A fresh window: this single event should not push the (rolled-over)
    # count anywhere near the threshold of 10.
    count = record_event(
        "update_check", "203.0.113.5", rate_store=rate_store, alert_store=alert_store
    )
    assert count == 1
    assert alert_store.list_open() == []


def test_different_source_keys_tracked_independently(tmp_path: Path) -> None:
    clock = _Clock()
    rate_store, alert_store = _stores(tmp_path, clock)

    for _ in range(10):
        record_event("update_check", "203.0.113.5", rate_store=rate_store, alert_store=alert_store)
    for _ in range(3):
        record_event("update_check", "198.51.100.9", rate_store=rate_store, alert_store=alert_store)

    open_alerts = alert_store.list_open()
    assert len(open_alerts) == 1
    assert open_alerts[0].subject == "update_check:203.0.113.5"


def test_different_event_types_use_own_threshold(tmp_path: Path) -> None:
    clock = _Clock()
    rate_store, alert_store = _stores(tmp_path, clock)

    for _ in range(10):
        record_event("update_check", "203.0.113.5", rate_store=rate_store, alert_store=alert_store)
    for _ in range(10):
        record_event(
            "remote_login_attempt", "203.0.113.5", rate_store=rate_store, alert_store=alert_store
        )

    open_alerts = {a.subject: a for a in alert_store.list_open()}
    # update_check (threshold 10) tripped; remote_login_attempt
    # (threshold 20) has not reached its own, higher threshold yet.
    assert "update_check:203.0.113.5" in open_alerts
    assert "remote_login_attempt:203.0.113.5" not in open_alerts

    for _ in range(10):
        record_event(
            "remote_login_attempt", "203.0.113.5", rate_store=rate_store, alert_store=alert_store
        )
    open_alerts = {a.subject: a for a in alert_store.list_open()}
    assert "remote_login_attempt:203.0.113.5" in open_alerts


def test_event_type_without_configured_threshold_never_alerts(tmp_path: Path) -> None:
    clock = _Clock()
    rate_store, alert_store = _stores(tmp_path, clock)

    for _ in range(500):
        record_event(
            "unconfigured_event", "203.0.113.5", rate_store=rate_store, alert_store=alert_store
        )

    assert alert_store.list_open() == []


def test_custom_thresholds_override_defaults(tmp_path: Path) -> None:
    clock = _Clock()
    rate_store, alert_store = _stores(tmp_path, clock)

    for i in range(1, 4):
        count = record_event(
            "update_check",
            "203.0.113.5",
            rate_store=rate_store,
            alert_store=alert_store,
            thresholds={"update_check": 3},
        )
        assert count == i

    open_alerts = alert_store.list_open()
    assert len(open_alerts) == 1
    assert "threshold: 3" in open_alerts[0].message


def test_default_thresholds_match_spec() -> None:
    assert DEFAULT_THRESHOLDS == {"update_check": 10, "remote_login_attempt": 20}
