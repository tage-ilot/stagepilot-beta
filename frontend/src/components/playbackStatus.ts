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
export const playbackMidiDetail = (status: PlaybackConnection) =>
  status.sources.midi.connected && status.active_source === "playback_api"
    ? "MIDI connected - standing by (Playback API is in use)"
    : status.sources.midi.reason;
export const playbackOrderNotice = (status: PlaybackStatusResponse) =>
  status.stale ? "Song order stale — rediscover before starts."
    : status.song_order.length === 0 ? "Song order unknown — discover song order before starts." : null;
