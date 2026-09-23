
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { LOCAL_CAPABILITIES } from "../access/accessState";
import lightsIcon from "../assets/lights-icon-purple.png";
import multitracksIcon from "../assets/multitracks-icon.png";
import planningCenterIcon from "../assets/planning-center-icon.png";
import propresenterIcon from "../assets/propresenter-icon.png";
import stagepilotIcon from "../assets/stagepilot-icon.png";
import { useDelayedHover } from "../hooks/useDelayedHover";
import type {
  AccessCapabilities,
  ActionName,
  ApplicationState,
  ConnectionStatus,
  GeneralSettingsInput,
  HealthResponse,
  LightingCue,
  LightsSettingsInput,
  LightsStatusResponse,
  MidiCueName,
  MidiInputsResponse,
  MidiMonitorMessage,
  MidiSettingsInput,
  PlanningCenterServiceType,
  PlanningCenterSettingsInput,
  PlanningCenterStatusResponse,
  PlanningCenterTestInput,
  ProPresenterSettingsInput,
  ProPresenterStatusResponse,
  SettingsResponse,
  SkippedServiceItem,
  Song,
} from "../types";
import type { UpdaterController } from "../hooks/useUpdater";
import { BackendSetupPanel } from "./BackendSetupPanel";
import { DashboardGrid } from "./dashboard/DashboardGrid";
import { latestActiveError } from "./dashboard/dashboardActiveError";
import {
  buildConnectionCardViews,
  buildReadinessChecks,
  readinessHasError,
  readinessPassed,
} from "./dashboard/dashboardReadiness";
import { LightsSetupPanel } from "./LightsSetupPanel";
import { MidiSetupPanel } from "./MidiSetupPanel";
import { PlanningCenterSetupPanel } from "./PlanningCenterSetupPanel";
import { ProPresenterSetupPanel } from "./ProPresenterSetupPanel";
import { SetupChecklist } from "./SetupChecklist";
import { StatusCard } from "./StatusCard";
import { UpdateAvailableButton } from "./UpdateAvailableButton";
import { UpdateDialog } from "./UpdateDialog";

type ConnectionPanel = "planning-center" | "midi" | "propresenter" | "lights" | "backend";
type HeaderNotification = {
  id: number;
  message: string;
  tone: "error" | "info";
};

const NOTIFICATION_DURATION_MS = 6_000;
const MAX_NOTIFICATION_QUEUE = 2;
// Retain the first-launch implementation for possible future use while keeping
// it disabled in every production configuration state.
const FIRST_LAUNCH_SETUP_ENABLED = false;

const formatDuration = (seconds: number | null | undefined) => {
  if (seconds == null) return "—:——";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
};

const formatTime = (value: string | null | undefined) =>
  value
    ? new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(new Date(value))
    : "No activity yet";

const formatPlanCurrentAsOf = (value: string | null | undefined) => {
  if (!value) return "Load time unavailable";
  const loadedAt = new Date(value);
  if (Number.isNaN(loadedAt.getTime())) return "Load time unavailable";
  const time = `${String(loadedAt.getHours()).padStart(2, "0")}:${String(loadedAt.getMinutes()).padStart(2, "0")}`;
  const date = `${String(loadedAt.getMonth() + 1).padStart(2, "0")}-${String(loadedAt.getDate()).padStart(2, "0")}-${loadedAt.getFullYear()}`;
  return `Current as of ${time} ${date}`;
};

const connectionIcon = (source: string, name: string, compact = false) => (
  <img
    alt=""
    className={`status-card-icon-image h-9 w-9 max-w-none object-cover ${compact ? "scale-110" : "scale-[1.35]"}`}
    data-status-icon={name}
    src={source}
  />
);

function ActionButton({
  action,
  label,
  tone = "neutral",
  disabled,
  onAction,
}: {
  action: ActionName;
  label: string;
  tone?: "green" | "orange" | "red" | "blue" | "neutral";
  disabled: boolean;
  onAction: (action: ActionName) => void;
}) {
  const color = {
    green: "border-emerald-400/40 bg-transparent text-emerald-200 hover:border-emerald-300/70 hover:bg-emerald-400 hover:text-slate-950 active:bg-emerald-500 active:text-slate-950",
    orange: "border-orange-300/40 bg-transparent text-orange-200 hover:border-orange-200/70 hover:bg-orange-300 hover:text-slate-950 active:bg-orange-400 active:text-slate-950",
    red: "border-rose-400/40 bg-transparent text-rose-200 hover:border-rose-300/70 hover:bg-rose-500 hover:text-white active:bg-rose-600 active:text-white",
    blue: "border-blue-600/50 bg-transparent text-blue-200 hover:border-blue-400/70 hover:bg-blue-700 hover:text-white active:bg-blue-800 active:text-white",
    neutral: "border-white/10 bg-white/5 text-slate-200 hover:border-white/20 hover:bg-white/10",
  }[tone];
  return (
    <button
      className={`rounded-lg border px-3.5 py-2.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-40 ${color}`}
      disabled={disabled}
      onClick={() => onAction(action)}
      type="button"
    >
      {label}
    </button>
  );
}

