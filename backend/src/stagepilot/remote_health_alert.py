"""Notify an operator when the private Remote connector heartbeat goes stale.

This reuses the existing freshness check in ``remote_health.check_status``
(same status file, same staleness threshold) and adds a single side effect:
a webhook POST when the heartbeat is judged unhealthy. It intentionally does
NOT implement a general alerting framework -- one condition, one delivery
path.

Delivery: this host runs Hermes, whose gateway exposes an HTTP webhook
listener (see ``platforms.webhook`` in ~/.hermes/config.yaml) capable of
relaying a message to the operator's paired WhatsApp number. StagePilot has
no first-party Hermes integration and cannot discover or mint that endpoint
on its own, so the webhook URL (and any auth token it requires) is supplied
by the operator via configuration/environment and is never hard-coded or
logged here. See ``docs/heartbeat-alerts.md`` for the exact setup this PR
assumes and the open question about Hermes's local hook shape.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Protocol

import httpx

from stagepilot.remote_health import StatusCheck, check_status


class AlertSender(Protocol):
    def __call__(self, webhook_url: str, payload: dict[str, object]) -> None: ...


def send_alert(
    webhook_url: str,
    payload: dict[str, object],
    *,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> None:
    """POST the alert payload to the operator-configured webhook.

    The payload never includes the webhook URL, tokens, or any connector
    credential -- only the non-sensitive freshness-check result.
    """

    owns_client = client is None
    active = client or httpx.Client(timeout=timeout, trust_env=False)
    try:
        response = active.post(webhook_url, json=payload)
        response.raise_for_status()
    finally:
        if owns_client:
            active.close()


def build_alert_payload(
    result: StatusCheck, *, source: str = "stagepilot-remote-connector"
) -> dict[str, object]:
    return {
        "source": source,
        "event": "connector_heartbeat_stale",
        "state": result.state,
        "age_seconds": round(result.age_seconds, 3) if result.age_seconds is not None else None,
        "detail": result.detail,
    }


def check_and_alert(
    status_path: Path,
    max_age_seconds: float,
    webhook_url: str | None,
    *,
    now: float | None = None,
    sender: AlertSender = send_alert,
) -> StatusCheck:
    """Evaluate the existing freshness check and fire an alert if stale.

    Reuses ``remote_health.check_status`` verbatim -- no new staleness
    threshold or freshness logic is introduced here. When ``webhook_url`` is
    unset, alerting is a no-op (a healthy default for installations that
    haven't opted in) and this is logged only via the returned StatusCheck.
    """

    result = check_status(status_path, max_age_seconds, now=now)
    if not result.healthy and webhook_url:
        sender(webhook_url, build_alert_payload(result))
    return result


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--max-age-seconds", type=float, default=10.0)
    parser.add_argument(
        "--webhook-url",
        default=None,
        help=(
            "Operator-configured local webhook URL that relays a WhatsApp "
            "alert. Prefer the STAGEPILOT_HEARTBEAT_ALERT_WEBHOOK_URL "
            "environment variable over this flag so the URL never appears "
            "in shell history or process listings."
        ),
    )
    args = parser.parse_args()
    if not args.status_file.is_absolute() or args.max_age_seconds <= 0:
        parser.error("Use an absolute status path and a positive maximum age")

    import os

    webhook_url = args.webhook_url or os.environ.get("STAGEPILOT_HEARTBEAT_ALERT_WEBHOOK_URL")
    result = check_and_alert(args.status_file, args.max_age_seconds, webhook_url, now=time.time())
    print(
        json.dumps(
            {
                "healthy": result.healthy,
                "state": result.state,
                "age_seconds": round(result.age_seconds, 3)
                if result.age_seconds is not None
                else None,
                "detail": result.detail,
                "alerted": bool(webhook_url) and not result.healthy,
            }
        )
    )
    if not result.healthy:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
