"""Sliding-window rate-anomaly watcher: operator-awareness, not access control.

Watches how often a normal/successful event happens for a given source (e.g.
an ``update_check`` or connection event happening 10x/hour from the same IP)
and raises a critical alert when a per-event-type threshold is exceeded
within the current rolling hour.

This is explicitly NOT a replacement for ``services.remote_auth.RemoteStore``'s
``attempts`` table / ``_throttle()`` mechanism, which stays exactly as-is:
that is a fast-acting, request-blocking limiter (30 global / 5 per-email
within a 5 minute window) enforced *before* a login is allowed to proceed.
This module is a slower-moving, longer-window signal for operator awareness
of anomalous patterns *after* events have already happened successfully; it
never blocks anything, it only informs via ``alert_critical``.

Follows the same "construction does no I/O, connect per call" SQLite pattern
as ``services.remote_auth.RemoteStore`` and ``services.critical_alerts.
CriticalAlertStore``.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from stagepilot.services.critical_alerts import CriticalAlertStore, alert_critical

WINDOW_SECONDS = 60 * 60

# Per-event-type threshold: max occurrences of a given event_type, from the
# same source_key, within a rolling hour, before it's considered anomalous.
# A simple dict is intentional: adding/tuning an event type is a one-line
# change and this is an internal detection-tuning knob, not an
# operator-facing, persisted setting (unlike stagepilot.core.settings, which
# models settings.json-persisted, UI-editable configuration).
#
# - update_check: a normal client polls for updates far less than 10x/hour;
#   10 is a generous "something is clearly wrong/misbehaving" bar.
# - remote_login_attempt: deliberately generous relative to
#   ``RemoteStore._throttle()``'s existing fast-acting limits (30 attempts
#   globally / 5 per-email within a 5 minute window, which already hard-blocks
#   well before this threshold could ever be reached by a single IP hammering
#   logins). This threshold instead answers "is this IP behaving weirdly over
#   a much longer window", a slower/complementary signal, not a duplicate of
#   the existing block.
DEFAULT_THRESHOLDS: dict[str, int] = {
    "update_check": 10,
    "remote_login_attempt": 20,
}


class RateAnomalyStore:
    """SQLite-backed rolling-hour event counter, keyed by (event_type, source_key).

    Construction does no I/O; each method opens and closes its own
    connection, matching ``services.remote_auth.RemoteStore`` and
    ``services.critical_alerts.CriticalAlertStore``.
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
                CREATE TABLE IF NOT EXISTS event_rate_windows (
                    event_type TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    window_start REAL NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (event_type, source_key)
                );
                """
            )
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def record(self, event_type: str, source_key: str) -> int:
        """Increment the rolling-hour count for (event_type, source_key).

        If no window row exists yet, or the existing window started more
        than ``WINDOW_SECONDS`` ago, a fresh window starts at count 1
        (rollover). Otherwise the existing window's count is incremented.
        Returns the count *after* this event is recorded.
        """

        now = self.clock()
        with self._db() as db:
            row = db.execute(
                "SELECT window_start, count FROM event_rate_windows "
                "WHERE event_type=? AND source_key=?",
                (event_type, source_key),
            ).fetchone()
            if row is None or (now - row["window_start"]) >= WINDOW_SECONDS:
                db.execute(
                    "INSERT INTO event_rate_windows "
                    "(event_type, source_key, window_start, count) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(event_type, source_key) "
                    "DO UPDATE SET window_start=excluded.window_start, count=1",
                    (event_type, source_key, now),
                )
                return 1
            new_count = int(row["count"]) + 1
            db.execute(
                "UPDATE event_rate_windows SET count=? WHERE event_type=? AND source_key=?",
                (new_count, event_type, source_key),
            )
            return new_count


def record_event(
    event_type: str,
    source_key: str,
    *,
    rate_store: RateAnomalyStore,
    alert_store: CriticalAlertStore,
    thresholds: dict[str, int] | None = None,
) -> int:
    """Record one occurrence of ``event_type`` from ``source_key``.

    Increments the rolling-hour counter and, when the count exceeds the
    configured per-event-type threshold, raises a "rate_anomaly" critical
    alert via ``alert_critical`` (imported from ``services.critical_alerts``;
    its own 15 minute cooldown/dedup engine prevents repeated trips of the
    same threshold from spamming notifications).

    Event types with no configured threshold are recorded but never trigger
    an alert.

    Returns the count within the current rolling hour after this event.
    """

    active_thresholds = DEFAULT_THRESHOLDS if thresholds is None else thresholds
    count = rate_store.record(event_type, source_key)

    threshold = active_thresholds.get(event_type)
    if threshold is not None and count >= threshold:
        alert_critical(
            alert_store,
            category="rate_anomaly",
            subject=f"{event_type}:{source_key}",
            message=(
                f"{event_type} occurred {count} times in the last hour from "
                f"{source_key} (threshold: {threshold})"
            ),
            severity="warning",
        )
    return count


__all__ = [
    "DEFAULT_THRESHOLDS",
    "WINDOW_SECONDS",
    "RateAnomalyStore",
    "record_event",
]
