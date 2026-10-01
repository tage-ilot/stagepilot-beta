import { apiOrigin } from "./api";
import { collectDiagnosticBundle, copyBackendLog } from "./desktop";

/**
 * Shared "send logs to developer" helper used by both `ErrorBoundary` and
 * `CrashLoopAlertDialog` so the log-gathering/upload logic lives in exactly
 * one place.
 *
 * INTEGRATION NOTE: this frontend has no installation id/credential
 * anywhere in `src/` -- that identity only exists on the desktop/local
 * backend side (`DesktopBootstrapStore`, see
 * `backend/src/stagepilot/remote_bootstrap.py`). Rather than exposing that
 * credential to the webview, the upload goes through a local-backend proxy
 * route, `POST {apiOrigin}/api/v1/diagnostics/send`, which forwards the
 * bundle to the control plane's `POST /v1/installations/:id/diagnostics`
 * (t_48cff096) using the installation's own enrolled identity -- the same
 * pattern `services.critical_alerts.send_control_plane_alert` already uses
 * for alert forwarding.
 */

export interface DiagnosticsUploadTransport {
  upload(bundle: string): Promise<void>;
}

// Thin seam over the network call so tests can inject a fake transport and
// so the real transport can be dropped in later without touching callers.
class ControlPlaneDiagnosticsTransport implements DiagnosticsUploadTransport {
  async upload(bundle: string): Promise<void> {
    const response = await fetch(`${apiOrigin}/api/v1/diagnostics/send`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ bundle }),
    });
    if (!response.ok) {
      let detail: string | undefined;
      try {
        const body = (await response.json()) as { detail?: string; message?: string };
        detail = body.detail ?? body.message;
      } catch {
        // Non-JSON error body; fall back to the status-based message below.
      }
      throw new Error(detail ?? `Upload failed (${response.status}).`);
    }
  }
}

let transport: DiagnosticsUploadTransport = new ControlPlaneDiagnosticsTransport();

/** Test-only seam: inject a fake transport instead of the real network call. */
export const setDiagnosticsTransport = (next: DiagnosticsUploadTransport) => {
  transport = next;
};

export const resetDiagnosticsTransport = () => {
  transport = new ControlPlaneDiagnosticsTransport();
};

export interface SendLogsResult {
  ok: boolean;
  message: string;
}

/**
 * Gathers a diagnostic bundle (optionally embedding a frontend error) and
 * uploads it. Used by both ErrorBoundary's crash surface and
 * CrashLoopAlertDialog's "Send to developer" button.
 */
export const sendDiagnosticsBundle = async (frontendError?: string): Promise<SendLogsResult> => {
  try {
    const bundle = await collectDiagnosticBundle(frontendError);
    await transport.upload(bundle);
    return { ok: true, message: "Logs sent to the developer." };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Unable to send logs to the developer.",
    };
  }
};

/** Fallback used by "Copy Log" when sending fails or the app is offline. */
export const copyDiagnosticsLog = async (): Promise<string> => copyBackendLog();
