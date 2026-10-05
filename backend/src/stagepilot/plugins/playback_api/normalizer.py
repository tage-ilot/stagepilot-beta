"""Typed wire observations. Song IDs never imply setlist positions."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal

EventType = Literal[
    "state.snapshot",
    "setlist.changed",
    "song.selected",
    "song.changed",
    "song.ended",
    "song.started",
    "song.resumed",
    "song.paused",
    "song.stopped",
    "seek",
    "section.jump",
    "section.loop",
    "fade.out",
    "fade.in",
    "pad.requested",
    "pad.on",
    "pad.off",
    "position.jump",
    "message.unknown",
]


@dataclass(frozen=True)
class Heartbeat:
    song_id: int
    position: float
    playing: bool
    pad: bool
    setlist_version: int | None


@dataclass(frozen=True)
class PlaybackEvent:
    type: EventType
    timestamp: float
    song_id: int | None = None
    position: float | None = None
    previous_song_id: int | None = None
    continues_playing: bool = False
    playing: bool | None = None
    pad: bool | None = None
    setlist_version: int | None = None
    previous_version: int | None = None
    section_id: int | None = None
    active: bool | None = None
    reason: str | None = None
    message_kind: str | None = None


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError:
            return None
        if math.isfinite(number) and number >= 0:
            return number
    return None


def parse_message(raw: str) -> tuple[str, dict[str, object]] | None:
    try:
        decoded: object = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    message = _mapping(decoded)
    if message is None or len(message) != 1:
        return None
    kind, value = next(iter(message.items()))
    body = _mapping(value)
    return (kind, body) if body is not None else None


def parse_heartbeat(body: dict[str, object]) -> Heartbeat | None:
    state = _mapping(body.get("stateData"))
    if state is None:
        return None
    song = integer(state.get("setlistSongID"))
    position = _number(state.get("sequenceTime"))
    transport = _mapping(state.get("sequencerPlayState"))
    pad = _mapping(state.get("padPlayerPlayState"))
    if song is None or position is None or transport is None or pad is None:
        return None
    if set(transport) not in ({"playing"}, {"stopped"}) or set(pad) not in (
        {"playing"},
        {"stopped"},
    ):
        return None
    return Heartbeat(
        song,
        position,
        "playing" in transport,
        "playing" in pad,
        integer(state.get("setlistCloudVersion")),
    )


class Normalizer:
    def __init__(self) -> None:
        self.heartbeat: Heartbeat | None = None
        self._armed = True
        self._selected: tuple[int, float] | None = None
        self._return_timestamp = -math.inf
        self._command_timestamp = -math.inf
        self._heartbeat_timestamp = 0.0

    def feed(self, raw: str, timestamp: float) -> tuple[PlaybackEvent, ...]:
        message = parse_message(raw)
        if message is None:
            return ()
        kind, body = message
        song = self.heartbeat.song_id if self.heartbeat else None
        if kind == "heartbeat":
            current = parse_heartbeat(body)
            return self._heartbeat(current, timestamp) if current else ()
        if kind == "setlistSelectSong":
            selected = integer(body.get("setlistSongID"))
            if selected is None:
                return ()
            self._selected = (selected, timestamp)
            self._armed = True
            return (PlaybackEvent("song.selected", timestamp, selected, reason="select"),)
        if kind == "transportReturnToStart":
            self._return_timestamp = timestamp
            self._armed = True
            return ()
        if kind == "waveformSeek":
            position = _number(body.get("sequenceTime"))
            if position is None:
                return ()
            self._command_timestamp = timestamp
            self._armed = position < 2.0
            return (PlaybackEvent("seek", timestamp, song, position),)
        if kind in ("waveformDoubleTap", "waveformLoop"):
            self._command_timestamp = timestamp
            section = integer(body.get("setlistSongSectionID"))
            active = body.get("active")
            return (
                PlaybackEvent(
                    "section.jump" if kind == "waveformDoubleTap" else "section.loop",
                    timestamp,
                    song,
                    section_id=section,
                    active=active if isinstance(active, bool) else None,
                ),
            )
        if kind == "transportFade":
            direction = integer(body.get("direction"))
            if direction not in (0, 1):
                return ()
            return (PlaybackEvent("fade.out" if direction == 1 else "fade.in", timestamp, song),)
        if kind == "transportPad":
            active = body.get("playing")
            return (
                PlaybackEvent(
                    "pad.requested",
                    timestamp,
                    song,
                    active=active if isinstance(active, bool) else None,
                ),
            )
        if kind == "transportPlay":
            return ()  # Classify only authoritative heartbeat edges.
        return (PlaybackEvent("message.unknown", timestamp, song, message_kind=kind),)

    def _heartbeat(self, current: Heartbeat, ts: float) -> tuple[PlaybackEvent, ...]:
        previous, self.heartbeat = self.heartbeat, current
        previous_ts, self._heartbeat_timestamp = self._heartbeat_timestamp, ts
        if previous is None:
            self._armed = current.position < 2.0
            return (
                PlaybackEvent(
                    "state.snapshot",
                    ts,
                    current.song_id,
                    current.position,
                    playing=current.playing,
                    pad=current.pad,
                    setlist_version=current.setlist_version,
                ),
            )
        events: list[PlaybackEvent] = []
        if current.setlist_version != previous.setlist_version:
            events.append(
                PlaybackEvent(
                    "setlist.changed",
                    ts,
                    setlist_version=current.setlist_version,
                    previous_version=previous.setlist_version,
                )
            )
        changed = current.song_id != previous.song_id
        if changed:
            selected = (
                self._selected is not None
                and self._selected[0] == current.song_id
                and ts - self._selected[1] < 5.0
            )
            self._selected = None
            if not selected:
                if previous.playing:
                    events.append(
                        PlaybackEvent("song.ended", ts, previous.song_id, previous.position)
                    )
                    events.append(
                        PlaybackEvent(
                            "song.changed",
                            ts,
                            current.song_id,
                            previous_song_id=previous.song_id,
                            continues_playing=current.playing,
                            reason="auto-advance",
                        )
                    )
                else:
                    events.append(
                        PlaybackEvent("song.selected", ts, current.song_id, reason="navigation")
                    )
            self._armed = True
        if current.playing and (not previous.playing or changed):
            started = (changed and previous.playing) or (self._armed and current.position < 2.0)
            events.append(
                PlaybackEvent(
                    "song.started" if started else "song.resumed",
                    ts,
                    current.song_id,
                    current.position,
                    reason="auto-advance" if changed and previous.playing else "play",
                )
            )
            self._armed = False
        elif not current.playing and previous.playing and not changed:
            stopped = current.position == 0 and ts - self._return_timestamp < 3.0
            events.append(
                PlaybackEvent(
                    "song.stopped" if stopped else "song.paused",
                    ts,
                    current.song_id,
                    current.position,
                )
            )
        elif (
            not current.playing
            and not previous.playing
            and not changed
            and current.position == 0
            and previous.position > 0
        ):
            self._armed = True
            events.append(PlaybackEvent("song.stopped", ts, current.song_id, 0.0))
        if current.pad != previous.pad:
            events.append(
                PlaybackEvent("pad.on" if current.pad else "pad.off", ts, current.song_id)
            )
        if (
            current.playing
            and previous.playing
            and not changed
            and ts - self._command_timestamp > 2.0
            and abs(current.position - previous.position - (ts - previous_ts)) > 1.5
        ):
            events.append(PlaybackEvent("position.jump", ts, current.song_id, current.position))
        return tuple(events)