function SongRow({ song, current, next }: { song: Song; current: boolean; next: boolean }) {
  return (
    <li className={`grid grid-cols-[2rem_1fr_auto] items-center gap-3 border-t border-white/5 px-4 py-3 ${current ? "current-song-row" : ""}`}>
      <span className={`grid h-7 w-7 place-items-center rounded text-xs font-bold ${current ? "bg-[#ff6238] text-slate-950" : "bg-white/5 text-slate-500"}`}>
        {song.order}
      </span>
      <div className="min-w-0">
        <p className="truncate font-medium text-slate-100">{song.title}</p>
        <div className="mt-0.5 flex gap-2 text-[0.68rem] font-bold uppercase tracking-wider">
          {current && <span className="text-[#ff805e]">Current · live</span>}
          {next && <span className="text-amber-300">Up next</span>}
          {song.is_generic && <span className="text-amber-300">Generic item</span>}
          {!song.duration_seconds && <span className="text-rose-300">Missing duration</span>}
        </div>
      </div>
      <span className={`font-mono text-sm font-semibold tabular-nums ${song.duration_seconds ? "text-slate-300" : "text-rose-300"}`}>
        {formatDuration(song.duration_seconds)}
      </span>
    </li>
  );
}

function ReferenceItemRow({ item }: { item: SkippedServiceItem }) {
  if (item.reason === "header") {
    return (
      <li className="border-t border-white/[0.04] bg-black/30 px-4 py-2">
        <p className="truncate text-xs font-extrabold uppercase tracking-[0.14em] text-slate-400">
          {item.title}
        </p>
      </li>
    );
  }

  return (
    <li className="grid grid-cols-[2rem_1fr_auto] items-center gap-3 border-t border-white/[0.035] bg-black/20 px-4 py-3">
      <span className="grid h-7 w-7 place-items-center rounded bg-black/25 text-xs font-bold text-slate-700">•</span>
      <div className="min-w-0">
        <p className="truncate font-medium text-slate-500">{item.title}</p>
        {item.description && (
          <p className="mt-0.5 truncate text-xs text-slate-600">{item.description}</p>
        )}
      </div>
      <span className="font-mono text-sm font-semibold tabular-nums text-slate-600">
        {formatDuration(item.duration_seconds)}
      </span>
    </li>
  );
}

