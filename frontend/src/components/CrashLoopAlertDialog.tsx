import { useEffect, useRef, useState } from "react";

import { copyBackendLog, type BackendCrashLoopDetected } from "../desktop";

const failureKindLabel = (kind: BackendCrashLoopDetected["failure_kind"]): string | null => {
  switch (kind) {
    case "port_occupied":
      return "The backend port is occupied.";
    case "sidecar_missing":
      return "The packaged backend is missing.";
    case "sidecar_exited":
      return "The packaged backend keeps exiting.";
    case "macos_code_signing":
      return "macOS is blocking the packaged backend.";
    case "timeout":
      return "The backend keeps timing out on startup.";
    default:
      return null;
  }
};

/**
 * Non-blocking alert for a repeat backend crash loop. Mounted at the App
 * root (see App.tsx) so it can appear regardless of whether the user is on
 * the startup screen or the live dashboard. Dismissing it never disables
 * backend retry logic; a fresh alert can still fire later per the re-arm
 * rule enforced on the Rust side.
 */
export function CrashLoopAlertDialog({
  crashLoop,
  onDismiss,
}: {
  crashLoop: BackendCrashLoopDetected | null;
  onDismiss: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const [copyMessage, setCopyMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!crashLoop) return;
    setCopyMessage(null);
    dialog.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onDismiss();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [crashLoop, onDismiss]);

  if (!crashLoop) return null;

  const detail = failureKindLabel(crashLoop.failure_kind);

  const copyLog = async () => {
    try {
      const content = await copyBackendLog();
      await navigator.clipboard.writeText(content);
      setCopyMessage("Backend log copied.");
    } catch (error) {
      setCopyMessage(error instanceof Error ? error.message : "Unable to copy the backend log.");
    }
  };

  return (
    <div
      className="fixed bottom-6 right-6 z-[110] w-full max-w-sm rounded-2xl border border-rose-300/30 bg-slate-950 p-5 shadow-2xl shadow-black/60"
      onMouseDown={(event) => event.stopPropagation()}
    >
      <div
        aria-describedby="crash-loop-dialog-message"
        aria-labelledby="crash-loop-dialog-title"
        aria-live="assertive"
        aria-modal="false"
        ref={dialog}
        role="alertdialog"
        tabIndex={-1}
      >
        <p className="text-xs font-black uppercase tracking-[0.18em] text-rose-300">Repeated backend crash</p>
        <h2 className="mt-1 text-lg font-bold text-white" id="crash-loop-dialog-title">
          StagePilot&rsquo;s backend keeps crashing
        </h2>
        {detail && (
          <p className="mt-2 text-sm text-slate-300" id="crash-loop-dialog-message">
            {detail}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            className="rounded-lg border border-white/15 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-white/10"
            onClick={onDismiss}
            type="button"
          >
            Dismiss
          </button>
          <button
            className="rounded-lg bg-rose-300 px-3 py-2 text-sm font-bold text-slate-950 hover:bg-rose-200"
            onClick={() => void copyLog()}
            type="button"
          >
            Copy Log
          </button>
        </div>
        {copyMessage && (
          <p aria-live="polite" className="mt-2 text-xs text-slate-400">
            {copyMessage}
          </p>
        )}
      </div>
    </div>
  );
}
