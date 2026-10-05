import { useEffect, useState } from "react";

import type { PlaybackController } from "../hooks/usePlaybackInput";
import type { PlaybackSettingsInput } from "../types";
import { playbackOrderNotice, validPlaybackHost } from "./playbackStatus";

const button = "min-h-11 rounded-lg border border-sky-400/30 bg-sky-400/10 px-3.5 py-2.5 text-sm font-semibold text-sky-200 hover:bg-sky-400/20 disabled:opacity-40";


export function PlaybackApiPanel({ playback }: { playback?: PlaybackController }) {
  const status = playback?.status;
  const [host, setHost] = useState("");
  const [confirm, setConfirm] = useState(false);
  useEffect(() => setHost(status?.settings.host ?? ""), [status?.settings.host]);
  const valid = validPlaybackHost(host);
  const running = status?.discovery === "running" || playback?.pending === "discover";
  const busy = Boolean(playback?.pending) || running;
  const apiConnected = status?.sources.playback_api.connected ?? false;
  const discoverReason = !status?.selected || !status.enabled ? "Select and enable Playback API first."
    : !apiConnected ? "Connect Playback API first."
      : status.playing ? "Stop Playback first." : running ? "Discovery is running." : null;
  const notice = status ? playbackOrderNotice(status) : null;
  const save = (enabled = status?.settings.enabled ?? true) => {
    if (!status || !valid) return;
    const input: PlaybackSettingsInput = { ...status.settings, enabled, host: host || null };
    playback?.save(input);
  };

  return (
    <div className="mt-4 space-y-3 rounded-xl border border-sky-400/20 bg-sky-400/[0.05] p-4">
      <h3 className="font-bold text-white">Playback API</h3>
      <label className="flex min-h-11 items-center gap-3 text-sm text-slate-200">
        <input type="checkbox" checked={status?.enabled ?? false} disabled={!status || busy || !valid} onChange={(event) => save(event.target.checked)} />
        Playback API on
      </label>
      <p className="text-sm text-slate-300" aria-live="polite">
        {apiConnected ? `Playback API connected to ${status?.host}:${status?.port}` : "Playback API not connected"}
      </p>
      {status?.selected && status.enabled && !apiConnected && <p className="text-sm text-amber-200">Playback remote connections may be off. Enable them in Playback; StagePilot will keep reconnecting.</p>}
      <div className="flex flex-wrap items-end gap-3">
        <label className="w-full min-w-0 flex-none text-sm text-slate-300 sm:min-w-48 sm:flex-1">
          Manual address
          <input className="mt-1 block min-h-11 w-full rounded-lg border border-white/10 bg-slate-950 px-3 text-white" value={host} placeholder="Automatic (this computer, then LAN)" disabled={!status || busy} onChange={(event) => setHost(event.target.value)} aria-invalid={!valid} />
        </label>
        <button className={button} disabled={!status || busy || !valid} onClick={() => save()} type="button">Save address</button>
        <button className={button} disabled={!status?.selected || !status.enabled || busy || host !== (status.settings.host ?? "")} onClick={playback?.scan} type="button">{playback?.pending === "scan" ? "Scanning…" : "Find / Scan"}</button>
      </div>
      <p className="text-xs text-slate-400">A manual address overrides discovery. Leave blank to check this computer (127.0.0.1:8080), then the attached LAN. Save changes before scanning.</p>
      {!valid && <p role="alert" className="text-sm text-rose-200">Enter a host name or IP address, without a URL, port, or spaces.</p>}
      {(playback?.error || status?.last_error) && <p role="alert" className="text-sm text-rose-200">{playback?.error ?? status?.last_error}</p>}
      {playback?.message && <p role="status" className="text-sm text-sky-200">{playback.message}</p>}
      <div className="space-y-2 border-t border-white/10 pt-3">
        <button className={button} disabled={Boolean(discoverReason) || busy} onClick={() => setConfirm(true)} type="button">Discover Song Order</button>
        {discoverReason && <p className="text-xs text-slate-400">{discoverReason}</p>}
        {running && <p role="status" className="text-sm text-sky-200">Discovering song order… {status?.progress ?? 0} steps. StagePilot ignores Playback events while this runs.</p>}
        {notice && <p className="text-sm text-amber-200">{notice}</p>}
        {status?.song_order.length ? <ol className="max-h-40 overflow-auto text-sm text-slate-300" aria-label="Discovered song order">{status.song_order.map((id, index) => <li key={id}>{index + 1} → ID {id}</li>)}</ol> : null}
        <p className="text-xs text-slate-400">Rediscover after changing the setlist or its order, even if Playback reports the same version.</p>
      </div>
      {confirm && <div role="dialog" aria-modal="true" aria-labelledby="discover-confirm-heading" className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4">
        <div className="max-w-lg space-y-4 rounded-xl border border-sky-400/30 bg-slate-950 p-5 shadow-2xl">
          <h3 id="discover-confirm-heading" className="text-lg font-bold text-white">Discover Song Order?</h3>
          <p className="text-sm text-slate-200">This will move through the Playback setlist with Next/Previous, then return to the song that was selected. Playback must be stopped; discovery will not run while a song is playing. StagePilot ignores Playback events while it runs. It takes about 2 seconds per song.</p>
          {discoverReason && <p className="text-sm text-amber-200">{discoverReason}</p>}
          <div className="flex flex-wrap gap-3">
            <button className={button} disabled={Boolean(discoverReason) || busy} onClick={() => { setConfirm(false); playback?.discover(); }} type="button">Discover Song Order</button>
            <button className={button} onClick={() => setConfirm(false)} type="button">Cancel</button>
          </div>
        </div>
      </div>}
      <div className="border-t border-white/10 pt-3">
        <h3 className="text-sm font-semibold text-slate-200">Recent Playback events</h3>
        <p className="text-xs text-slate-400">Time is seconds on the backend's monotonic clock, not wall-clock time. Song numbers follow the discovered order.</p>
        {!playback?.events.length ? <p className="mt-2 text-sm text-slate-400">No Playback events received yet.</p> : <div className="mt-2 max-h-56 overflow-auto"><table className="w-full text-left text-sm text-slate-300">
          <thead><tr><th className="pr-3">Time (s)</th><th className="pr-3">Event</th><th>Song</th></tr></thead>
          <tbody>{[...playback.events].reverse().map(({ event, discovery }, index) => {
            const position = event.song_id === null ? -1 : status?.song_order.indexOf(event.song_id) ?? -1;
            return <tr key={`${event.timestamp}-${index}`}><td className="whitespace-nowrap pr-3">{event.timestamp.toFixed(1)}</td><td className="pr-3">{event.type}{discovery ? " (discovery)" : ""}</td><td>{status?.stale || position < 0 ? "Unknown" : position + 1}</td></tr>;
          })}</tbody>
        </table></div>}
      </div>
    </div>
  );
}
