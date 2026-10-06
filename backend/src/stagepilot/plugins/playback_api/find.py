"""Identify Playback by negotiated protocol AND validated heartbeat; no commands."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from .normalizer import Heartbeat, parse_heartbeat, parse_message
from .ws import WebSocket

DiscoverySource = Literal["manual", "loopback", "lan"]


@dataclass(frozen=True)
class DiscoveryResult:
    host: str
    port: int
    source: DiscoverySource
    heartbeat: Heartbeat


Probe = Callable[[str, int, float], Heartbeat | None]


def probe(host: str, port: int = 8080, timeout: float = 3.0) -> Heartbeat | None:
    deadline = time.monotonic() + timeout
    try:
        ws = WebSocket(host, port, timeout)
        try:
            while time.monotonic() < deadline:
                raw = ws.recv(max(0.001, deadline - time.monotonic()))
                if raw is None:
                    break
                message = parse_message(raw)
                if message and message[0] == "heartbeat":
                    heartbeat = parse_heartbeat(message[1])
                    if heartbeat is not None:
                        return heartbeat
        finally:
            ws.close()
    except (OSError, ValueError):
        pass
    return None


def parse_interfaces(text: str) -> tuple[tuple[str, int], ...]:
    interfaces: list[tuple[str, int]] = []
    for match in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", text):
        interfaces.append((match[1], int(match[2])))
    for match in re.finditer(
        r"inet\s+(\d+\.\d+\.\d+\.\d+)\s+(?:-->\s+\S+\s+)?netmask\s+(\S+)", text
    ):
        try:
            mask = match[2]
            if mask.lower().startswith("0x"):
                mask = str(ipaddress.IPv4Address(int(mask, 16)))
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
            interfaces.append((match[1], prefix))
        except ValueError:
            continue
    return tuple(interfaces)


def usable_networks(interfaces: Iterable[tuple[str, int]]) -> tuple[ipaddress.IPv4Network, ...]:
    networks: list[ipaddress.IPv4Network] = []
    for host, prefix in interfaces:
        try:
            address = ipaddress.IPv4Address(host)
            if (
                address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or not 0 < prefix < 31
            ):
                continue
            # Never scan more than the attached /24, nor outside a narrower subnet.
            network = ipaddress.IPv4Network(f"{host}/{max(prefix, 24)}", strict=False)
            if network not in networks:
                networks.append(network)
        except ValueError:
            continue
    return tuple(networks)


def local_networks() -> tuple[ipaddress.IPv4Network, ...]:
    for command in (["ip", "-o", "-4", "addr", "show"], ["/sbin/ifconfig"], ["ifconfig"]):
        try:
            output = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        interfaces = parse_interfaces(output.stdout)
        if interfaces:
            return usable_networks(interfaces)
    # Windows uses a stdlib subprocess, not a new Python dependency. CIDR comes
    # from the OS; do not guess attached LANs from hostname DNS/default routes.
    script = (
        "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { "
        "'inet ' + $_.IPAddress + '/' + $_.PrefixLength }"
    )
    try:
        output = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return usable_networks(parse_interfaces(output.stdout))
    except (OSError, subprocess.SubprocessError):
        return ()


def find_playback(
    *,
    host_override: str | None = None,
    port: int = 8080,
    auto_scan: bool = True,
    timeout: float = 3.0,
    probe_endpoint: Probe = probe,
    networks: Callable[[], tuple[ipaddress.IPv4Network, ...]] = local_networks,
) -> DiscoveryResult | None:
    """Blocking worker-thread operation; manual override never falls back."""
    if host_override is not None:
        heartbeat = probe_endpoint(host_override, port, timeout)
        return DiscoveryResult(host_override, port, "manual", heartbeat) if heartbeat else None
    heartbeat = probe_endpoint("127.0.0.1", port, timeout)
    if heartbeat:
        return DiscoveryResult("127.0.0.1", port, "loopback", heartbeat)
    if not auto_scan:
        return None
    for network in networks():
        hosts = tuple(str(host) for host in network.hosts())
        with ThreadPoolExecutor(max_workers=32, thread_name_prefix="playback-find") as executor:
            results = executor.map(lambda host: probe_endpoint(host, port, timeout), hosts)
            for host, heartbeat in zip(hosts, results, strict=True):
                if heartbeat:
                    return DiscoveryResult(host, port, "lan", heartbeat)
    return None


@dataclass(frozen=True)
class ScanCandidate:
    name: str
    host: str
    source: DiscoverySource


def computer_name(host: str) -> str:
    """Best-effort friendly name; never raises and falls back to the address."""
    if host == "127.0.0.1":
        return "This computer"
    try:
        name = socket.gethostbyaddr(host)[0].split(".")[0]
    except (OSError, UnicodeError):
        return host
    return name or host


def scan_candidates(
    *,
    host_override: str | None = None,
    port: int = 8080,
    auto_scan: bool = True,
    timeout: float = 3.0,
    probe_endpoint: Probe = probe,
    networks: Callable[[], tuple[ipaddress.IPv4Network, ...]] = local_networks,
    cancel: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
    resolve_name: Callable[[str], str] = computer_name,
) -> list[ScanCandidate]:
    """Blocking: this computer first, then every Playback on the attached LAN."""
    cancel = cancel or threading.Event()
    if host_override is not None:
        hit = probe_endpoint(host_override, port, timeout)
        return [ScanCandidate(resolve_name(host_override), host_override, "manual")] if hit else []
    if probe_endpoint("127.0.0.1", port, timeout):
        return [ScanCandidate(resolve_name("127.0.0.1"), "127.0.0.1", "loopback")]
    if not auto_scan or cancel.is_set():
        return []
    hosts = [str(h) for network in networks() for h in network.hosts()]
    found: list[ScanCandidate] = []
    chunk = 64
    with ThreadPoolExecutor(max_workers=32, thread_name_prefix="playback-scan") as executor:
        for start in range(0, len(hosts), chunk):
            if cancel.is_set():
                break
            batch = hosts[start : start + chunk]
            for host, hit in zip(
                batch, executor.map(lambda h: probe_endpoint(h, port, timeout), batch), strict=True
            ):
                if hit:
                    found.append(ScanCandidate(resolve_name(host), host, "lan"))
            if progress:
                progress(min(start + chunk, len(hosts)), len(hosts))
    return found
