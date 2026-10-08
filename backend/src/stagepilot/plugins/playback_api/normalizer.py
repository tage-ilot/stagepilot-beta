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
    "transport.reverted",
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
    provisional: bool = False


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
    def __init__(self, *, fast_transport: bool = False) -> None:
        self.heartbeat: Heartbeat | None = None
        self.setlist_id: int | str | None = None
        self.fast_transport = fast_transport
        self._expected: bool | None = None
        self._expected_song: int | None = None
        self._pending: list[PlaybackEvent] = []
        self._pending_timestamp = 0.0
        self._pending_heartbeats = 0
        self._armed_before = True
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
            if current is None:
                return ()
            armed = self._armed
            events = self._heartbeat(current, timestamp)
            if self._pending:
                self._armed = armed
                return self._settle(current, timestamp, events)
            return events
        if kind == "contentLoadSetlist":
            if self.heartbeat is None:
                return ()  # Identity is known only from loads seen while connected.
            data = _mapping(body.get("setlistData"))
            identity = data.get("setlistID") if data else None
            if isinstance(identity, str) and identity:
                self.setlist_id = identity
            elif integer(identity) is not None:
                self.setlist_id = integer(identity)
            return ()  # Never retain or expose private setlist names.
        if kind == "setlistSelectSong":
            selected = integer(body.get("setlistSongID"))
            if selected is None:
                return ()
            self._selected = (selected, timestamp)
            armed = self._armed
            self._armed = True
            events = (PlaybackEvent("song.selected", timestamp, selected, reason="select"),)
            if self.fast_transport and self.heartbeat and selected != song and self._playing_now():
                events += self._provisional(
                    PlaybackEvent(
                        "song.stopped",
                        timestamp,
                        song,
                        self.heartbeat.position,
                        reason="song-selected",
                        provisional=True,
                    ),
                    False,
                    selected,
                    armed,
                )
            return events
        if kind == "transportReturnToStart":
            self._return_timestamp = timestamp
            armed = self._armed
            self._armed = True
            if (
                self.fast_transport
                and self.heartbeat
                and (self._playing_now() or self._song_position(timestamp)[1] > 0)
            ):
                song, _ = self._song_position(timestamp)
                return self._provisional(
                    PlaybackEvent(
                        "song.stopped",
                        timestamp,
                        song,
                        0.0,
                        reason="return-to-start",
                        provisional=True,
                    ),
                    False,
                    song,
                    armed,
                )
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
            want = body.get("playing")
            if not self.fast_transport or self.heartbeat is None or not isinstance(want, bool):
                return ()
            if want == self._playing_now():
                return ()
            song, position = self._song_position(timestamp)
            armed = self._armed
            started = armed and position < 2.0
            event_type: EventType = (
                ("song.started" if started else "song.resumed") if want else "song.paused"
            )
            if want:
                self._armed = False
            return self._provisional(
                PlaybackEvent(
                    event_type,
                    timestamp,
                    song,
                    position,
                    reason="play" if want else None,
                    provisional=True,
                ),
                want,
                song,
                armed,
            )
        if kind in {
            "transportNextSong",
            "transportPreviousSong",
            "mixerInfiniteLoop",
            "mixerLoop",
            "mixerMuteMIDI",
            "setlistSelectSongTransition",
            "contentUpdateSetlist",
            "audioDeviceChanged",
            "mixerTrackVolume",
            "mixerTrackMute",
            "mixerTrackSolo",
            "transportNavigateToSongMapElementIndex",
        }:
            if kind == "transportNavigateToSongMapElementIndex":
                self._command_timestamp = timestamp
            return ()
        return (PlaybackEvent("message.unknown", timestamp, song, message_kind=kind),)

    def _song_position(self, timestamp: float) -> tuple[int | None, float]:
        if self.heartbeat is None:
            return None, 0.0
        if (
            self._selected is not None
            and self._selected[0] != self.heartbeat.song_id
            and timestamp - self._selected[1] < 5.0
        ):
            return self._selected[0], 0.0
        if self._pending and self._pending[-1].reason == "return-to-start":
            return self.heartbeat.song_id, 0.0
        return self.heartbeat.song_id, self.heartbeat.position

    def _playing_now(self) -> bool:
        return (
            self._expected
            if self._expected is not None
            else bool(self.heartbeat and self.heartbeat.playing)
        )

    def _provisional(
        self, event: PlaybackEvent, expected: bool, song: int | None, armed: bool
    ) -> tuple[PlaybackEvent, ...]:
        if not self._pending:
            self._armed_before = armed
            self._pending_timestamp = event.timestamp
            self._pending_heartbeats = 0
        self._pending.append(event)
        self._expected, self._expected_song = expected, song
        return (event,)

    def _settle(
        self, current: Heartbeat, timestamp: float, events: tuple[PlaybackEvent, ...]
    ) -> tuple[PlaybackEvent, ...]:
        # Start/resume classifications may differ at heartbeat time: both promise playing.
        returning = self._pending[-1].reason == "return-to-start"

        def key(event: PlaybackEvent) -> tuple[str, int | None]:
            if returning and event.type in ("song.stopped", "song.paused"):
                return "stop", event.song_id
            return (
                "play" if event.type in ("song.started", "song.resumed") else event.type,
                event.song_id,
            )

        pending = [key(event) for event in self._pending]
        kept: list[PlaybackEvent] = []
        for event in events:
            match = key(event)
            if match in pending:
                pending.remove(match)
            else:
                kept.append(event)
        if (
            current.playing == self._expected
            and current.song_id == self._expected_song
            and (not returning or current.position == 0)
        ):
            self._pending.clear()
            self._expected = None
        else:
            self._pending_heartbeats += 1
            if self._pending_heartbeats >= 2 and timestamp - self._pending_timestamp >= 3.0:
                started = any(event.type == "song.started" for event in self._pending)
                self._pending.clear()
                self._expected = None
                self._armed = self._armed_before
                kept.append(
                    PlaybackEvent(
                        "transport.reverted",
                        timestamp,
                        current.song_id,
                        current.position,
                        playing=current.playing,
                        reason="song.started" if started else None,
                    )
                )
        return tuple(kept)

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
            if selected and previous.playing and not current.playing:
                events.append(
                    PlaybackEvent(
                        "song.stopped",
                        ts,
                        previous.song_id,
                        previous.position,
                        reason="song-selected",
                    )
                )
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
