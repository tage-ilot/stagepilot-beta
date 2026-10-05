from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest

from stagepilot.plugins.playback_api.client import ConnectionOptions, PlaybackClient, PlaybackStatus
from stagepilot.plugins.playback_api.find import (
    DiscoveryResult,
    find_playback,
    parse_interfaces,
    probe,
    usable_networks,
)
from stagepilot.plugins.playback_api.normalizer import Heartbeat, Normalizer, PlaybackEvent
from stagepilot.plugins.playback_api.ws import MAX_MESSAGE, WebSocket, WSClosed, WSHandshakeError


def heartbeat(song: int = 101, position: float = 0, playing: bool = False, version: int = 1) -> str:
    return json.dumps(
        {
            "heartbeat": {
                "stateData": {
                    "setlistSongID": song,
                    "sequenceTime": position,
                    "sequencerPlayState": {"playing" if playing else "stopped": {}},
                    "padPlayerPlayState": {"stopped": {}},
                    "setlistCloudVersion": version,
                }
            }
        }
    )


def frame(payload: bytes, opcode: int = 1, final: bool = True) -> bytes:
    first = opcode | (128 if final else 0)
    if len(payload) < 126:
        return bytes((first, len(payload))) + payload
    return bytes((first, 126)) + len(payload).to_bytes(2, "big") + payload


@dataclass
class FakeEndpoint:
    port: int
    received: list[tuple[int, bytes]] = field(default_factory=list)
    request: str = ""
    failure: BaseException | None = None
    done: threading.Event = field(default_factory=threading.Event)


@contextmanager
def endpoint(
    payload: bytes, *, protocol: str = "pr-protocol", accept: bool = True
) -> Iterator[FakeEndpoint]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(3)
    fake = FakeEndpoint(listener.getsockname()[1])

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(3)
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(4096)
                fake.request = request.decode()
                headers = dict(
                    line.split(": ", 1) for line in fake.request.split("\r\n")[1:] if ": " in line
                )
                key = headers["Sec-WebSocket-Key"]
                accepted = (
                    base64.b64encode(
                        hashlib.sha1(
                            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(),
                            usedforsecurity=False,
                        ).digest()
                    ).decode()
                    if accept
                    else "incorrect"
                )
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accepted}\r\n"
                        f"Sec-WebSocket-Protocol: {protocol}\r\n\r\n"
                    ).encode()
                    + payload
                )
                while True:
                    header = conn.recv(2)
                    if not header:
                        break
                    while len(header) < 2:
                        header += conn.recv(2 - len(header))
                    size = header[1] & 127
                    assert header[1] & 128
                    mask = conn.recv(4)
                    data = b""
                    while len(data) < size:
                        data += conn.recv(size - len(data))
                    fake.received.append(
                        (
                            header[0] & 15,
                            bytes(value ^ mask[index % 4] for index, value in enumerate(data)),
                        )
                    )
        except BaseException as exc:
            fake.failure = exc
        finally:
            fake.done.set()
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        thread.join(4)
        assert not thread.is_alive()
        if fake.failure:
            raise fake.failure


def test_handshake_fragment_ping_and_no_application_control() -> None:
    raw = heartbeat().encode()
    with endpoint(frame(raw[:20], final=False) + frame(b"ping", 9) + frame(raw[20:], 0)) as fake:
        ws = WebSocket("127.0.0.1", fake.port)
        assert ws.recv() == raw.decode()
        ws.close()
    assert "GET / HTTP/1.1" in fake.request
    assert "Sec-WebSocket-Protocol: pr-protocol" in fake.request
    assert "Authorization" not in fake.request
    assert fake.received == [(10, b"ping")]
    assert not hasattr(ws, "send")


@pytest.mark.parametrize(("protocol", "accept"), [("wrong", True), ("pr-protocol", False)])
def test_handshake_validates_subprotocol_and_accept(protocol: str, accept: bool) -> None:
    with endpoint(b"", protocol=protocol, accept=accept) as fake, pytest.raises(WSHandshakeError):
        WebSocket("127.0.0.1", fake.port)


@pytest.mark.parametrize(
    "payload",
    [
        bytes((129, 127)) + (MAX_MESSAGE + 1).to_bytes(8, "big"),
        frame(b"binary", 2),
        frame(b"", 0),
        frame(b"\xff"),
    ],
)
def test_rejects_invalid_or_unbounded_frames(payload: bytes) -> None:
    with endpoint(payload) as fake:
        ws = WebSocket("127.0.0.1", fake.port)
        try:
            with pytest.raises(WSClosed):
                ws.recv()
        finally:
            ws.close()