export function Dashboard({
  capabilities = LOCAL_CAPABILITIES,
  state,
  health,
  live,
  error,
  actionMessage,
  pendingAction,
  pendingPlanId,
  settings = null,
  settingsError = null,
  settingsMessage = null,
  pendingSettingsOperation = false,
  planningCenterStatus = null,
  planningCenterServiceTypes = [],
  planningCenterError = null,
  planningCenterMessage = null,
  pendingPlanningCenterOperation = null,
  midi,
  midiMessages,
  midiError,
  midiMessage,
  pendingMidiOperation,
  pendingMidiCue,
  propresenter = null,
  propresenterError = null,
  propresenterMessage = null,
  pendingProPresenterOperation = null,
  lights = null,
  lightsError = null,
  lightsMessage = null,
  pendingLightsOperation = null,
  dispatch,
  selectPlan,
  saveGeneralSettings = () => undefined,
  saveMidiSettings = () => undefined,
  testPlanningCenterConnection = () => undefined,
  loadPlanningCenterServiceTypes = () => undefined,
  savePlanningCenter = () => undefined,
  signInPlanningCenterOAuth = () => undefined,
  disconnectPlanningCenter = () => undefined,
  refreshMidi,
  selectMidi,
  simulateMidi,
  saveProPresenter = () => undefined,
  runProPresenterTest = () => undefined,
  refreshProPresenter = () => undefined,
  saveLights = () => undefined,
  refreshLights = () => undefined,
  sendLightingTest = () => undefined,
  saveLightingCues = () => undefined,
  clearAllLightingCues = () => undefined,
  updater,
}: {
  capabilities?: AccessCapabilities;
  state: ApplicationState;
  health: HealthResponse | null;
  live: boolean;
  error: string | null;
  actionMessage: string | null;
  pendingAction: ActionName | null;
  pendingPlanId: string | null;
  settings?: SettingsResponse | null;
  settingsError?: string | null;
  settingsMessage?: string | null;
  pendingSettingsOperation?: boolean;
  planningCenterStatus?: PlanningCenterStatusResponse | null;
  planningCenterServiceTypes?: PlanningCenterServiceType[];
  planningCenterError?: string | null;
  planningCenterMessage?: string | null;
  pendingPlanningCenterOperation?: "test" | "load-types" | "save" | "oauth-sign-in" | "oauth-disconnect" | null;
  midi: MidiInputsResponse | null;
  midiMessages: MidiMonitorMessage[];
  midiError: string | null;
  midiMessage: string | null;
  pendingMidiOperation: "refresh" | "connect" | "disconnect" | null;
  pendingMidiCue: MidiCueName | null;
  propresenter?: ProPresenterStatusResponse | null;
  propresenterError?: string | null;
  propresenterMessage?: string | null;
  pendingProPresenterOperation?: "save" | "test" | "refresh" | null;
  lights?: LightsStatusResponse | null;
  lightsError?: string | null;
  lightsMessage?: string | null;
  pendingLightsOperation?: "save" | "refresh" | "test" | "save-cues" | null;
  dispatch: (action: ActionName) => void;
  selectPlan: (planId: string) => void;
  saveGeneralSettings?: (settings: GeneralSettingsInput) => void;
  saveMidiSettings?: (settings: MidiSettingsInput) => void;
  testPlanningCenterConnection?: (input: PlanningCenterTestInput) => void;
  loadPlanningCenterServiceTypes?: () => void;
  savePlanningCenter?: (
    input: PlanningCenterSettingsInput,
    timezone: string,
  ) => void;
  signInPlanningCenterOAuth?: () => void;
  disconnectPlanningCenter?: () => void;
  refreshMidi: () => void;
  selectMidi: (inputId: string | null) => void;
  simulateMidi: (cue: MidiCueName) => void;
  saveProPresenter?: (settings: ProPresenterSettingsInput) => void;
  runProPresenterTest?: () => void;
  refreshProPresenter?: () => void;
  saveLights?: (settings: LightsSettingsInput) => void;
  refreshLights?: () => void;
  sendLightingTest?: (note: number, velocity: number) => void;
  saveLightingCues?: (song: Song, cues: LightingCue[]) => void;
  clearAllLightingCues?: (songs: Song[]) => void;
  updater?: UpdaterController;
}) {
  const { canOperate, canConfigure } = capabilities;
  const [activeConnection, setActiveConnection] = useState<ConnectionPanel | null>(null);
  const [clockNow, setClockNow] = useState(Date.now());
  const [notificationQueue, setNotificationQueue] = useState<HeaderNotification[]>([]);
  const [statusCompact, setStatusCompact] = useState(() => window.innerWidth <= 1_000);
  const [statusMotionPhase, setStatusMotionPhase] = useState<"idle" | "preparing" | "moving">("idle");
  const readinessHover = useDelayedHover();
  const notificationId = useRef(0);
  const updateButton = useRef<HTMLButtonElement>(null);
  const connectionsRow = useRef<HTMLElement>(null);
  const statusCompactRef = useRef(statusCompact);
  const statusMotionPhaseRef = useRef(statusMotionPhase);
  const statusMotionRects = useRef(new Map<string, DOMRect>());
  const statusMotionTimers = useRef<number[]>([]);
  const statusPreparationAnimations = useRef<Animation[]>([]);
  const previousNotificationSources = useRef({
    action: null as string | null,
    error: null as string | null,
    service: null as string | null,
    update: null as string | null,
  });
  useEffect(() => {
    setClockNow(Date.now());
    if (state.timer.status !== "running" || !state.timer.started_at) return;
    const timer = window.setInterval(() => setClockNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [state.timer.started_at, state.timer.status]);
  useEffect(() => {
    const timer = window.setInterval(() => setClockNow(Date.now()), 5_000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    let secondFrame = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        connectionsRow.current?.classList.add("status-motion-ready");
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
    };
  }, []);
  useEffect(() => {
    statusMotionPhaseRef.current = statusMotionPhase;
  }, [statusMotionPhase]);
  useEffect(() => {
    const clearMotionTimers = () => {
      for (const timer of statusMotionTimers.current) window.clearTimeout(timer);
      statusMotionTimers.current = [];
      for (const animation of statusPreparationAnimations.current) animation.cancel();
      statusPreparationAnimations.current = [];
    };
    const captureMotionRects = () => {
      const next = new Map<string, DOMRect>();
      connectionsRow.current?.querySelectorAll<HTMLElement>("[data-status-motion-part]").forEach((element, index) => {
        next.set(`${index}:${element.dataset.statusMotionPart}`, element.getBoundingClientRect());
      });
      statusMotionRects.current = next;
    };
    const finishMotion = () => {
      statusMotionPhaseRef.current = "idle";
      setStatusMotionPhase("idle");
    };
    const applyMode = (compact: boolean) => {
      captureMotionRects();
      statusCompactRef.current = compact;
      statusMotionPhaseRef.current = "moving";
      setStatusMotionPhase("moving");
      setStatusCompact(compact);
      statusMotionTimers.current.push(window.setTimeout(finishMotion, 295));
    };
    const handleResize = () => {
      const compact = window.innerWidth <= 1_000;
      if (compact && statusMotionPhaseRef.current === "preparing") return;
      if (compact === statusCompactRef.current) {
        if (statusMotionPhaseRef.current === "preparing") {
          clearMotionTimers();
          finishMotion();
        }
        return;
      }
      clearMotionTimers();
      if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false) {
        statusCompactRef.current = compact;
        setStatusCompact(compact);
        finishMotion();
        return;
      }
      if (compact) {
        statusMotionPhaseRef.current = "preparing";
        setStatusMotionPhase("preparing");
        const labels = connectionsRow.current?.querySelectorAll<HTMLElement>(
          ".status-card-title, .status-card-detail",
        ) ?? [];
        if (![...labels].every((label) => typeof label.animate === "function")) {
          statusMotionTimers.current.push(window.setTimeout(() => applyMode(true), 75));
          return;
        }
        const animations = [...labels].map((label) => label.animate(
          [{ opacity: 1 }, { opacity: 0 }],
          { duration: 75, easing: "linear", fill: "forwards" },
        ));
        statusPreparationAnimations.current = animations;
        void Promise.all(animations.map((animation) => animation.finished)).then(() => {
          for (const animation of animations) animation.cancel();
          statusPreparationAnimations.current = [];
          if (statusMotionPhaseRef.current === "preparing" && window.innerWidth <= 1_000) {
            applyMode(true);
          }
        }).catch(() => undefined);
      } else {
        applyMode(false);
      }
    };
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      clearMotionTimers();
    };
  }, []);
  useLayoutEffect(() => {
    const previous = statusMotionRects.current;
    if (!previous.size) return;
    connectionsRow.current?.querySelectorAll<HTMLElement>("[data-status-motion-part]").forEach((element, index) => {
      const before = previous.get(`${index}:${element.dataset.statusMotionPart}`);
      if (!before) return;
      const after = element.getBoundingClientRect();
      const scaleX = after.width ? before.width / after.width : 1;
      const scaleY = after.height ? before.height / after.height : 1;
      const isSurface = element.dataset.statusMotionPart === "surface";
      if (typeof element.animate !== "function") return;
      if (typeof element.getAnimations === "function") {
        element.getAnimations().forEach((animation) => animation.cancel());
      }
      element.animate([
        {
          opacity: isSurface ? 1 : 0.55,
          transform: `translate(${before.left - after.left}px, ${before.top - after.top}px) scale(${scaleX}, ${scaleY})`,
          transformOrigin: "top left",
        },
        { opacity: 1, transform: "none", transformOrigin: "top left" },
      ], { duration: 275, easing: "linear" });
    });
    statusMotionRects.current = new Map();
  }, [statusCompact]);
  const backendStatus: ConnectionStatus = state.application_status === "running" && live
    ? "connected"
    : state.application_status === "error" ? "error" : live ? "connecting" : "disconnected";
  const plan = state.plan;
  const serviceLoad = state.service_load;
  const serviceNotification = serviceLoad.status !== "idle"
    && serviceLoad.status !== "loaded"
    && serviceLoad.status !== "ambiguous"
    ? `${serviceLoad.message ?? "Planning Center plan status changed."}${serviceLoad.is_stale ? " The last successful plan is still displayed as stale." : ""}`
    : null;
  useEffect(() => {
    const previous = previousNotificationSources.current;
    const pending: HeaderNotification[] = [];
    const add = (message: string, tone: HeaderNotification["tone"]) => {
      notificationId.current += 1;
      pending.push({ id: notificationId.current, message, tone });
    };

    if (error && error !== previous.error) add(error, "error");
    if (actionMessage && actionMessage !== error && actionMessage !== previous.action) {
      add(actionMessage, "info");
    }
    if (serviceNotification && serviceNotification !== previous.service) {
      add(serviceNotification, serviceLoad.status === "loading" ? "info" : "error");
    }
    if (updater?.successMessage && updater.successMessage !== previous.update) {
      add(updater.successMessage, "info");
    }

    previousNotificationSources.current = {
      action: actionMessage,
      error,
      service: serviceNotification,
      update: updater?.successMessage ?? null,
    };
    if (pending.length) {
      setNotificationQueue((current) => [...current, ...pending].slice(-MAX_NOTIFICATION_QUEUE));
    }
  }, [actionMessage, error, serviceLoad.status, serviceNotification, updater?.successMessage]);

  const notification = notificationQueue[0] ?? null;
  useEffect(() => {
    if (!notification) return;
    const timeout = window.setTimeout(() => {
      setNotificationQueue((current) => current[0]?.id === notification.id ? current.slice(1) : current);
    }, NOTIFICATION_DURATION_MS);
    return () => window.clearTimeout(timeout);
  }, [notification]);
  const connectionViews = buildConnectionCardViews({
    state,
    stateOnly: !canConfigure,
    settings,
    midi,
    propresenter,
    lights,
  });
  const checks = buildReadinessChecks({
    state,
    stateOnly: !canConfigure,
    settings,
    propresenter,
    live,
    views: connectionViews,
  });
  const ready = readinessPassed(checks);
  const systemError = readinessHasError(checks);
  const EVENT_DISPLAY_WINDOW_MS = 120_000;
  const activity = [...state.recent_events].reverse()
    .filter((event) => clockNow - Date.parse(event.timestamp) <= EVENT_DISPLAY_WINDOW_MS)
    .slice(0, 10);
  const pinnedError = latestActiveError(state);
  const midiDetail = connectionViews.midi.detail;
  const timerDuration = state.timer.duration_seconds ?? state.current_song?.duration_seconds ?? 0;
  const elapsedMilliseconds = state.timer.status === "running" && state.timer.started_at
    ? Math.max(0, clockNow - Date.parse(state.timer.started_at))
    : 0;
  const timerElapsed = Math.min(timerDuration, Math.floor(elapsedMilliseconds / 1_000));
  const timerRemaining = Math.max(
    0,
    timerDuration - Math.ceil(elapsedMilliseconds / 1_000),
  );
  const toggleConnection = (connection: ConnectionPanel) => {
    if (!canConfigure) return;
    setActiveConnection((current) => current === connection ? null : connection);
  };
  const closeConnection = () => setActiveConnection(null);
  const servicePlanEntries = [
    ...(plan?.songs.map((song) => ({
      kind: "song" as const,
      sequence: song.service_sequence ?? song.order * 1_000,
      song,
    })) ?? []),
    ...serviceLoad.skipped_items.map((item) => ({
      kind: "reference" as const,
      sequence: item.sequence,
      item,
    })),
  ].sort((left, right) => left.sequence - right.sequence);

  return (
    <main className="stagepilot-dashboard mx-auto min-h-screen max-w-[1680px] px-4 py-5 sm:px-6 lg:px-8">
      <header className="mb-5 grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 sm:grid-cols-[auto_minmax(12rem,1fr)_auto] sm:gap-4">
        <div className="flex items-center">
          <h1 className="relative z-10 -translate-y-4 shrink-0 select-none font-brand text-[4.05rem] leading-[0.56] text-white">StagePilot</h1>
          {canConfigure && updater?.status === "available" && updater.availableVersion && (
            <UpdateAvailableButton
              onClick={updater.openConfirmation}
              ref={updateButton}
              version={updater.availableVersion}
            />
          )}
        </div>
        <div
          aria-atomic="true"
          aria-live="polite"
          className={`col-span-2 row-start-2 min-w-0 justify-self-end sm:col-span-1 sm:col-start-2 sm:row-start-1 ${notification ? "" : "hidden sm:block"}`}
        >
          <div
            className={`ml-auto h-9 w-fit max-w-full truncate rounded-lg border px-4 py-2 text-center text-sm transition-opacity ${notification ? `opacity-100 ${notification.tone === "error" ? "border-rose-400/25 bg-rose-400/10 text-rose-200" : "border-sky-400/20 bg-sky-400/10 text-sky-200"}` : "invisible border-transparent opacity-0"}`}
            role="status"
            title={notification?.message}
          >
            {notification?.message ?? "\u00A0"}
          </div>
        </div>
        <div
          className="relative z-40"
          ref={readinessHover.containerRef}
          {...readinessHover.hoverProps}
        >
          <button
            aria-describedby="system-readiness-popover"
            aria-expanded={readinessHover.open}
            className={`flex shrink-0 cursor-help items-center gap-2 whitespace-nowrap rounded-full border px-3 py-1.5 text-xs font-bold uppercase tracking-wider transition hover:brightness-125 hover:ring-1 hover:ring-white/25 hover:shadow-[0_0_18px_rgba(255,255,255,0.12)] ${ready ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-300" : systemError ? "border-rose-400/30 bg-rose-400/10 text-rose-300" : "border-amber-400/30 bg-amber-400/10 text-amber-300"}`}
            type="button"
          >
            <span className={`h-2 w-2 rounded-full ${ready ? "bg-emerald-400" : systemError ? "bg-rose-400" : "bg-amber-400"}`} />
            {!canConfigure ? "Live status" : ready ? "Ready" : systemError ? "Error" : "Check system"}
          </button>
          <div
            className={`absolute right-0 top-full w-[min(22rem,calc(100vw-2rem))] pt-2 transition ${readinessHover.open ? "visible translate-y-0 opacity-100" : "pointer-events-none invisible translate-y-1 opacity-0"}`}
            id="system-readiness-popover"
            role="tooltip"
          >
            <div className="rounded-xl border border-white/10 bg-slate-950/85 p-3 shadow-2xl shadow-black/40 backdrop-blur-xl">
              <ul className="grid gap-1.5">
                {checks.map((check) => (
                  <li
                    className="flex items-center gap-2 rounded-lg bg-white/[0.035] px-3 py-2 text-sm"
                    key={check.id}
                    title={check.detail}
                  >
                    <span className={`grid h-5 w-5 shrink-0 place-items-center rounded-full text-[0.65rem] font-black ${check.status === "connected" ? "bg-emerald-400/15 text-emerald-300" : check.status === "error" ? "bg-rose-400/15 text-rose-300" : "bg-white/[0.07] text-slate-400"}`}>
                      {check.status === "connected" ? "✓" : check.status === "error" ? "!" : "—"}
                    </span>
                    <span className={check.status === "connected" ? "text-emerald-200" : check.status === "error" ? "text-rose-200" : "text-slate-400"}>
                      {check.label}
                    </span>
                    {!check.required && (
                      <span className="ml-auto text-[0.6rem] font-bold uppercase tracking-wider text-slate-500">
                        Optional
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      </header>


      {canConfigure && updater && (
        <UpdateDialog
          onCancel={updater.cancelConfirmation}
          onCloseError={updater.closeError}
          onConfirm={() => void updater.install()}
          onRetry={() => void updater.retry()}
          returnFocus={updateButton}
          updater={updater}
        />
      )}

      {canConfigure && FIRST_LAUNCH_SETUP_ENABLED && (
        <SetupChecklist
          live={live}
          midi={midi}
          onOpen={setActiveConnection}
          propresenter={propresenter}
          settings={settings}
          state={state}
        />
      )}

      {serviceLoad.status === "ambiguous" && (
        <section className="mb-5 rounded-xl border border-amber-400/30 bg-amber-400/10 p-4" aria-live="polite">
          <p className="text-[0.68rem] font-black uppercase tracking-[0.18em] text-amber-300">Plan selection required</p>
          <h2 className="mt-1 text-lg font-bold text-white">Multiple plans match {serviceLoad.target_date ?? "the service date"}</h2>
          {serviceLoad.message && <p className="mt-1 text-sm font-medium text-amber-100">{serviceLoad.message}</p>}
          <p className="mt-1 text-sm text-amber-100/70">
            {serviceLoad.is_stale ? "The previous plan remains available but is marked stale. " : ""}
            {canOperate ? "Choose the service plan StagePilot should load." : "Waiting for an Operator to select the service plan."}
          </p>
          <div className="mt-3 grid gap-2 md:grid-cols-2">
            {serviceLoad.candidates.map((candidate) => (
              <div key={candidate.id} className="flex items-center justify-between gap-3 rounded-lg border border-amber-300/15 bg-slate-950/30 px-3 py-3">
                <div className="min-w-0">
                  <p className="truncate font-semibold text-slate-100">{candidate.title}</p>
                  <p className="mt-0.5 text-xs text-slate-400">{candidate.service_type_name} · {candidate.service_times.join(", ")}</p>
                </div>
                {canOperate && <button
                  aria-label={pendingPlanId === candidate.id ? `Loading ${candidate.title}` : `Use ${candidate.title}`}
                  className="shrink-0 rounded-lg bg-amber-300 px-3 py-2 text-xs font-bold text-slate-950 transition hover:bg-amber-200 disabled:cursor-wait disabled:opacity-50"
                  disabled={pendingPlanId !== null}
                  onClick={() => selectPlan(candidate.id)}
                  type="button"
                >
                  {pendingPlanId === candidate.id ? "Loading…" : "Use this plan"}
                </button>}
              </div>
            ))}
          </div>
        </section>
      )}

      <section
        aria-label="Connections"
        className={`connections-status grid grid-cols-5 gap-1.5 sm:gap-3 ${statusCompact ? "status-compact" : ""} ${statusMotionPhase === "preparing" ? "status-preparing" : statusMotionPhase === "moving" ? "status-moving" : ""}`}
        ref={connectionsRow}
      >
        <StatusCard
          interactive={canConfigure}
          accessibleTitle="Planning Center"
          active={activeConnection === "planning-center"}
          controls="planning-center-configuration"
          detail={connectionViews.planningCenter.detail}
          icon={connectionIcon(planningCenterIcon, "planning-center")}
          onClick={() => toggleConnection("planning-center")}
          status={connectionViews.planningCenter.status}
          title="Services"
        />
        <StatusCard
          interactive={canConfigure}
          accessibleTitle="MIDI / Playback"
          active={activeConnection === "midi"}
          controls="midi-configuration"
          detail={midiDetail}
          icon={connectionIcon(multitracksIcon, "multitracks")}
          onClick={() => toggleConnection("midi")}
          status={connectionViews.midi.status}
          title="Playback"
        />
        <StatusCard
          interactive={canConfigure}
          accessibleTitle="ProPresenter"
          active={activeConnection === "propresenter"}
          controls="propresenter-configuration"
          detail={connectionViews.propresenter.detail}
          icon={connectionIcon(propresenterIcon, "propresenter", true)}
          onClick={() => toggleConnection("propresenter")}
          status={connectionViews.propresenter.status}
          title="Presentation"
        />
        <StatusCard
          interactive={canConfigure}
          active={activeConnection === "lights"}
          controls="lights-configuration"
          detail={connectionViews.lights.detail}
          icon={connectionIcon(lightsIcon, "lights", true)}
          onClick={() => toggleConnection("lights")}
          status={connectionViews.lights.status}
          title="Lights"
        />
        <StatusCard
          interactive={canConfigure}
          accessibleTitle="StagePilot backend"
          active={activeConnection === "backend"}
          controls="backend-configuration"
          detail={health ? `v${health.version} · state revision ${state.revision}` : "Connecting to local API"}
          icon={connectionIcon(stagepilotIcon, "stagepilot", true)}
          onClick={() => toggleConnection("backend")}
          status={backendStatus}
          title="Backend"
        />
      </section>

      {canConfigure && activeConnection === "planning-center" && (
        <PlanningCenterSetupPanel
          error={planningCenterError}
          message={planningCenterMessage}
          onClose={closeConnection}
          onDisconnectOAuth={disconnectPlanningCenter}
          onLoadServiceTypes={loadPlanningCenterServiceTypes}
          onReload={() => dispatch("reload_plan")}
          onSave={savePlanningCenter}
          onSelectPlan={selectPlan}
          onSignInOAuth={signInPlanningCenterOAuth}
          onTest={testPlanningCenterConnection}
          pendingAction={pendingAction}
          pendingOperation={pendingPlanningCenterOperation}
          pendingPlanId={pendingPlanId}
          serviceTypes={planningCenterServiceTypes}
          settings={settings}
          state={state}
          status={planningCenterStatus}
        />
      )}

      {canConfigure && activeConnection === "midi" && (
        <MidiSetupPanel
          error={midiError}
          message={midiMessage}
          midi={midi}
          messages={midiMessages}
          onClose={closeConnection}
          onRefresh={refreshMidi}
          onSelect={selectMidi}
          onSimulate={simulateMidi}
          onSaveSettings={saveMidiSettings}
          pendingCue={pendingMidiCue}
          pendingOperation={pendingMidiOperation}
          pendingSettingsSave={pendingSettingsOperation}
          settings={settings}
          settingsError={settingsError}
          settingsMessage={settingsMessage}
          songs={state?.plan?.songs ?? []}
        />
      )}

      {canConfigure && activeConnection === "propresenter" && (
        <ProPresenterSetupPanel
          error={propresenterError}
          message={propresenterMessage}
          onClose={closeConnection}
          onRefreshTimers={refreshProPresenter}
          onSave={saveProPresenter}
          onTest={runProPresenterTest}
          pendingOperation={pendingProPresenterOperation}
          propresenter={propresenter}
        />
      )}

      {canConfigure && activeConnection === "lights" && (
        <LightsSetupPanel
          connectionStatus={connectionViews.lights.status}
          error={lightsError}
          lights={lights}
          message={lightsMessage}
          onClearAllCues={clearAllLightingCues}
          onClose={closeConnection}
          onRefresh={refreshLights}
          onSaveCues={saveLightingCues}
          onSaveSettings={saveLights}
          onTest={sendLightingTest}
          pendingOperation={pendingLightsOperation}
          settings={settings}
          state={state}
        />
      )}

      {canConfigure && activeConnection === "backend" && (
        <BackendSetupPanel
          error={settingsError}
          health={health}
          live={live}
          message={settingsMessage}
          onClose={closeConnection}
          onSave={saveGeneralSettings}
          pending={pendingSettingsOperation}
          settings={settings}
          state={state}
        />
      )}

      <DashboardGrid
        canEditLayout={canConfigure}
        widgets={{
          "service-plan": (
        <section className="stage-panel widget-autosize-target flex h-full min-h-0 flex-col overflow-hidden rounded-xl">
          <div className="flex flex-wrap items-center justify-between gap-3 p-4">
            <div>
              <p className="section-kicker">Service plan</p>
              <h2 className="mt-1 text-lg font-bold text-white">{plan?.title ?? "No service loaded"}</h2>
              <p className="mt-1 text-xs text-slate-500">
                {plan ? `${plan.service_type} · ${plan.date} · ${plan.service_times.join(", ")}` : "Waiting for a current or upcoming service plan."}
              </p>
            </div>
            <div className="text-right">
              <p className="text-sm font-semibold text-slate-200">{plan?.songs.length ?? 0} songs</p>
              <p className="text-xs text-slate-500">
                {plan ? formatPlanCurrentAsOf(state.last_successful_plan_reload_at) : "Load time unavailable"}
              </p>
            </div>
          </div>
          <ol aria-label="Service plan order" className="min-h-0 flex-1 overflow-hidden" data-autosize-content>
            {servicePlanEntries.map((entry) => entry.kind === "song"
              ? <SongRow key={`song-${entry.song.id}`} song={entry.song} current={entry.song.id === state.current_song?.id} next={entry.song.id === state.next_song?.id} />
              : <ReferenceItemRow key={`reference-${entry.item.item_id}`} item={entry.item} />)}
          </ol>
        </section>
          ),
          "now-playing": (
          <section className="now-playing-panel widget-autosize-target h-full min-h-0 overflow-hidden rounded-2xl border p-5 sm:p-6">
            <div className="flex items-center justify-between gap-3">
              <p className="live-kicker text-[0.68rem] font-black uppercase tracking-[0.2em]">Now playing · live signal</p>
              <span className={`rounded-full px-2.5 py-1 text-[0.65rem] font-bold uppercase tracking-wider ${state.timer.status === "running" ? "bg-emerald-400/15 text-emerald-300" : state.timer.status === "error" ? "bg-rose-400/15 text-rose-300" : "bg-white/5 text-slate-400"}`}>Timer {state.timer.status}</span>
            </div>
            <h2 className="live-title-transition mt-7 min-h-9 text-[clamp(2rem,3.2vw,3.65rem)] font-black leading-[0.96] tracking-[-0.045em] text-white" key={state.current_song?.id ?? "idle"}>{state.current_song?.title ?? "Waiting for first cue"}</h2>
            <div className="mt-2 flex items-end justify-between gap-5">
              <div>
                <p className="text-sm text-slate-500">Time remaining</p>
                <p className="operational-timer mt-1 font-mono text-[clamp(3rem,5vw,5.4rem)] font-light leading-none">{formatDuration(timerRemaining)}</p>
              </div>
              <div className="elapsed-time-block pb-1 text-right">
                <p className="text-sm font-semibold text-slate-400 sm:text-base">Elapsed time</p>
                <p className="mt-1 font-mono text-[clamp(1.5rem,2.4cqw,2rem)] font-semibold leading-none tabular-nums text-[#ff9b7e]">{formatDuration(timerElapsed)}</p>
                <p className="mt-1 text-xs font-medium text-slate-400 sm:text-sm">of {formatDuration(state.current_song?.duration_seconds)}</p>
              </div>
            </div>
            <div className="now-playing-metadata mt-7 grid grid-cols-2 gap-4 border-t border-white/7 pt-4 text-sm">
              <div><p className="text-xs text-slate-500">Position</p><p className="mt-1 font-semibold text-slate-200">{state.current_song_index == null ? "Not started" : `${state.current_song_index + 1} of ${plan?.songs.length ?? 0}`}</p></div>
              <div><p className="text-xs text-slate-500">Up next</p><p className="mt-1 line-clamp-2 font-semibold text-slate-100">{state.next_song?.title ?? "End of service"}</p></div>
              <div><p className="text-xs text-slate-500">Countdown started</p><p className="mt-1 font-semibold text-slate-200">{formatTime(state.timer.started_at)}</p></div>
              <div><p className="text-xs text-slate-500">Last action</p><p className="mt-1 font-semibold capitalize text-slate-200">{state.last_action?.replaceAll("_", " ") ?? "None"}</p></div>
            </div>
          </section>
          ),
          "manual-controls": canOperate ? (
          <section className="stage-panel widget-autosize-target manual-controls-panel h-full min-h-0 overflow-hidden rounded-xl p-4">
            <p className="section-kicker">Manual controls</p>
            <div className="manual-controls-grid mt-3 grid gap-2">
              <ActionButton action="start_next" label="Start next" tone="green" disabled={pendingAction !== null} onAction={dispatch} />
              <ActionButton action="restart_current" label="Restart current" tone="green" disabled={pendingAction !== null || !state.current_song} onAction={dispatch} />
              <ActionButton action="previous" label="Previous" tone="orange" disabled={pendingAction !== null} onAction={dispatch} />
              <ActionButton action="reload_plan" label="Reload plan" tone="blue" disabled={pendingAction !== null} onAction={dispatch} />
              <ActionButton action="stop_timer" label="Stop timer" tone="red" disabled={pendingAction !== null} onAction={dispatch} />
              <ActionButton action="reset_position" label="Reset position" tone="red" disabled={pendingAction !== null} onAction={dispatch} />
            </div>
          </section>
          ) : null,
          events: (
        <section className="stage-panel flex h-full min-h-0 flex-col overflow-hidden rounded-xl">
          <div className="flex items-center justify-between p-4"><p className="section-kicker">Recent event stream</p><span className="text-xs text-slate-600">Latest {activity.length}</span></div>
          <div className="min-h-0 flex-1 overflow-auto border-t border-white/5">
            {pinnedError && <div className="grid grid-cols-[4.5rem_1fr] gap-3 border-b border-rose-400/15 bg-rose-500/[0.08] px-4 py-2.5 text-xs"><time className="font-mono text-rose-300/70">{formatTime(pinnedError.timestamp)}</time><p className="text-rose-200"><span className="font-bold uppercase text-rose-300">{pinnedError.component}</span> · {pinnedError.message}</p></div>}
            {activity.map((event) => <div key={event.id} className="event-row grid grid-cols-[4.5rem_1fr] gap-3 border-b border-white/[0.055] px-4 py-2.5 text-xs"><time className="font-mono text-slate-600">{formatTime(event.timestamp)}</time><p className="truncate text-slate-300"><span className="font-semibold text-slate-200">{event.type}</span> · {event.source}</p></div>)}
            {!activity.length && <p className="p-4 text-sm text-slate-500">Waiting for demo events…</p>}
          </div>
        </section>
          ),
        }}
      />
    </main>
  );
}
