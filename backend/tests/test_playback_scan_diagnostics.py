"""Scan failures are classified, logged without secrets, and reported (never swallowed)."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import socket
import threading
from typing import Any, cast

import httpx
import pytest

from stagepilot.plugins.playback_api import plugin as plugin_module
from stagepilot.plugins.playback_api.find import (
    ProbeFailure,
    ScanReport,
    classify_exception,
    probe_checked,
    scan_network,
)
from stagepilot.plugins.playback_api.normalizer import Heartbeat
from stagepilot.plugins.playback_api.ws import WSClosed, WSHandshakeError

HB = Heartbeat(101, 0, False, False, 1)
NET = (ipaddress.IPv4Network("192.0.2.0/29"),)  # 6 hosts


def oserror(code: int) -> OSError:
    return OSError(code, errno.errorcode[code])


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (oserror(errno.EPERM), "permission_denied"),
        (oserror(errno.EACCES), "permission_denied"),
        (PermissionError("denied"), "permission_denied"),
        (oserror(errno.EHOSTUNREACH), "no_route"),
        (oserror(errno.ENETUNREACH), "no_route"),
        (ConnectionRefusedError(), "refused"),
        (TimeoutError(), "timed_out"),
        (TimeoutError(), "timed_out"),
        (WSHandshakeError("bad"), "not_playback"),
        (WSClosed("closed"), "not_playback"),
        (socket.gaierror(), "invalid_address"),
        (ValueError("invalid Playback host"), "invalid_address"),
        (RuntimeError("boom"), "unexpected_error"),
        (oserror(errno.EIO), "unexpected_error"),
    ],
)
def test_each_exception_maps_to_one_error_class(exc: BaseException, expected: str) -> None:
    assert classify_exception(exc)[0] == expected


def test_not_playback_names_the_failed_step() -> None:
    assert classify_exception(WSHandshakeError("x"))[2] == "handshake"
    assert classify_exception(ProbeFailure("not_playback", "d", "heartbeat"))[2] == "heartbeat"


def test_probe_checked_raises_instead_of_returning_none() -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    # Windows retries a refused loopback connect until the timeout; both are real, classified errors.
    with pytest.raises((ConnectionRefusedError, TimeoutError)) as caught:
        probe_checked("127.0.0.1", port, 0.2)
    assert classify_exception(caught.value)[0] in ("refused", "timed_out")


def make_scan(probe: Any, **kwargs: Any) -> ScanReport:
    defaults: dict[str, Any] = {
        "probe_endpoint": probe,
        "networks": lambda: NET,
        "gateway": lambda: "192.0.2.1",
        "selftest_loopback": lambda: True,
        "selftest_connect": lambda *_: "ok",
        "interface_lookup": lambda: {"192.0.2.2": "en9"},
        "timeout": 0.1,
    }
    defaults.update(kwargs)
    return scan_network(**defaults)


def test_all_hosts_blocked_while_loopback_works_is_permission_denied() -> None:
    def blocked(host: str, port: int, timeout: float) -> Heartbeat | None:
        if host == "127.0.0.1":
            return None
        raise oserror(errno.EHOSTUNREACH if False else errno.EPERM)

    report = make_scan(blocked, selftest_connect=lambda *_: "blocked")
    assert (report.outcome, report.error_class) == ("error", "permission_denied")
    assert report.loopback_ok is True and report.lan_check == "blocked"
    assert report.hosts_probed == 7 and report.error_counts["permission_denied"] == 6


def test_timeouts_everywhere_with_working_network_is_plain_not_found() -> None:
    report = make_scan(lambda *_: None)
    assert report.outcome == "not_found" and report.error_class is None
    assert report.hosts_probed == 7 and report.networks == ["192.0.2.0/29"]
    assert report.error_counts == {"timed_out": 6}
    assert report.interfaces == ["en9"]


def test_all_unexpected_errors_are_reported_with_type_and_message() -> None:
    def broken(host: str, port: int, timeout: float) -> Heartbeat | None:
        if host == "127.0.0.1":
            return None
        raise RuntimeError("pool exploded")

    report = make_scan(broken)
    assert (report.outcome, report.error_class) == ("error", "unexpected_error")
    assert "RuntimeError: pool exploded" in (report.message or "")
    assert len(report.sample_errors) <= 5


def test_found_on_lan_and_loopback_first() -> None:
    calls: list[str] = []

    def probe(host: str, port: int, timeout: float) -> Heartbeat | None:
        calls.append(host)
        return HB if host == "192.0.2.3" else None

    report = make_scan(probe, resolve_name=lambda h: h)
    assert calls[0] == "127.0.0.1"
    assert [c.host for c in report.candidates] == ["192.0.2.3"] and report.outcome == "found"


def test_no_networks_is_no_route() -> None:
    report = make_scan(lambda *_: None, networks=lambda: ())
    assert (report.outcome, report.error_class) == ("error", "no_route")


def test_typed_host_is_one_direct_probe_with_specific_class_and_no_lan_scan() -> None:
    calls: list[str] = []

    def refused(host: str, port: int, timeout: float) -> Heartbeat | None:
        calls.append(host)
        raise ConnectionRefusedError()

    def explode() -> tuple[ipaddress.IPv4Network, ...]:
        raise AssertionError("a typed address must never enumerate networks")

    report = make_scan(refused, host_override="192.0.2.50", networks=explode)
    assert calls == ["192.0.2.50"]
    assert (report.outcome, report.error_class) == ("error", "refused")
    assert report.hosts_total == 1 and report.typed_host == "192.0.2.50"
    assert report.duration >= 0


def test_typed_host_success_and_invalid() -> None:
    ok = make_scan(lambda *_: HB, host_override="192.0.2.50", resolve_name=lambda h: "Example")
    assert ok.outcome == "found" and ok.candidates[0].source == "manual"
    bad = make_scan(lambda *_: HB, host_override="bad host/x")
    assert (bad.outcome, bad.error_class) == ("error", "invalid_address")


def test_cancel_stops_scan() -> None:
    cancel = threading.Event()
    cancel.set()
    assert make_scan(lambda *_: None, cancel=cancel).outcome == "cancelled"


def test_progress_and_phase_callbacks_report_real_counts() -> None:
    seen: list[tuple[int, int]] = []
    phases: list[str] = []
    make_scan(
        lambda *_: None,
        progress=lambda c, t: seen.append((c, t)),
        on_phase=lambda n, c, t: phases.append(n),
    )
    assert seen[-1] == (6, 6) and phases[0] == "This computer"


# ---- plugin integration (status surface + logging) --------------------------------


async def _scan_via_api(scanner: Any, host: str | None = None) -> dict[str, Any]:
    from stagepilot.core.config import Settings
    from stagepilot.main import create_app

    app = create_app(Settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        app.state.runtime.playback_input._scanner = scanner
        body = {"host": host} if host else {}
        started = (await client.post("/api/v1/playback-api/find", json=body)).json()
        assert started["scan"]["state"] == "scanning"  # never flashes straight to not_found
        for _ in range(400):
            data = (await client.get("/api/v1/playback-api/status")).json()
            if data["scan"]["state"] != "scanning":
                diag = await client.post("/api/v1/diagnostics/send", json={"bundle": "{}"})
                data["_diag_status"] = diag.status_code
                data["_last_scan"] = app.state.runtime.playback_input.last_scan
                return cast(dict[str, Any], data)
            await asyncio.sleep(0.02)
    raise AssertionError("scan never finished")


@pytest.mark.parametrize(
    "error_class",
    ["permission_denied", "no_route", "refused", "timed_out", "not_playback", "invalid_address"],
)
async def test_api_surfaces_each_class_and_logs_it(error_class: str) -> None:
    plugin_module.RECENT_LOG.clear()

    def scanner(**kwargs: Any) -> ScanReport:
        return ScanReport(
            outcome="error",
            error_class=error_class,  # type: ignore[arg-type]
            typed_host=kwargs.get("host_override"),
            hosts_probed=1,
            hosts_total=1,
            error_counts={error_class: 1},
        )

    data = await _scan_via_api(scanner, host="192.0.2.77")
    scan = data["scan"]
    assert scan["state"] == "not_found" and scan["error_class"] == error_class
    assert scan["reason"] == plugin_module.SCAN_MESSAGES[error_class]
    assert "Couldn't find Playback" not in scan["reason"]  # never the generic text
    assert scan["typed"] is True and scan["details"]
    assert (scan["settings_url"] is not None) == (error_class == "permission_denied")
    log = "\n".join(plugin_module.RECENT_LOG)
    assert "playback_scan_start" in log and "playback_scan_result" in log and error_class in log


async def test_scanner_exception_is_reported_not_swallowed() -> None:
    plugin_module.RECENT_LOG.clear()

    def scanner(**_: Any) -> ScanReport:
        raise RuntimeError("pool exploded")

    data = await _scan_via_api(scanner)
    assert data["scan"]["error_class"] == "unexpected_error"
    assert data["scan"]["reason"] == plugin_module.SCAN_MESSAGES["unexpected_error"]
    assert "pool exploded" in json.dumps(data["_last_scan"])
    assert "RuntimeError" in "\n".join(plugin_module.RECENT_LOG)


async def test_diagnostics_bundle_carries_last_scan_and_recent_log() -> None:
    from stagepilot.api.diagnostics import with_playback_section

    plugin_module.RECENT_LOG.clear()
    plugin_module.playback_log("playback_scan_result", outcome="not_found", errors={"timed_out": 3})

    class State:
        runtime = type(
            "R", (), {"playback_input": type("P", (), {"last_scan": {"outcome": "x"}})()}
        )()

    request = type("Req", (), {"app": type("A", (), {"state": State()})()})()
    out = json.loads(with_playback_section('{"app_info": {}}', request))
    assert out["playback"]["last_scan"] == {"outcome": "x"}
    assert any("playback_scan_result" in line for line in out["playback"]["recent_log"])


def test_secrets_never_reach_the_recent_log() -> None:
    plugin_module.RECENT_LOG.clear()
    plugin_module.playback_log(
        "x", token="abc123", note="Authorization: Bearer abc123 password=hunter2"
    )
    line = plugin_module.RECENT_LOG[-1]
    assert "abc123" not in line and "hunter2" not in line and "REDACTED" in line


def test_scan_messages_cover_every_class_with_one_next_step() -> None:
    expected = {
        "permission_denied",
        "no_route",
        "refused",
        "timed_out",
        "not_playback",
        "invalid_address",
        "unexpected_error",
    }
    assert expected <= set(plugin_module.SCAN_MESSAGES)
    assert "Privacy & Security > Local Network" in plugin_module.SCAN_MESSAGES["permission_denied"]
    assert plugin_module.LOCAL_NETWORK_SETTINGS_URL.endswith("Privacy_LocalNetwork")