def test_probe_requires_valid_heartbeat_and_never_controls() -> None:
    with endpoint(frame(b'{"heartbeat":{"stateData":{}}}') + frame(heartbeat().encode())) as fake:
        assert probe("127.0.0.1", fake.port, 1) == Heartbeat(101, 0, False, False, 1)
    assert fake.received == []
    with endpoint(frame(b'{"other":{}}')) as fake:
        assert probe("127.0.0.1", fake.port, 0.05) is None


def test_discovery_order_manual_override_and_scan_disabled() -> None:
    calls: list[str] = []
    hb = Heartbeat(101, 0, False, False, 1)
    available: set[str] = set()

    def fake_probe(host: str, port: int, timeout: float) -> Heartbeat | None:
        calls.append(host)
        return hb if host in available else None

    def networks() -> tuple[ipaddress.IPv4Network, ...]:
        assert calls[0] == "127.0.0.1"
        return (ipaddress.IPv4Network("192.0.2.8/30"),)

    assert (
        find_playback(host_override="192.0.2.10", probe_endpoint=fake_probe, networks=networks)
        is None
    )
    assert calls == ["192.0.2.10"]
    calls.clear()
    assert find_playback(auto_scan=False, probe_endpoint=fake_probe, networks=networks) is None
    assert calls == ["127.0.0.1"]
    calls.clear()
    available.add("192.0.2.10")
    result = find_playback(probe_endpoint=fake_probe, networks=networks)
    assert result and result.host == "192.0.2.10" and result.source == "lan"
    assert calls[0] == "127.0.0.1"
    calls.clear()
    available.add("127.0.0.1")
    assert find_playback(probe_endpoint=fake_probe, networks=networks) == DiscoveryResult(
        "127.0.0.1", 8080, "loopback", hb
    )
    assert calls == ["127.0.0.1"]


def test_attached_network_bounds_and_interface_parsing() -> None:
    assert parse_interfaces("inet 192.0.2.10/22 inet 192.0.2.11 netmask 0xffffff00") == (
        ("192.0.2.10", 22),
        ("192.0.2.11", 24),
    )
    assert usable_networks(
        [
            ("127.0.0.1", 8),
            ("169.254.1.2", 16),
            ("192.0.2.10", 16),
            ("192.0.2.10", 24),
            ("192.0.2.10", 32),
        ]
    ) == (ipaddress.IPv4Network("192.0.2.0/24"),)


def test_normalizer_snapshot_start_pause_resume_stop_restart_and_transition() -> None:
    norm = Normalizer()
    assert [event.type for event in norm.feed(heartbeat(), 0)] == ["state.snapshot"]
    assert [event.type for event in norm.feed(heartbeat(playing=True), 1)] == ["song.started"]
    norm.feed(heartbeat(position=10, playing=True), 10)
    assert norm.feed(heartbeat(position=10), 11)[0].type == "song.paused"
    assert norm.feed(heartbeat(position=10, playing=True), 12)[0].type == "song.resumed"
    norm.feed('{"transportReturnToStart":{}}', 13)
    assert norm.feed(heartbeat(), 14)[0].type == "song.stopped"
    assert norm.feed(heartbeat(playing=True), 15)[0].type == "song.started"
    events = norm.feed(heartbeat(202, playing=True, version=2), 16)
    assert [event.type for event in events] == [
        "setlist.changed",
        "song.ended",
        "song.changed",
        "song.started",
    ]
    assert events[2].continues_playing
    assert events[3].song_id == 202  # opaque ID, NOT position 2
    assert not hasattr(events[3], "song_number")
    assert norm.feed("[]", 17) == ()
    assert norm.feed('{"heartbeat":{"stateData":{}}}', 18) == ()
    assert norm.heartbeat == Heartbeat(202, 0, True, False, 2)


@pytest.mark.parametrize(
    ("message", "event_type"),
    [
        ('{"setlistSelectSong":{"setlistSongID":202}}', "song.selected"),
        ('{"waveformSeek":{"sequenceTime":0}}', "seek"),
        ('{"waveformDoubleTap":{"setlistSongSectionID":5}}', "section.jump"),
        ('{"waveformLoop":{"setlistSongSectionID":5,"active":true}}', "section.loop"),
        ('{"transportFade":{"direction":1}}', "fade.out"),
        ('{"transportFade":{"direction":0}}', "fade.in"),
        ('{"transportPad":{"playing":true}}', "pad.requested"),
    ],
)
def test_broadcast_commands_are_observed_only(message: str, event_type: str) -> None:
    norm = Normalizer()
    norm.feed(heartbeat(), 0)
    assert norm.feed(message, 1)[0].type == event_type


