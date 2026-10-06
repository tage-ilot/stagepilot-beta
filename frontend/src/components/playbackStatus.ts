import type { PlaybackConnection, PlaybackStatusResponse } from "../types";

// The backend accepts a bare host, not a URL. Keep port at the existing configured value.
export function validPlaybackHost(host: string): boolean {
  if (!host) return true;
  if (host.length > 255 || /[\s/?#]/.test(host)) return false;
  if (host.includes(":")) {
    try { new URL(`http://[${host}]/`); return true; }
    catch { return false; }
  }
  if (/^[\d.]+$/.test(host)) {
    const parts = host.split(".");
    return parts.length === 4 && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
  }
  return host.split(".").every((part) => /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/i.test(part) && part.length <= 63);
}

// Shared wording: connection health is not song-order/action readiness.
export const playbackConnectionDetail = (status: PlaybackConnection) => status.reason;
export const playbackOrderNotice = (status: PlaybackStatusResponse) =>
  status.stale && status.song_order.length > 0 ? "Your Playback setlist changed. Set up song order again."
    : status.song_order.length === 0 ? "Song order isn't set up yet." : null;

export type BannerTone = "connected" | "searching" | "backup" | "idle";
// The sentence always comes from the backend `reason`; only the colour is chosen here.
export const playbackBannerTone = (status: PlaybackConnection & { scan?: { state: string } }): BannerTone =>
  status.scan?.state === "scanning" ? "searching"
    : status.active_source === "playback_api" ? "connected"
      : status.active_source === "midi" ? "backup" : "idle";

export const playbackMidiNote = (status: PlaybackConnection) =>
  status.sources.midi.connected
    ? status.active_source === "playback_api" ? "MIDI is connected and standing by." : "MIDI is connected."
    : status.sources.midi.reason;

const EVENT_TEXT: Record<string, string> = {
  "song.started": "started", "song.resumed": "resumed", "song.paused": "Paused", "song.stopped": "Stopped",
  "song.ended": "ended", "song.selected": "selected", "song.changed": "changed",
};
export function playbackActivityText(type: string, position: number): string {
  const verb = EVENT_TEXT[type] ?? type;
  const generic = verb === "Paused" || verb === "Stopped";
  if (generic) return verb;
  return position < 0 ? `A song ${verb}` : `Song ${position + 1} ${verb}`;
}
