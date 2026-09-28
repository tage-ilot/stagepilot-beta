# Connector heartbeat stale alert (WhatsApp)

StagePilot's private Remote connector already fails closed when its status
heartbeat goes stale (`backend/src/stagepilot/remote_health.py`,
`check_status()`). This adds one thing on top: an outbound webhook POST when
that existing check judges the heartbeat stale, so the operator gets a
WhatsApp notification instead of having to notice a broken tunnel manually.

## What ships in this PR

- `backend/src/stagepilot/remote_health_alert.py`: reuses
  `remote_health.check_status()` verbatim (same status file, same staleness
  threshold -- no new threshold is introduced) and POSTs a small JSON
  payload to an operator-configured webhook URL when the check reports
  unhealthy. When no webhook URL is configured, alerting is a no-op.
- `backend/tests/test_remote_health_alert.py`: staleness triggers the alert,
  freshness does not, no webhook configured does not call the sender, and
  the HTTP call itself is verified against an `httpx.MockTransport` (no real
  network call, no real WhatsApp message sent during tests).

This intentionally does NOT ship a systemd timer/cron entry to invoke the
script periodically -- that's host configuration, out of scope for this
card, and left to the operator (or a sibling systemd/ops card) to wire up,
e.g.:

```
ExecStart=/path/to/venv/bin/python -m stagepilot.remote_health_alert \
  --status-file /path/to/connector-status.json --max-age-seconds 10
```

with `STAGEPILOT_HEARTBEAT_ALERT_WEBHOOK_URL` set in the unit's environment
file (never on the command line, to keep it out of `ps`/shell history).

## Open question for operator review: the Hermes local-hook shape

I could not find a first-party StagePilot-to-Hermes integration in this
codebase, and I deliberately avoided guessing at Hermes internals I can't
verify from inside this repo. From `~/.hermes/config.yaml` on this host:

```yaml
platforms:
  webhook:
    enabled: true
gateway:
  api_server:
    max_concurrent_runs: 10
```

This confirms Hermes's gateway has a webhook platform enabled, but the
config does not show the endpoint path, payload shape, or auth scheme for
that webhook listener (those presumably live in Hermes's own docs/gateway
code, not in this repo). So `remote_health_alert.py` takes the smallest
reasonable approach: a plain `webhook_url` string (from
`STAGEPILOT_HEARTBEAT_ALERT_WEBHOOK_URL` or `--webhook-url`) that the
operator points at whatever local Hermes endpoint actually relays to their
paired WhatsApp number, with a generic JSON payload
(`source`, `event`, `state`, `age_seconds`, `detail` -- no secrets, no
webhook URL echoed back).

**Please confirm/redirect:**
1. The exact Hermes gateway webhook URL and any required auth header/token
   for "relay this message to my paired WhatsApp number" on this host.
2. Whether Hermes expects a specific payload shape (e.g. `{"text": "..."}`)
   rather than the StagePilot-native shape above -- if so I can add a thin
   payload adapter, or the operator's webhook receiver can adapt it.

No secrets (webhook URL or token) are committed to source, logs, or tests.