async def test_client_lifecycle_failure_reconnect_and_snapshot() -> None:
    hb = Heartbeat(101, 0, False, False, 1)
    attempts = 0
    observed: list[PlaybackEvent] = []
    ready = asyncio.Event()

    def finder(options: ConnectionOptions) -> DiscoveryResult | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return None
        return DiscoveryResult("127.0.0.1", options.port, "manual", hb)

    def observer(status: PlaybackStatus, events: tuple[PlaybackEvent, ...]) -> None:
        observed.extend(events)
        if status.connected:
            ready.set()

    with endpoint(frame(heartbeat(playing=True).encode())) as fake:
        client = PlaybackClient(
            ConnectionOptions(host_override="127.0.0.1", port=fake.port),
            finder=finder,
            observer=observer,
            reconnect_delays=(0.01,),
            heartbeat_timeout=0.5,
        )
        await client.start()
        await asyncio.wait_for(ready.wait(), 2)
        assert attempts == 2
        assert client.status.connected
        assert [event.type for event in observed] == ["state.snapshot"]
        assert client.events == tuple(observed)
        with pytest.raises(ValueError, match="Stop Playback"):
            await client._discovery_step("next")
        await client.stop()
        assert not client.status.connected
        await client.reconfigure(ConnectionOptions(enabled=False))
        assert not client.status.connected
    assert fake.received == []


async def test_stop_interrupts_reconnect_backoff() -> None:
    called = asyncio.Event()
    loop = asyncio.get_running_loop()

    def finder(options: ConnectionOptions) -> DiscoveryResult | None:
        loop.call_soon_threadsafe(called.set)
        return None

    client = PlaybackClient(finder=finder, reconnect_delays=(15,))
    await client.start()
    await asyncio.wait_for(called.wait(), 1)
    await asyncio.wait_for(client.stop(), 1)
    assert not client.status.connected


def test_partial_frame_timeout_keeps_buffer_and_fragment_state() -> None:
    ws = object.__new__(WebSocket)
    reader, writer = socket.socketpair()
    ws._sock = reader
    ws._buffer = bytearray()
    ws._parts = bytearray()
    ws._fragmented = False
    ws._write_lock = threading.Lock()
    try:
        writer.sendall(frame(b"first", final=False) + b"\x80")
        assert ws.recv(0.01) is None
        assert ws._fragmented
        writer.sendall(b"\x06second")
        assert ws.recv(1) == "firstsecond"
    finally:
        ws.close()
        writer.close()


def test_only_narrow_discovery_payloads_exist() -> None:
    with endpoint(frame(heartbeat().encode())) as fake:
        ws = WebSocket("127.0.0.1", fake.port)
        assert ws.recv() == heartbeat()
        ws._discovery_step("previous")
        ws._discovery_step("next")
        ws.close()
    assert fake.received == [(1, b'{"transportPreviousSong":{}}'), (1, b'{"transportNextSong":{}}')]


@pytest.mark.parametrize(
    "raw",
    [
        '{"heartbeat":{"stateData":{"setlistSongID":true}}}',
        '{"heartbeat":null}',
        '{"heartbeat":{},"transportPlay":{}}',
        '{"waveformSeek":{"sequenceTime":NaN}}',
        '{"waveformSeek":{"sequenceTime":-1}}',
    ],
)
def test_malformed_wire_input_never_changes_heartbeat(raw: str) -> None:
    norm = Normalizer()
    norm.feed(heartbeat(), 0)
    before = norm.heartbeat
    assert norm.feed(raw, 1) == ()
    assert norm.heartbeat is before


async def test_client_disconnect_reconnect_is_new_snapshot() -> None:
    hb = Heartbeat(101, 0, False, False, 1)
    ready = asyncio.Event()
    received: list[PlaybackEvent] = []
    attempts = 0

    with (
        endpoint(frame(heartbeat().encode()) + frame(b"", 8)) as first,
        endpoint(frame(heartbeat(202, playing=True).encode())) as second,
    ):

        def finder(options: ConnectionOptions) -> DiscoveryResult:
            nonlocal attempts
            attempts += 1
            return DiscoveryResult(
                "127.0.0.1", first.port if attempts == 1 else second.port, "manual", hb
            )

        def observer(status: PlaybackStatus, events: tuple[PlaybackEvent, ...]) -> None:
            received.extend(events)
            if status.heartbeat and status.heartbeat.song_id == 202:
                ready.set()

        client = PlaybackClient(finder=finder, observer=observer, reconnect_delays=(0.01,))
        await client.start()
        await asyncio.wait_for(ready.wait(), 2)
        assert [event.type for event in received] == ["state.snapshot", "state.snapshot"]
        assert client.status.host == "127.0.0.1"
        await client.stop()
    assert first.received == second.received == []
