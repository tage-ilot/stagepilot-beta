"""Fail closed when the private Remote connector heartbeat is stale."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import NamedTuple


class StatusCheck(NamedTuple):
    healthy: bool
    state: str
    age_seconds: float | None
    detail: str


def check_status(
    path: Path, max_age_seconds: float = 10.0, now: float | None = None
) -> StatusCheck:
    """Validate a connector heartbeat without exposing installation credentials."""

    current = time.time() if now is None else now
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise ValueError("status file is unavailable")
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = payload["state"]
        checked_at = float(payload["checked_at"])
        if state not in {"disabled", "reconnecting", "connected", "stopped"}:
            raise ValueError("status state is invalid")
        age = current - checked_at
        if age < 0 or age > max_age_seconds:
            return StatusCheck(False, str(state), age, "connector status heartbeat is stale")
        if state == "stopped":
            return StatusCheck(False, state, age, "connector supervisor is stopped")
        return StatusCheck(True, state, age, "connector status heartbeat is fresh")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return StatusCheck(False, "unknown", None, "connector status heartbeat is invalid")


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--max-age-seconds", type=float, default=10.0)
    args = parser.parse_args()
    if not args.status_file.is_absolute() or args.max_age_seconds <= 0:
        parser.error("Use an absolute status path and a positive maximum age")
    result = check_status(args.status_file, args.max_age_seconds)
    print(
        json.dumps(
            {
                "healthy": result.healthy,
                "state": result.state,
                "age_seconds": round(result.age_seconds, 3)
                if result.age_seconds is not None
                else None,
                "detail": result.detail,
            }
        )
    )
    if not result.healthy:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
