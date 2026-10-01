import { useEffect, useState } from "react";

import { sendDiagnosticsBundle, copyDiagnosticsLog } from "../diagnostics";

/**
 * Catches uncaught async errors (`window.onerror`, `unhandledrejection`)
 * that an ErrorBoundary cannot see, and renders the same dark
 * "something went wrong, send logs?" alert-dialog used by
 * `CrashLoopAlertDialog.tsx` / `ErrorBoundary.tsx`. Mounted once at the app
 * root alongside `ErrorBoundary`.
 */
export function GlobalErrorListener() {
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [sendMessage, setSendMessage] = useState<string | null>(null);

  useEffect(() => {
    const onWindowError = (event: ErrorEvent) => {
      setError(event.message || "An unexpected error occurred.");
    };
    const onUnhandledRejection = (event: PromiseRejectionEvent) => {
      const reason = event.reason;
      setError(
        reason instanceof Error ? `${reason.name}: ${reason.message}` : String(reason ?? "Unhandled rejection."),
      );
    };
    window.addEventListener("error", onWindowError);
    window.addEventListener("unhandledrejection", onUnhandledRejection);
    return () => {
      window.removeEventListener("error", onWindowError);
      window.removeEventListener("unhandledrejection", onUnhandledRejection);
    };
  }, []);

  if (!error) return null;

  const sendLogs = async () => {
    setSending(true);
    setSendMessage(null);
    const result = await sendDiagnosticsBundle(error);
    if (result.ok) {
      setSending(false);
      setSendMessage(result.message);
      return;
    }
    try {
      const content = await copyDiagnosticsLog();
      await navigator.clipboard.writeText(content);
      setSending(false);
      setSendMessage(`${result.message} Backend log copied instead.`);
    } catch {
      setSending(false);
      setSendMessage(result.message);
    }
  };

  return (
    <div
      className="fixed bottom-6 left-6 z-[110] w-full max-w-sm rounded-2xl border border-rose-300/30 bg-slate-950 p-5 shadow-2xl shadow-black/60"
      onMouseDown={(event) => event.stopPropagation()}
    >
      <div
        aria-describedby="global-error-dialog-message"
        aria-labelledby="global-error-dialog-title"
        aria-live="assertive"
        aria-modal="false"
        role="alertdialog"
        tabIndex={-1}
      >
        <p className="text-xs font-black uppercase tracking-[0.18em] text-rose-300">Something went wrong</p>
        <h2 className="mt-1 text-lg font-bold text-white" id="global-error-dialog-title">
          StagePilot ran into a problem
        </h2>
        <p className="mt-2 text-sm text-slate-300" id="global-error-dialog-message">
          This didn&rsquo;t stop the app, but you can send logs to the developer to help track it down.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            className="rounded-lg border border-white/15 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-white/10"
            onClick={() => setError(null)}
            type="button"
          >
            Dismiss
          </button>
          <button
            className="rounded-lg bg-rose-300 px-3 py-2 text-sm font-bold text-slate-950 hover:bg-rose-200"
            disabled={sending}
            onClick={() => void sendLogs()}
            type="button"
          >
            {sending ? "Sending…" : "Send logs to developer"}
          </button>
        </div>
        {sendMessage && (
          <p aria-live="polite" className="mt-2 text-xs text-slate-400">
            {sendMessage}
          </p>
        )}
      </div>
    </div>
  );
}
