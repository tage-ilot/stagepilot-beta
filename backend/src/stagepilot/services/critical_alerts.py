"""Critical-error alert store with a 15 minute per-alert-key cooldown/dedup engine.

Foundation for selective, deliberately-chosen-call-site alerting. This module
is intentionally NOT a blanket "forward every error" pipe: callers decide
when ``alert_critical`` is worth invoking. It owns only:

- Persisting alert occurrences (SQLite, one row per distinct ``(category,
  subject)`` key while the alert stays "open").
- Deciding whether a given occurrence should trigger a fresh notification,
  given the 15 minute cooldown and any prior acknowledgement.
- Forwarding fresh notifications to the shared control plane using the
  installation's existing enrolled identity.
- Read/query helpers for a future admin API and web UI (list open alerts,
  fetch by id, acknowledge).

Follows the same "construction does no I/O, connect per call" SQLite pattern
as ``services.remote_auth.RemoteStore``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from stagepilot.remote_bootstrap import DesktopBootstrapStore

COOLDOWN_SECONDS = 15 * 60

STATUS_OPEN = "open"
STATUS_ACKNOWLEDGED = "acknowledged"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CriticalAlert:
    id: int
    category: str
    subject: str
    message: str
    severity: str
    first_seen: float
    last_seen: float
    count: int
    status: str
    last_notified: float | None


@dataclass(frozen=True)
class AlertResult:
    alert: CriticalAlert
    should_notify: bool


class AlertSender(Protocol):
    def __call__(self, bootstrap: DesktopBootstrapStore, payload: dict[str, object]) -> bool: ...


def send_control_plane_alert(
    bootstrap: DesktopBootstrapStore,
    payload: dict[str, object],
    *,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> bool:
    """Forward an alert using this installation's existing enrolled identity.

    A machine without an active enrollment is a normal no-op. Network,
    credential, or configuration failures are logged locally and never escape
    into the alerting call path.
    """

    try:
        active = bootstrap.state().active
        if active is None:
            logger.info("critical alert not forwarded: installation is not enrolled")
            return False
        if active.control_plane_origin not in bootstrap.trusted_origins:
            logger.warning("critical alert not forwarded: control-plane origin is not trusted")
            return False
        credential = bootstrap.credential(active)
        owns_client = client is None
        http = client or httpx.Client(
            base_url=active.control_plane_origin,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )
        try:
            response = http.post(
                f"{active.control_plane_origin}/v1/installations/{active.installation_id}/alerts",
                headers={"authorization": f"Bearer {credential}"},
                json=payload,
            )
            response.raise_for_status()
        finally:
            if owns_client:
                http.close()
    except Exception:
        logger.warning("critical alert could not be forwarded to the control plane", exc_info=True)
        return False
    return True


def build_control_plane_payload(alert: CriticalAlert) -> dict[str, object]:
    """Payload accepted by the control-plane installation alert endpoint."""

    def utc_isoformat(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")

    return {
        "category": alert.category,
        "subject": alert.subject,
        "message": alert.message,
        "severity": alert.severity,
        "first_seen": utc_isoformat(alert.first_seen),
        "last_seen": utc_isoformat(alert.last_seen),
        "count": alert.count,
    }


class CriticalAlertStore:
    """SQLite-backed alert store: cooldown/dedup engine plus read/query helpers.

    Construction does no I/O; each method opens and closes its own
    connection, matching ``services.remote_auth.RemoteStore``.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS critical_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    message TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK(severity IN ('warning', 'critical')),
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    count INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open', 'acknowledged')),
                    last_notified REAL
                );
                CREATE INDEX IF NOT EXISTS idx_critical_alerts_key
                    ON critical_alerts(category, subject, status);
                """
            )
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def _row_to_alert(row: sqlite3.Row) -> CriticalAlert:
        return CriticalAlert(
            id=row["id"],
            category=row["category"],
            subject=row["subject"],
            message=row["message"],
            severity=row["severity"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            count=row["count"],
            status=row["status"],
            last_notified=row["last_notified"],
        )

    def record(self, category: str, subject: str, message: str, severity: str) -> AlertResult:
        """Record an occurrence and decide whether it should notify now.

        - No existing "open" row for ``(category, subject)`` (either none
          exists, or the most recent one was acknowledged/resolved): insert a
          fresh row, notify immediately.
        - An "open" row exists and the cooldown has elapsed since it last
          notified: bump the counter, notify again.
        - An "open" row exists within cooldown: bump the counter, do not
          notify.
        """

        now = self.clock()
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM critical_alerts WHERE category=? AND subject=? AND status=?",
                (category, subject, STATUS_OPEN),
            ).fetchone()
            if row is None:
                cursor = db.execute(
                    "INSERT INTO critical_alerts "
                    "(category, subject, message, severity, first_seen, last_seen, "
                    "count, status, last_notified) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (category, subject, message, severity, now, now, STATUS_OPEN, now),
                )
                new_row = db.execute(
                    "SELECT * FROM critical_alerts WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                return AlertResult(self._row_to_alert(new_row), should_notify=True)

            last_notified = row["last_notified"]
            should_notify = last_notified is None or (now - last_notified) >= COOLDOWN_SECONDS
            if should_notify:
                db.execute(
                    "UPDATE critical_alerts SET count=count+1, last_seen=?, last_notified=?, "
                    "message=?, severity=? WHERE id=?",
                    (now, now, message, severity, row["id"]),
                )
            else:
                db.execute(
                    "UPDATE critical_alerts SET count=count+1, last_seen=?, message=?, "
                    "severity=? WHERE id=?",
                    (now, message, severity, row["id"]),
                )
            updated_row = db.execute(
                "SELECT * FROM critical_alerts WHERE id=?", (row["id"],)
            ).fetchone()
            return AlertResult(self._row_to_alert(updated_row), should_notify=should_notify)

    def list_open(self) -> list[CriticalAlert]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM critical_alerts WHERE status=? ORDER BY last_seen DESC",
                (STATUS_OPEN,),
            ).fetchall()
            return [self._row_to_alert(row) for row in rows]

    def get(self, alert_id: int) -> CriticalAlert | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM critical_alerts WHERE id=?", (alert_id,)).fetchone()
            return self._row_to_alert(row) if row is not None else None

    def acknowledge(self, alert_id: int) -> CriticalAlert | None:
        with self._db() as db:
            db.execute(
                "UPDATE critical_alerts SET status=? WHERE id=?",
                (STATUS_ACKNOWLEDGED, alert_id),
            )
            row = db.execute("SELECT * FROM critical_alerts WHERE id=?", (alert_id,)).fetchone()
            return self._row_to_alert(row) if row is not None else None


def alert_critical(
    store: CriticalAlertStore,
    category: str,
    subject: str,
    message: str,
    severity: str,
    *,
    bootstrap: DesktopBootstrapStore | None = None,
    sender: AlertSender = send_control_plane_alert,
) -> AlertResult:
    """Record an alert and forward fresh notifications to the control plane.

    Cooldown/dedup always runs locally. A missing bootstrap store means the
    installation has no enrolled control-plane identity yet, so forwarding is
    deliberately skipped without affecting the caller.
    """

    result = store.record(category, subject, message, severity)
    if result.should_notify and bootstrap is not None:
        sender(bootstrap, build_control_plane_payload(result.alert))
    return result


__all__ = [
    "COOLDOWN_SECONDS",
    "STATUS_ACKNOWLEDGED",
    "STATUS_OPEN",
    "AlertResult",
    "AlertSender",
    "CriticalAlert",
    "CriticalAlertStore",
    "alert_critical",
    "build_control_plane_payload",
    "send_control_plane_alert",
]
