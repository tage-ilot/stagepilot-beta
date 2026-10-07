import { useEffect, useState } from "react";

import { openExternalUrl } from "../desktop";
import type { PlaybackController } from "../hooks/usePlaybackInput";
import type { PlaybackDraft } from "./PlaybackSaveFooter";
import { playbackActivityText, playbackBannerTone, playbackOrderNotice, scanProgressText, scanSummary, validPlaybackHost } from "./playbackStatus";

const button = "min-h-11 rounded-lg border border-sky-400/30 bg-sky-400/10 px-3.5 py-2.5 text-sm font-semibold text-sky-200 hover:bg-sky-400/20";
const big = "min-h-12 w-full rounded-xl border border-sky-300/40 bg-sky-500 px-5 py-3 text-base font-bold text-white hover:bg-sky-400 sm:w-auto";
const toneClass = {
  connected: "border-emerald-400/30 bg-emerald-400/10 text-emerald-100",
  backup: "border-amber-400/30 bg-amber-400/10 text-amber-100",
  searching: "border-amber-400/30 bg-amber-400/10 text-amber-100",
  idle: "border-white/10 bg-white/5 text-slate-200",
} as const;
const LOCAL_NETWORK_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork";
const CHECKLIST = ["Playback is open on a computer", "Remote Connections is turned on in Playback", "Both computers are on the same network"];

