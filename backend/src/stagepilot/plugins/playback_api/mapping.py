"""Receive-only action mapping; discovery and transport remain separate owners."""

from dataclasses import dataclass

from stagepilot.core.actions import ActionDispatcher, ActionOutcome
from stagepilot.core.events import ActionName
from stagepilot.core.state import StateStore
from stagepilot.plugins.playback_api.normalizer import Heartbeat, PlaybackEvent


@dataclass(frozen=True)
class Observation:
    """Queue envelope captured on receipt, never reconstructed on consumption."""

    event: PlaybackEvent
    discovery: bool
    generation: int


class PlaybackMapper:
    def __init__(self, dispatcher: ActionDispatcher, state_store: StateStore) -> None:
        self._dispatcher = dispatcher
        self._state_store = state_store
        self.song_order: tuple[int, ...] = ()
        self.captured_version: int | None = None
        self.setlist_id: int | str | None = None
        self.stale = True
        self.discovery = False
        self._generation = 0
        self._heartbeat: Heartbeat | None = None
        self._invalidated = True

    def install_order(
        self,
        song_order: tuple[int, ...],
        captured_version: int,
        setlist_id: int | str | None = None,
    ) -> None:
        """Install only a complete explicitly discovered order, not learned IDs."""
        if (
            not song_order
            or any(type(song) is not int for song in song_order)
            or len(song_order) > 200
            or len(set(song_order)) != len(song_order)
            or type(captured_version) is not int
        ):
            raise ValueError("Discovery requires unique integer IDs and an integer version.")
        self.song_order = tuple(song_order)
        self.captured_version = captured_version
        self.setlist_id = setlist_id
        self.stale = True  # A fresh authoritative heartbeat must validate the result.
        self._heartbeat = None
        self._generation += 1
        self._invalidated = False

    def set_discovery(self, active: bool) -> None:
        if active != self.discovery:
            self.discovery = active
            self._generation += 1  # Also suppress pre-discovery queued actions.

    def discard_pending(self) -> None:
        """A disconnect drops queued work, without invalidating the saved order."""
        self._generation += 1
        self._heartbeat = None

    @property
    def revision(self) -> int:
        return self._generation

    def observe(
        self,
        heartbeat: Heartbeat | None,
        events: tuple[PlaybackEvent, ...],
        setlist_id: int | str | None = None,
    ) -> tuple[Observation, ...]:
        """Validate status BEFORE queuing the accompanying event batch."""
        if self.setlist_id is not None and setlist_id is not None and setlist_id != self.setlist_id:
            self.stale = True
            self._invalidated = True
            self._generation += 1
        if heartbeat is not None:
            self._heartbeat = heartbeat
            if (
                heartbeat.setlist_version != self.captured_version
                or heartbeat.song_id not in self.song_order
            ):
                self.stale = True
                self._invalidated = True
                self._generation += 1
            elif self.song_order:
                # Only a newly installed order can recover from stale observations.
                # Once invalidated, matching later heartbeats cannot silently repair it.
                if not self._invalidated:
                    self.stale = False
        for event in events:
            if event.song_id is not None and event.song_id not in self.song_order:
                self.stale = True
                self._invalidated = True
                self._generation += 1
        return tuple(Observation(event, self.discovery, self._generation) for event in events)

    async def dispatch(self, observation: Observation) -> ActionOutcome | None:
        event = observation.event
        if observation.discovery or self.discovery or observation.generation != self._generation:
            return None
        if event.type in ("song.paused", "song.stopped"):
            return await self._dispatcher.dispatch(ActionName.STOP_TIMER, source="playback_api")
        if (
            event.type == "transport.reverted"
            and event.reason == "song.started"
            and not event.playing
        ):
            return await self._dispatcher.dispatch(ActionName.STOP_TIMER, source="playback_api")
        if event.type != "song.started" or self.stale or self._heartbeat is None:
            return None
        if event.song_id not in self.song_order:
            return None
        position = self.song_order.index(event.song_id) + 1
        state = await self._state_store.snapshot()
        if self.discovery or self.stale or observation.generation != self._generation:
            return None
        if state.current_song is not None and state.current_song_index == position - 1:
            return await self._dispatcher.dispatch(
                ActionName.RESTART_CURRENT, source="playback_api"
            )
        return await self._dispatcher.dispatch_song_position(position, source="playback_api")
