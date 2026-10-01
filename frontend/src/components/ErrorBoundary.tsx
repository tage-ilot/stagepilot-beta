import { Component, type ErrorInfo, type ReactNode } from "react";

import { sendDiagnosticsBundle, copyDiagnosticsLog } from "../diagnostics";

interface ErrorBoundaryState {
  error: Error | null;
  sendMessage: string | null;
  sending: boolean;
}

/**
 * Top-level render/lifecycle error boundary, mounted around the app root
 * in App.tsx. Shares CrashLoopAlertDialog's dark alert-dialog visual style
 * (see that component) and the same log-gathering helper
 * (`../diagnostics`), so there is one place that knows how to collect and
 * upload a diagnostic bundle.
 *
 * `window.onerror` / `unhandledrejection` are handled separately (see
 * `installGlobalErrorHandlers` below) since a boundary can't catch those;
 * both paths render through this same component so there is one surface.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, sendMessage: null, sending: false };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("StagePilot render error", error, info.componentStack);
  }

  reload = () => {
    window.location.reload();
  };

  sendLogs = async () => {
    this.setState({ sending: true, sendMessage: null });
    const result = await sendDiagnosticsBundle(
      this.state.error ? `${this.state.error.name}: ${this.state.error.message}` : undefined,
    );
    if (result.ok) {
      this.setState({ sending: false, sendMessage: result.message });
      return;
    }
    try {
      const content = await copyDiagnosticsLog();
      await navigator.clipboard.writeText(content);
      this.setState({ sending: false, sendMessage: `${result.message} Backend log copied instead.` });
    } catch {
      this.setState({ sending: false, sendMessage: result.message });
    }
  };

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div
        className="fixed inset-0 z-[120] grid place-items-center bg-slate-950/80 px-6"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div
          aria-describedby="error-boundary-message"
          aria-labelledby="error-boundary-title"
          aria-live="assertive"
          aria-modal="true"
          className="w-full max-w-sm rounded-2xl border border-rose-300/30 bg-slate-950 p-5 shadow-2xl shadow-black/60"
          role="alertdialog"
          tabIndex={-1}
        >
          <p className="text-xs font-black uppercase tracking-[0.18em] text-rose-300">Something went wrong</p>
          <h2 className="mt-1 text-lg font-bold text-white" id="error-boundary-title">
            StagePilot ran into a problem
          </h2>
          <p className="mt-2 text-sm text-slate-300" id="error-boundary-message">
            Reloading usually fixes this. You can also send logs to the developer to help track it down.
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <button
              className="rounded-lg border border-white/15 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-white/10"
              disabled={this.state.sending}
              onClick={() => void this.sendLogs()}
              type="button"
            >
              {this.state.sending ? "Sending…" : "Send logs to developer"}
            </button>
            <button
              className="rounded-lg bg-rose-300 px-3 py-2 text-sm font-bold text-slate-950 hover:bg-rose-200"
              onClick={this.reload}
              type="button"
            >
              Reload StagePilot
            </button>
          </div>
          {this.state.sendMessage && (
            <p aria-live="polite" className="mt-2 text-xs text-slate-400">
              {this.state.sendMessage}
            </p>
          )}
        </div>
      </div>
    );
  }
}