export function PlaybackApiPanel({ playback, draft }: { playback?: PlaybackController; draft?: PlaybackDraft }) {
  const status = playback?.status;
  const [host, setHost] = useState("");
  const [specific, setSpecific] = useState(false);
  const [confirm, setConfirm] = useState(false);
  useEffect(() => setHost(status?.settings.host ?? ""), [status?.settings.host]);

  const scan = status?.scan;
  const scanning = scan?.state === "scanning" || playback?.pending === "scan";
  const running = status?.discovery === "running" || playback?.pending === "discover";
  const apiConnected = status?.sources.playback_api.connected ?? false;
  const trimmed = host.trim();
  const hostOk = validPlaybackHost(trimmed);
  const orderSaved = Boolean(status?.song_order.length) && !status?.stale;
  const notice = status ? playbackOrderNotice(status) : null;
  // A dialog never outlives the state that allowed it (e.g. Playback started playing).
  const canDiscover = apiConnected && !status?.playing && !running;
  useEffect(() => { if (!canDiscover) setConfirm(false); }, [canDiscover]);

  const [copied, setCopied] = useState(false);
  const copyDetails = async (text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(true); } catch { setCopied(false); }
  };
  const connect = () => { if (trimmed && hostOk) playback?.scan(trimmed); };
  const tone = status ? playbackBannerTone(status) : "idle";
  const apiOn = draft?.enabled ?? status?.enabled ?? true;

  return (
    <div className="mt-4 space-y-4">
      <div className={`rounded-xl border p-4 ${toneClass[tone]}`} role="status" aria-live="polite" data-testid="playback-banner">
        <p className="text-base font-semibold">{status ? status.reason : playback?.error ?? "Checking Playback…"}</p>
        {status?.scan.state === "scanning" && <p className="mt-1 text-sm opacity-80" data-testid="scan-progress">{scanProgressText(status.scan)}</p>}
      </div>

      <div className="space-y-3">
        {scanning
          ? <button className={big} type="button" onClick={playback?.cancelScan}>Scanning… (cancel)</button>
          : <button className={big} type="button" onClick={() => playback?.scan()}>{scan?.state === "not_found" ? "Scan again" : "Scan Network"}</button>}
        {scan?.state === "not_found" && !scanning && (scan.error_class === "permission_denied"
          ? (
            <div role="alert" className="rounded-lg border border-rose-400/30 bg-rose-400/[0.06] p-3 text-sm text-slate-200" data-testid="scan-error">
              <p className="font-semibold">macOS is blocking StagePilot's local-network access</p>
              <ol className="mt-2 list-decimal pl-5 space-y-1">
                <li>Quit StagePilot.</li>
                <li>Open System Settings &gt; Privacy &amp; Security &gt; Local Network.</li>
                <li>Turn StagePilot off and on again (if it is not listed, run Scan Network once and click Allow when macOS asks).</li>
                <li>Reopen StagePilot and scan again.</li>
              </ol>
              <div className="mt-2 flex flex-wrap gap-3">
                <button className="min-h-11 text-sky-200 underline" type="button" onClick={() => void openExternalUrl(scan.settings_url ?? LOCAL_NETWORK_URL)}>Open Local Network settings</button>
                {scan.details && <button className="min-h-11 text-sky-200 underline" type="button" onClick={() => void copyDetails(scan.details as string)}>{copied ? "Copied" : "Copy diagnostic details"}</button>}
                {!scan.typed && <button className="min-h-11 text-sky-200 underline" type="button" onClick={() => setSpecific(true)}>Enter address instead</button>}
              </div>
            </div>
          )
          : scan.error_class
          ? (
            <div role="alert" className="rounded-lg border border-rose-400/30 bg-rose-400/[0.06] p-3 text-sm text-slate-200" data-testid="scan-error">
              <p className="font-semibold">{scan.typed ? "Couldn't connect" : "Couldn't scan the network"}</p>
              <p className="mt-1">{scan.reason}</p>
              <p className="mt-1 text-xs text-slate-400">{scanSummary(scan)}</p>
              <div className="mt-2 flex flex-wrap gap-3">
                {scan.settings_url && <button className="min-h-11 text-sky-200 underline" type="button" onClick={() => void openExternalUrl(scan.settings_url as string)}>Open Local Network settings</button>}
                {scan.details && <button className="min-h-11 text-sky-200 underline" type="button" onClick={() => void copyDetails(scan.details as string)}>{copied ? "Copied" : "Copy diagnostic details"}</button>}
                {!scan.typed && <button className="min-h-11 text-sky-200 underline" type="button" onClick={() => setSpecific(true)}>Enter address instead</button>}
              </div>
            </div>
          )
          : (
            <div className="rounded-lg border border-white/10 bg-white/[0.03] p-3 text-sm text-slate-200">
              <p className="font-semibold">Couldn't find Playback</p>
              <ul className="mt-1 list-disc pl-5 text-slate-300">{CHECKLIST.map((line) => <li key={line}>{line}</li>)}</ul>
              {scan.hosts_total ? <p className="mt-1 text-xs text-slate-400">{scanSummary(scan)}</p> : null}
              <button className="mt-2 min-h-11 text-sky-200 underline" type="button" onClick={() => setSpecific(true)}>Enter address instead</button>
            </div>
          ))}
        {scan?.state === "found" && scan.candidates.length > 1 && !scanning && (
          <div className="rounded-lg border border-white/10 bg-white/[0.03] p-3" role="group" aria-label="Choose which Playback">
            <p className="text-sm font-semibold text-slate-100">Choose which Playback</p>
            <ul className="mt-2 grid gap-2">{scan.candidates.map((candidate) => (
              <li key={candidate.host}><button className={`${button} w-full text-left`} type="button" onClick={() => playback?.scan(candidate.host)}>{candidate.name} <span className="text-xs text-slate-400">{candidate.host}</span></button></li>
            ))}</ul>
          </div>
        )}
        {playback?.error && <p role="alert" className="text-sm text-rose-200">{playback.error}</p>}
        {playback?.message && <p role="status" className="text-sm text-sky-200">{playback.message}</p>}
      </div>

      {status && (
        <section className="space-y-2 rounded-xl border border-sky-400/20 bg-sky-400/[0.05] p-4" aria-labelledby="song-order-heading">
          <h3 id="song-order-heading" className="font-bold text-white">Song order</h3>
          {!apiConnected ? <p className="text-sm text-slate-300">Connect to Playback first.</p>
            : running ? <p role="status" className="text-sm text-sky-200">Checking your songs… step {status.progress}. Please wait.</p>
              : status.playing ? <p className="text-sm text-amber-200">Stop Playback to set up song order.</p>
                : <>
                  {orderSaved ? <p className="text-sm text-emerald-200">Song order saved ({status.song_order.length} songs)</p>
                    : <>{notice && <p className="text-sm text-amber-200">{notice}</p>}
                      <p className="text-sm text-slate-300">StagePilot steps through your Playback songs once to learn the order. Playback must be stopped.</p></>}
                  <button className={orderSaved ? button : big} type="button" onClick={() => setConfirm(true)}>{orderSaved ? "Set up again" : "Set up song order"}</button>
                </>}
          {status.discovery === "failed" && status.last_error && <p role="alert" className="text-sm text-rose-200">{status.last_error}</p>}
        </section>
      )}
      {confirm && <div role="dialog" aria-modal="true" aria-labelledby="discover-confirm-heading" className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4">
        <div className="max-w-lg space-y-4 rounded-xl border border-sky-400/30 bg-slate-950 p-5 shadow-2xl">
          <h3 id="discover-confirm-heading" className="text-lg font-bold text-white">Set up song order?</h3>
          <p className="text-sm text-slate-200">StagePilot will step through the songs in Playback, then go back to the song that was selected. Playback must stay stopped. It takes about 2 seconds per song.</p>
          <div className="flex flex-wrap gap-3">
            <button className={button} autoFocus onClick={() => { setConfirm(false); playback?.discover(); }} type="button">Set up song order</button>
            <button className={button} onClick={() => setConfirm(false)} type="button">Cancel</button>
          </div>
        </div>
      </div>}

      <div className="space-y-3 rounded-xl border border-white/10 p-4">
        <label className="flex min-h-11 items-center gap-3 text-sm text-slate-200">
          <input type="checkbox" checked={apiOn} disabled={!status || !draft} onChange={(event) => draft?.setEnabled(event.target.checked)} />
          Connect to Playback automatically
        </label>
        <label className="flex min-h-11 items-center gap-3 text-sm text-slate-200">
          <input type="checkbox" checked={draft?.autoScan ?? true} disabled={!status || !draft} onChange={(event) => draft?.setAutoScan(event.target.checked)} />
          Look for Playback on the network if it isn't on this computer
        </label>
      </div>

      <div>
        <button className="min-h-11 text-sm font-semibold text-slate-200" type="button" aria-expanded={specific} aria-controls="playback-specific" onClick={() => setSpecific((value) => !value)}>{specific ? "▾" : "▸"} Connect a specific computer</button>
        {specific && <form id="playback-specific" className="mt-2 flex flex-wrap items-end gap-3" onSubmit={(event) => { event.preventDefault(); connect(); }}>
          <label className="w-full min-w-0 flex-none text-sm text-slate-300 sm:min-w-48 sm:flex-1">
            Computer name or address
            <input className="mt-1 block min-h-11 w-full rounded-lg border border-white/10 bg-slate-950 px-3 text-white" value={host} placeholder="192.0.2.10" onChange={(event) => setHost(event.target.value)} aria-invalid={!hostOk} />
          </label>
          <button className={button} type="submit" disabled={!trimmed || !hostOk || scanning}>Connect</button>
          {!trimmed && <p className="w-full text-xs text-slate-400">Type an address to connect.</p>}
          {!hostOk && <p role="alert" className="w-full text-sm text-rose-200">Enter a computer name or address like 192.0.2.10 (no http://, port or spaces).</p>}
        </form>}
      </div>

      <details className="border-t border-white/10 pt-3">
        <summary className="min-h-11 cursor-pointer text-sm font-semibold text-slate-200">Recent activity</summary>
        {!playback?.events.length ? <p className="mt-2 text-sm text-slate-400">Nothing yet.</p> : <ul className="mt-2 max-h-56 overflow-auto text-sm text-slate-300" aria-label="Recent activity">
          {[...playback.events].reverse().map(({ event, discovery, at }, index) => {
            const position = event.song_id === null ? -1 : status?.song_order.indexOf(event.song_id) ?? -1;
            const known = !status?.stale && position >= 0 ? position : -1;
            return <li key={`${event.timestamp}-${index}`} className="flex gap-3 py-0.5"><span className="whitespace-nowrap text-slate-400">{at ? new Date(at).toLocaleTimeString() : ""}</span><span>{playbackActivityText(event.type, known)}{discovery ? " (while setting up song order)" : ""}</span></li>;
          })}
        </ul>}
      </details>
    </div>
  );
}
