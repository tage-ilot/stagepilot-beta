import { apiOrigin } from "./api";
import { collectDiagnosticBundle, copyBackendLog } from "./desktop";

/**
 * Shared "send logs to developer" helper used by both `ErrorBoundary` and
 * `CrashLoopAlertDialog` so the log-gathering/upload logic lives in exactly
 * one place.
 *
 * INTEGRATION ASSUMPTIONS (the server-side pieces are being built in
 * parallel tasks; see kanban t_72482465's child tasks):
 *  - Tauri command `collect_diagnostic_bundle` (t_40dd46f5):
 *    `invoke("collect_diagnostic_bundle", { frontendError?: string }) => Promise<string>`
 *    returning a JSON/text diagnostic blob. Implemented via
 *    `collectDiagnosticBundle()` in `./desktop`.
 *  - Control-plane route `POST /v1/installations/:id/diagnostics` (t_48cff096),
 *    authenticated the same way other installation calls to the control
 *    plane are (an `Authorization: Bearer spi_<id>.<signature>` installation
 *    credential). This frontend does not currently have a stored
 *    installation id/credential anywhere in `src/` (grep found none), so the
 *    POST is stubbed behind `DiagnosticsUploadTransport` below -- swap
 *    `defaultTransport` for the real fetch call once an installation
 *    id/credential accessor exists on this branch.
 */

export interface DiagnosticsUploadTransport {
  upload(bundle: string): Promise<void>;
}

// Thin seam over the network call so tests can inject a fake transport and
// so the real transport can be dropped in later without touching callers.
class ControlPlaneDiagnosticsTransport implements DiagnosticsUploadTransport {
  async upload(bundle: string): Promise<void> {
    // NOTE: installationId and the installation bearer credential are not
    // yet exposed anywhere in frontend/src. Once they are (see assumption
    // above), replace this stub with:
    //
    //   const response = await fetch(`${apiOrigin}/v1/installations/${installationId}/diagnostics`, {
    //     method: "POST",
    //     headers: {
    //       "Content-Type": "application/json",
    //       Authorization: `Bearer ${installationCredential}`,
    //     },
    //     body: bundle,
    //   });
    //   if (!response.ok) throw new Error(`Upload failed (${response.status}).`);
    //
    // `apiOrigin` is imported above for exactly that purpose.
    void apiOrigin;
    void bundle;
    throw new Error("Sending logs to the developer isn't wired up yet.");
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
