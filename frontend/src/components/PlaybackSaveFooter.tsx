import { useCallback, useEffect, useRef, useState } from "react";

import type { PlaybackController } from "../hooks/usePlaybackInput";

export interface PlaybackDraft {
  enabled: boolean;
  autoScan: boolean;
  fastTransport: boolean;
  setEnabled: (value: boolean) => void;
  setAutoScan: (value: boolean) => void;
  setFastTransport: (value: boolean) => void;
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  error: string | null;
  save: () => Promise<boolean>;
  revert: () => void;
}

const SAVED_FLASH_MS = 2_000;

// One draft of every editable setting in the visible Playback panel. Scan Network and
// Connect are instant actions that save themselves; they never read or wait on this draft.
export function usePlaybackDraft(playback?: PlaybackController): PlaybackDraft {
  const status = playback?.status ?? null;
  const savedEnabled = status?.settings.enabled ?? true;
  const savedAutoScan = status?.settings.auto_scan ?? true;
  const savedFastTransport = status?.settings.fast_transport ?? true;
  const [fastTransport, setFastTransportState] = useState(savedFastTransport);
  const [enabled, setEnabledState] = useState(savedEnabled);
  const [autoScan, setAutoScanState] = useState(savedAutoScan);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => setEnabledState(savedEnabled), [savedEnabled]);
  useEffect(() => setAutoScanState(savedAutoScan), [savedAutoScan]);
  useEffect(() => setFastTransportState(savedFastTransport), [savedFastTransport]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const dirty = enabled !== savedEnabled || autoScan !== savedAutoScan || fastTransport !== savedFastTransport;
  const setEnabled = useCallback((value: boolean) => { setSaved(false); setError(null); setEnabledState(value); }, []);
  const setAutoScan = useCallback((value: boolean) => { setSaved(false); setError(null); setAutoScanState(value); }, []);

  const setFastTransport = useCallback((value: boolean) => { setSaved(false); setError(null); setFastTransportState(value); }, []);

  const save = useCallback(async () => {
    if (!status || !playback) return false;
    setError(null);
    setSaving(true);
    try {
      const ok = await playback.save({ ...status.settings, enabled, auto_scan: autoScan, fast_transport: fastTransport });
      if (!ok) {
        setError("Your settings were not saved. Your changes are still here; try again.");
        return false;
      }
      setSaved(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setSaved(false), SAVED_FLASH_MS);
      return true;
    } finally {
      setSaving(false);
    }
  }, [autoScan, enabled, fastTransport, playback, status]);

  const revert = useCallback(() => {
    setEnabledState(savedEnabled);
    setAutoScanState(savedAutoScan);
    setFastTransportState(savedFastTransport);
    setError(null);
  }, [savedAutoScan, savedEnabled, savedFastTransport]);

  return { enabled, autoScan, fastTransport, setEnabled, setAutoScan, setFastTransport, dirty, saving, saved, error, save, revert };
}

const primary = "min-h-11 rounded-lg border border-sky-300/40 bg-sky-500 px-5 py-2.5 text-sm font-semibold text-white hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-40";

export function PlaybackSaveFooter({ draft, playback, midiDirty = false }: {
  draft: PlaybackDraft;
  playback?: PlaybackController;
  midiDirty?: boolean;
}) {
  const reason = draft.error ?? (draft.dirty ? playback?.error ?? null : null);
  return (
    <div className="sticky bottom-0 z-10 -mx-5 mt-5 space-y-2 rounded-b-2xl border-t border-white/10 bg-slate-950/95 px-5 py-3 backdrop-blur" data-testid="playback-save-footer">
      {reason && <p role="alert" className="text-sm text-rose-200">{reason}</p>}
      {midiDirty && <p className="text-sm text-amber-200">You also have unsaved MIDI changes in Alternate connection: MIDI settings. They are saved with Save MIDI settings, not here.</p>}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <button className={primary} type="button" disabled={!draft.dirty || draft.saving || !playback?.status} onClick={() => void draft.save()}>
          {draft.saving ? "Saving…" : "Save settings"}
        </button>
        {draft.dirty && !draft.saving && <span className="text-xs font-semibold text-amber-200">Unsaved changes</span>}
        {draft.dirty && !draft.saving && <button className="min-h-11 text-sm text-slate-300 underline hover:text-white" type="button" onClick={draft.revert}>Discard changes</button>}
        {!draft.dirty && !draft.saving && (draft.saved
          ? <span role="status" className="text-sm font-semibold text-emerald-300">Saved</span>
          : <span className="text-xs text-slate-400">No changes to save</span>)}
      </div>
    </div>
  );
}
