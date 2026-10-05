import { useCallback, useEffect, useRef, useState } from "react";

import { discoverPlaybackSongOrder, findPlayback, getPlaybackEvents, getPlaybackStatus, getSettings, updatePlaybackSettings, updateSettings } from "../api";
import type { PlaybackMonitorEntry, PlaybackSettingsInput, PlaybackStatusResponse, SettingsResponse } from "../types";

export type PlaybackOperation = "save" | "source" | "scan" | "discover" | null;
export interface PlaybackController {
  status: PlaybackStatusResponse | null;
  events: PlaybackMonitorEntry[];
  error: string | null;
  message: string | null;
  pending: PlaybackOperation;
  save: (settings: PlaybackSettingsInput) => void;
  selectSource: (source: "playback_api" | "real") => void;
  scan: () => void;
  discover: () => void;
}

export function usePlaybackInput(canConfigure: boolean, onSettings: (settings: SettingsResponse) => void): PlaybackController {
  const [status, setStatus] = useState<PlaybackStatusResponse | null>(null);
  const [events, setEvents] = useState<PlaybackMonitorEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState<PlaybackOperation>(null);
  const generation = useRef(0);
  const statusRevision = useRef(0);
  const operation = useRef<PlaybackOperation>(null);
  const scanStarted = useRef<number | null>(null);
  // Poll continuously for failover/failback even when the overall connection stays green.
  // A serialized poll and generation checks prevent overlapping or obsolete responses.
  useEffect(() => {
    const current = ++generation.current;
    let timer: number | undefined;
    operation.current = null;
    scanStarted.current = null;
    setPending(null);
    setStatus(null);
    setEvents([]);
    setError(null);
    setStatusError(null);
    setMessage(null);
    if (!canConfigure) return;
    const poll = async () => {
      const revision = statusRevision.current;
      try {
        const next = await getPlaybackStatus();
        if (generation.current !== current || statusRevision.current !== revision) return;
        setStatus(next);
        setStatusError(null);
        if (scanStarted.current !== null) {
          const elapsed = Date.now() - scanStarted.current;
          if (next.sources.playback_api.connected || next.last_error || elapsed >= 60_000) {
            scanStarted.current = null;
            operation.current = null;
            setPending(null);
            setMessage(next.sources.playback_api.connected
              ? `Found Playback at ${next.host}:${next.port}.`
              : "Playback not found yet. Reconnection continues; Playback remote connections may be off.");
          }
        }
        try {
          const monitor = await getPlaybackEvents();
          if (generation.current === current) setEvents(monitor.events);
        } catch {
          // A monitor error must not hide verified connection status.
        }
      } catch (cause) {
        if (generation.current === current && statusRevision.current === revision) {
          setStatus(null);
          setStatusError(cause instanceof Error ? cause.message : "Playback status unavailable.");
          if (operation.current === "scan" && scanStarted.current !== null) {
            operation.current = null;
            scanStarted.current = null;
            setPending(null);
          }
        }
      } finally {
        if (generation.current === current) timer = window.setTimeout(poll, 750);
      }
    };
    void poll();
    return () => {
      generation.current = current + 1;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [canConfigure]);

  const run = useCallback(async (kind: Exclude<PlaybackOperation, null>, action: () => Promise<void>) => {
    if (!canConfigure || operation.current !== null) return;
    const current = generation.current;
    operation.current = kind;
    ++statusRevision.current;
    setPending(kind);
    setError(null);
    setMessage(null);
    try {
      await action();
    } catch (cause) {
      if (generation.current === current) {
        scanStarted.current = null;
        setError(cause instanceof Error ? cause.message : "Playback operation failed.");
      }
    } finally {
      if (generation.current === current && scanStarted.current === null) {
        operation.current = null;
        setPending(null);
      }
    }
  }, [canConfigure]);

  const save = useCallback((input: PlaybackSettingsInput) => {
    const current = generation.current;
    void run("save", async () => {
      const next = await updatePlaybackSettings(input);
      if (generation.current !== current) return;
      ++statusRevision.current;
      setStatus(next);
      const saved = await getSettings();
      if (generation.current !== current) return;
      onSettings(saved);
      setMessage("Playback connection settings saved.");
    });
  }, [onSettings, run]);

  const selectSource = useCallback((source: "playback_api" | "real") => {
    const current = generation.current;
    void run("source", async () => {
      // Read fresh settings so source changes preserve discovered order and MIDI values.
      const saved = await getSettings();
      if (generation.current !== current) return;
      const next = await updateSettings({ ...saved.settings,
        integration_modes: { ...saved.settings.integration_modes, midi_source: source },
      });
      if (generation.current !== current) return;
      onSettings(next);
      const connection = await getPlaybackStatus();
      if (generation.current !== current) return;
      ++statusRevision.current;
      setStatus(connection);
      setMessage("Playback source saved.");
    });
  }, [onSettings, run]);

  const scan = useCallback(() => {
    const current = generation.current;
    void run("scan", async () => {
      const next = await findPlayback();
      if (generation.current !== current) return;
      ++statusRevision.current;
      setStatus(next);
      scanStarted.current = Date.now();
    });
  }, [run]);

  const discover = useCallback(() => {
    const current = generation.current;
    void run("discover", async () => {
      const next = await discoverPlaybackSongOrder();
      if (generation.current !== current) return;
      ++statusRevision.current;
      setStatus(next);
      if (next.discovery === "failed") throw new Error(next.last_error ?? "Song order discovery failed.");
      const saved = await getSettings();
      if (generation.current !== current) return;
      onSettings(saved);
      setMessage("Song order discovered. Playback returned to its original selection.");
    });
  }, [onSettings, run]);

  return { status, events, error: error ?? statusError, message, pending, save, selectSource, scan, discover };
}
