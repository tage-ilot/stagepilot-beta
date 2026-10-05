"""Bounded, receive-only Playback WebSocket transport (stdlib RFC 6455)."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
import time
from contextlib import suppress
from typing import Literal

SUBPROTOCOL = "pr-protocol"
MAX_MESSAGE = 1024 * 1024


class WSClosed(OSError):
    """Peer closed, timed out, or sent an invalid frame."""


class WSHandshakeError(OSError):
    """Peer did not negotiate the Playback protocol."""


class WebSocket:
    """Receive-only except the integration's guarded song-order discovery steps."""

    def __init__(self, host: str, port: int = 8080, timeout: float = 5.0) -> None:
        if not host or any(c in host for c in "\r\n /?#"):
            raise ValueError("invalid Playback host")
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buffer = bytearray()
        self._parts = bytearray()
        self._fragmented = False
        self._write_lock = threading.Lock()
        try:
            self._handshake(host, port, timeout)
        except BaseException:
            self._sock.close()
            raise

    def _handshake(self, host: str, port: int, timeout: float) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        authority = f"[{host}]" if ":" in host else host
        self._sock.sendall(
            (
                f"GET / HTTP/1.1\r\nHost: {authority}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: {SUBPROTOCOL}\r\n\r\n"
            ).encode("ascii")
        )
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WSHandshakeError("Playback upgrade timed out")
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSHandshakeError("Playback remote connections may be off")
            self._buffer.extend(chunk)
            if len(self._buffer) > 16384:
                raise WSHandshakeError("oversized upgrade response")
        head, _, tail = self._buffer.partition(b"\r\n\r\n")
        self._buffer = bytearray(tail)
        lines = head.decode("latin1").split("\r\n")
        if lines[0].split()[:2] != ["HTTP/1.1", "101"]:
            raise WSHandshakeError("Playback WebSocket upgrade refused")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator or name.lower() in headers:
                raise WSHandshakeError("invalid upgrade headers")
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(), usedforsecurity=False
            ).digest()
        ).decode("ascii")
        if (
            headers.get("sec-websocket-accept") != expected
            or headers.get("sec-websocket-protocol") != SUBPROTOCOL
            or headers.get("upgrade", "").lower() != "websocket"
            or "upgrade"
            not in {token.strip().lower() for token in headers.get("connection", "").split(",")}
        ):
            raise WSHandshakeError("invalid Playback protocol negotiation")

    def _fill(self, size: int, deadline: float) -> None:
        while len(self._buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(min(65536, size - len(self._buffer)))
            if not chunk:
                raise WSClosed("Playback connection closed")
            self._buffer.extend(chunk)

    def recv(self, timeout: float = 5.0) -> str | None:
        """Preserve partial frames/messages across timeouts; cap frame/message size."""
        deadline = time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() >= deadline:
                    return None
                self._fill(2, deadline)
                first, second = self._buffer[:2]
                opcode, final = first & 15, bool(first & 128)
                if first & 112 or second & 128:
                    raise WSClosed("invalid server frame")
                size, offset = second & 127, 2
                if size == 126:
                    self._fill(4, deadline)
                    size, offset = int.from_bytes(self._buffer[2:4], "big"), 4
                elif size == 127:
                    self._fill(10, deadline)
                    size, offset = int.from_bytes(self._buffer[2:10], "big"), 10
                if size > MAX_MESSAGE or len(self._parts) + size > MAX_MESSAGE:
                    raise WSClosed("oversized Playback message")
                if opcode >= 8 and (not final or size > 125):
                    raise WSClosed("invalid control frame")
                self._fill(offset + size, deadline)
                payload = bytes(self._buffer[offset : offset + size])
                del self._buffer[: offset + size]
                if opcode == 8:
                    raise WSClosed("Playback sent close")
                if opcode == 9:
                    self._control(10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode not in (0, 1) or (opcode == 0) != self._fragmented:
                    raise WSClosed("unexpected Playback frame")
                self._parts.extend(payload)
                self._fragmented = not final
                if final:
                    data = bytes(self._parts)
                    self._parts.clear()
                    try:
                        return data.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise WSClosed("invalid text encoding") from exc
        except TimeoutError:
            return None

    def _control(self, opcode: int, payload: bytes) -> None:
        if opcode not in (8, 10) or len(payload) > 125:
            raise ValueError("only close and pong are permitted")
        mask = os.urandom(4)
        frame = bytes((128 | opcode, 128 | len(payload))) + mask
        frame += bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self._write_lock:
            self._sock.sendall(frame)

    def _discovery_step(self, direction: Literal["previous", "next"]) -> None:
        """Integration must guard confirmation/stopped state before each step."""
        if direction == "previous":
            payload = b'{"transportPreviousSong":{}}'
        elif direction == "next":
            payload = b'{"transportNextSong":{}}'
        else:
            raise ValueError("invalid discovery direction")
        mask = os.urandom(4)
        frame = bytes((129, 128 | len(payload))) + mask
        frame += bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self._write_lock:
            self._sock.sendall(frame)

    def close(self) -> None:
        # Shutdown wakes a blocked receive immediately; no concurrent application writes.
        with suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()
