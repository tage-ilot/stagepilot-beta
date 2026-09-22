import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { openUrl } from "@tauri-apps/plugin-opener";

export type BackendSupervisorStatus = {
  state: "starting" | "ready" | "external" | "failed" | "stopped";
  message: string;
  port: number;
  managed: boolean;
  failure_kind?:
    | "port_occupied"
    | "sidecar_missing"
    | "sidecar_exited"
    | "macos_code_signing"
    | "timeout"
    | null;
  log_path?: string | null;
};

export const backendStartupTitle = (status: BackendSupervisorStatus | null): string => {
  if (!status) return "Connecting to the local backend";
  if (status.state === "ready" || status.state === "external") return "Backend connected";
  if (status.state === "starting") return "Starting the local backend";
  if (status.state === "stopped") return "Backend stopped";
  switch (status.failure_kind) {
    case "port_occupied":
      return "Backend port is occupied";
    case "sidecar_missing":
      return "Packaged backend is missing";
    case "sidecar_exited":
      return "Packaged backend exited";
    case "macos_code_signing":
      return "macOS blocked the packaged backend";
    case "timeout":
      return "Backend startup timed out";
    default:
      return "Backend startup failed";
  }
};

export const desktopBackendStatus = async (): Promise<BackendSupervisorStatus | null> => {
  if (!isTauri()) return null;
  return invoke<BackendSupervisorStatus>("backend_supervisor_status");
};

export const listenForDesktopBackend = async (
  onStatus: (status: BackendSupervisorStatus) => void,
): Promise<UnlistenFn | null> => {
  if (!isTauri()) return null;
  return listen<BackendSupervisorStatus>("stagepilot://backend-status", (event) => {
    onStatus(event.payload);
  });
};

export type BackendCrashLoopDetected = {
  failure_kind:
    | "port_occupied"
    | "sidecar_missing"
    | "sidecar_exited"
    | "macos_code_signing"
    | "timeout"
    | null;
  message: string;
};

/// Mirrors `listenForDesktopBackend` but for the repeat-crash-loop alert.
/// Intentionally independent from the startup-gated backend-status
/// listener so callers can mount it at the app root, outside any
/// dashboardVisible gate, and still receive the event whether the user is
/// on the startup screen or the live dashboard.
export const listenForDesktopBackendCrashLoop = async (
  onCrashLoop: (payload: BackendCrashLoopDetected) => void,
): Promise<UnlistenFn | null> => {
  if (!isTauri()) return null;
  return listen<BackendCrashLoopDetected>(
    "stagepilot://backend-crash-loop-detected",
    (event) => {
      onCrashLoop(event.payload);
    },
  );
};

export const copyBackendLog = async (): Promise<string> => {
  if (!isTauri()) throw new Error("Backend log copy is only available in the desktop app.");
  return invoke<string>("copy_backend_log");
};


export const isDesktopShell = () => isTauri();

export const minimizeDesktopWindow = async () => {
  if (!isTauri()) return;
  await getCurrentWindow().minimize();
};

export const toggleMaximizeDesktopWindow = async () => {
  if (!isTauri()) return;
  await getCurrentWindow().toggleMaximize();
};

export const hideDesktopWindow = async () => {
  if (!isTauri()) return;
  await invoke("hide_application_window");
};

export const restartDesktopBackend = async (): Promise<boolean> => {
  if (!isTauri()) return false;
  await invoke<BackendSupervisorStatus>("restart_managed_backend");
  return true;
};

export const setRemoteAutostart = async (enabled: boolean): Promise<void> => {
  if (!isTauri()) return;
  await invoke("set_remote_autostart", {enabled});
};

export const openExternalUrl = async (url: string): Promise<void> => {
  if (isTauri()) {
    await openUrl(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
};
