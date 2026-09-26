import { useEffect, useMemo, useState } from "react";

import { useDelayedHover } from "../hooks/useDelayedHover";
import { openExternalUrl } from "../desktop";
import type {
  ApplicationState,
  PlanningCenterServiceType,
  PlanningCenterSettingsInput,
  PlanningCenterStatusResponse,
  PlanningCenterTestInput,
  SettingsResponse,
} from "../types";
import { SetupPanelHeader } from "./SetupPanelHeader";

const ALL_SERVICE_TYPES_ID = "stagepilot:all-service-types";

type TimezoneOption = {
  city: string;
  offsetMinutes: number;
  timezone: string;
};

// One representative city for every populated civil standard-time offset.
// Labels stay fixed year-round; values are real IANA zones with their normal DST rules.
export const TIMEZONE_OPTIONS: readonly TimezoneOption[] = [
  { city: "Pago Pago", offsetMinutes: -660, timezone: "Pacific/Pago_Pago" },
  { city: "Honolulu", offsetMinutes: -600, timezone: "Pacific/Honolulu" },
  { city: "Taiohae", offsetMinutes: -570, timezone: "Pacific/Marquesas" },
  { city: "Anchorage", offsetMinutes: -540, timezone: "America/Anchorage" },
  { city: "Los Angeles", offsetMinutes: -480, timezone: "America/Los_Angeles" },
  { city: "Phoenix", offsetMinutes: -420, timezone: "America/Phoenix" },
  { city: "Mexico City", offsetMinutes: -360, timezone: "America/Mexico_City" },
  { city: "New York", offsetMinutes: -300, timezone: "America/New_York" },
  { city: "Santiago", offsetMinutes: -240, timezone: "America/Santiago" },
  { city: "St. John’s", offsetMinutes: -210, timezone: "America/St_Johns" },
  { city: "São Paulo", offsetMinutes: -180, timezone: "America/Sao_Paulo" },
  { city: "Vila dos Remédios", offsetMinutes: -120, timezone: "America/Noronha" },
  { city: "Praia", offsetMinutes: -60, timezone: "Atlantic/Cape_Verde" },
  { city: "London", offsetMinutes: 0, timezone: "Europe/London" },
  { city: "Lagos", offsetMinutes: 60, timezone: "Africa/Lagos" },
  { city: "Cairo", offsetMinutes: 120, timezone: "Africa/Cairo" },
  { city: "Istanbul", offsetMinutes: 180, timezone: "Europe/Istanbul" },
  { city: "Tehran", offsetMinutes: 210, timezone: "Asia/Tehran" },
  { city: "Dubai", offsetMinutes: 240, timezone: "Asia/Dubai" },
  { city: "Kabul", offsetMinutes: 270, timezone: "Asia/Kabul" },
  { city: "Karachi", offsetMinutes: 300, timezone: "Asia/Karachi" },
  { city: "Delhi", offsetMinutes: 330, timezone: "Asia/Kolkata" },
  { city: "Kathmandu", offsetMinutes: 345, timezone: "Asia/Kathmandu" },
  { city: "Dhaka", offsetMinutes: 360, timezone: "Asia/Dhaka" },
  { city: "Yangon", offsetMinutes: 390, timezone: "Asia/Yangon" },
  { city: "Jakarta", offsetMinutes: 420, timezone: "Asia/Jakarta" },
  { city: "Shanghai", offsetMinutes: 480, timezone: "Asia/Shanghai" },
  { city: "Eucla", offsetMinutes: 525, timezone: "Australia/Eucla" },
  { city: "Tokyo", offsetMinutes: 540, timezone: "Asia/Tokyo" },
  { city: "Adelaide", offsetMinutes: 570, timezone: "Australia/Adelaide" },
  { city: "Sydney", offsetMinutes: 600, timezone: "Australia/Sydney" },
  { city: "Lord Howe Island", offsetMinutes: 630, timezone: "Australia/Lord_Howe" },
  { city: "Nouméa", offsetMinutes: 660, timezone: "Pacific/Noumea" },
  { city: "Auckland", offsetMinutes: 720, timezone: "Pacific/Auckland" },
  { city: "Waitangi", offsetMinutes: 765, timezone: "Pacific/Chatham" },
  { city: "Apia", offsetMinutes: 780, timezone: "Pacific/Apia" },
  { city: "London, Kiribati", offsetMinutes: 840, timezone: "Pacific/Kiritimati" },
];

const formatOffset = (offsetMinutes: number) => {
  const sign = offsetMinutes < 0 ? "-" : "+";
  const absoluteMinutes = Math.abs(offsetMinutes);
  const hours = Math.floor(absoluteMinutes / 60);
  const minutes = absoluteMinutes % 60;
  return `UTC${sign}${hours}${minutes ? `:${minutes.toString().padStart(2, "0")}` : ""}`;
};

const offsetAt = (timezone: string, date: Date) => {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const representedAsUtc = Date.UTC(
    Number(values.year),
    Number(values.month) - 1,
    Number(values.day),
    Number(values.hour),
    Number(values.minute),
    Number(values.second),
  );
  return Math.round((representedAsUtc - date.getTime()) / 60_000);
};

const standardOffsetForTimezone = (timezone: string) => {
  try {
    const january = offsetAt(timezone, new Date("2024-01-15T12:00:00Z"));
    const july = offsetAt(timezone, new Date("2024-07-15T12:00:00Z"));
    return Math.min(january, july);
  } catch {
    return null;
  }
};

const systemTimezone = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

const formatTimestamp = (value: string | null) =>
  value ? new Date(value).toLocaleString() : "Not yet";

export function PlanningCenterSetupPanel({
  state,
  settings,
  status,
  serviceTypes,
  error,
  message,
  pendingOperation,
  pendingAction,
  pendingPlanId,
  onClose,
  onTest,
  onLoadServiceTypes,
  onSave,
  onReload,
  onSelectPlan,
  onSignInOAuth,
  onDisconnectOAuth,
}: {
  state: ApplicationState;
  settings: SettingsResponse | null;
  status: PlanningCenterStatusResponse | null;
  serviceTypes: PlanningCenterServiceType[];
  error: string | null;
  message: string | null;
  pendingOperation: "test" | "load-types" | "save" | "oauth-sign-in" | "oauth-disconnect" | null;
  pendingAction: string | null;
  pendingPlanId: string | null;
  onClose: () => void;
  onTest: (input: PlanningCenterTestInput) => void;
  onLoadServiceTypes: () => void;
  onSave: (
    input: PlanningCenterSettingsInput,
    timezone: string,
  ) => void;
  onReload: () => void;
  onSelectPlan: (planId: string) => void;
  onSignInOAuth?: () => void;
  onDisconnectOAuth?: () => void;
}) {
  const serviceLoad = state.service_load;
  const publicSettings = settings?.settings.planning_center;
  const [appId, setAppId] = useState("");
  const [secret, setSecret] = useState("");
  const [serviceTypeId, setServiceTypeId] = useState("");
  const [timezone, setTimezone] = useState(systemTimezone);
  const [timezoneOffset, setTimezoneOffset] = useState(() => (
    standardOffsetForTimezone(systemTimezone()) ?? 0
  ));
  const [titlePreference, setTitlePreference] = useState("");
  const [preferredTime, setPreferredTime] = useState("");
  const [removeSecret, setRemoveSecret] = useState(false);
  const applicationIdHelp = useDelayedHover();

  const connectionMethod = status?.connection_method ?? "manual";
  const oauthConnected = status?.oauth_connected ?? false;
  const oauthNeedsReconnect = status?.oauth_needs_reconnect ?? false;
  const [manualConnectionOpen, setManualConnectionOpen] = useState(false);

  useEffect(() => {
    if (!settings) return;
    setAppId(settings.settings.planning_center.app_id ?? "");
    setServiceTypeId(settings.settings.planning_center.service_type_id ?? "");
    const savedTimezone = settings.settings.timezone || systemTimezone();
    setTimezone(savedTimezone);
    setTimezoneOffset(standardOffsetForTimezone(savedTimezone) ?? 0);
    setTitlePreference(settings.settings.planning_center.plan_title_preference ?? "");
    setPreferredTime(settings.settings.planning_center.preferred_service_time ?? "");
  }, [settings]);

  const selectedServiceTypeKnown = serviceTypeId === ALL_SERVICE_TYPES_ID
    || serviceTypes.some((value) => value.id === serviceTypeId);
  const valid = useMemo(
    () => Boolean(
      (connectionMethod === "oauth" || appId.trim())
      && serviceTypeId
      && timezone.trim(),
    ),
    [appId, connectionMethod, serviceTypeId, timezone],
  );
  const busy = pendingOperation !== null;

  const testInput = (): PlanningCenterTestInput => ({
    ...(appId.trim() ? { app_id: appId.trim() } : {}),
    ...(secret ? { secret } : {}),
  });

  const save = () => {
    if (!publicSettings || !valid) return;
    onSave(
      {
        app_id: appId.trim(),
        service_type_id: serviceTypeId,
        plan_title_preference: titlePreference.trim() || null,
        preferred_service_time: preferredTime || null,
        upcoming_lookahead_days: publicSettings.upcoming_lookahead_days,
        request_timeout_seconds: publicSettings.request_timeout_seconds,
        ...(secret ? { secret } : {}),
        ...(removeSecret ? { remove_secret: true } : {}),
      },
      timezone.trim(),
    );
    setSecret("");
  };

  return (
    <section
      aria-busy={busy}
      aria-labelledby="planning-center-setup-heading"
      className="setup-panel mt-5 rounded-2xl border border-white/10 bg-slate-950/70 p-5 shadow-2xl shadow-black/20"
      id="planning-center-configuration"
    >
      <SetupPanelHeader
        closeLabel="Close Planning Center configuration"
        description="Sign in with Planning Center, or manage a manual connection, to keep the Service Plan updated."
        headingId="planning-center-setup-heading"
        onClose={onClose}
        status={status?.connection_status ?? state.planning_center_status}
        title="Planning Center Services"
      />

      {oauthNeedsReconnect && (
        <div
          className="mt-4 rounded-lg border border-amber-400/30 bg-amber-400/10 px-4 py-3 text-sm text-amber-100"
          role="alert"
        >
          <p className="font-semibold">Your Planning Center connection has expired</p>
          <p className="mt-1 text-amber-200/90">
            Sign in again to keep loading your Service Plan from Planning Center.
          </p>
          <button
            className="mt-3 rounded-lg border border-amber-400/40 bg-amber-500/20 px-3.5 py-2 text-sm font-semibold text-amber-100 transition hover:bg-amber-500/30 disabled:opacity-40"
            disabled={busy || !onSignInOAuth}
            onClick={onSignInOAuth}
            type="button"
          >
            {pendingOperation === "oauth-sign-in" ? "Reconnecting…" : "Reconnect to Planning Center"}
          </button>
        </div>
      )}

      {connectionMethod === "oauth" && oauthConnected && !oauthNeedsReconnect && (
        <div className="mt-4 rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-4 py-3 text-sm text-emerald-100">
          <p className="font-semibold">Connected to Planning Center</p>
          <p className="mt-1 text-emerald-200/80">
            Signed in with Planning Center. Your credentials never leave Planning Center&apos;s servers.
          </p>
          <button
            className="mt-3 rounded-lg border border-white/10 bg-black/20 px-3.5 py-2 text-sm font-semibold text-slate-200 transition hover:bg-black/30 disabled:opacity-40"
            disabled={busy || !onDisconnectOAuth}
            onClick={onDisconnectOAuth}
            type="button"
          >
            {pendingOperation === "oauth-disconnect" ? "Disconnecting…" : "Disconnect"}
          </button>
        </div>
      )}

      {!(connectionMethod === "oauth" && oauthConnected) && !oauthNeedsReconnect && (
        <div className="mt-4 rounded-lg border border-blue-500/30 bg-blue-500/10 px-4 py-3">
          <p className="text-sm font-semibold text-blue-100">Sign in with Planning Center</p>
          <p className="mt-1 text-sm text-blue-200/80">
            The fastest way to connect: sign in with your Planning Center account, no App ID or
            Secret required.
          </p>
          <button
            className="mt-3 rounded-lg border border-blue-500/40 bg-blue-700 px-3.5 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-600 disabled:opacity-40"
            disabled={busy || !onSignInOAuth}
            onClick={onSignInOAuth}
            type="button"
          >
            {pendingOperation === "oauth-sign-in" ? "Signing in…" : "Sign in with Planning Center"}
          </button>
        </div>
      )}

      <details
        className="mt-5 rounded-xl border border-white/10 bg-black/10 p-4"
        open={manualConnectionOpen}
      >
        <summary
          className="cursor-pointer select-none text-sm font-bold uppercase tracking-wider text-slate-400"
          onClick={(event) => {
            event.preventDefault();
            setManualConnectionOpen((value) => !value);
          }}
        >
          Manual API Connection
        </summary>

        <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <div className="text-sm text-slate-300">
            <div className="mb-1.5 flex items-center gap-1.5">
              <label
                className="text-xs font-bold uppercase tracking-wider text-slate-500"
                htmlFor="planning-center-application-id"
              >
                Application ID
              </label>
              <div
                className="relative"
                ref={applicationIdHelp.containerRef}
                {...applicationIdHelp.hoverProps}
              >
                <button
                  aria-describedby="planning-center-pat-help"
                  aria-label="How to get a Planning Center Personal Access Token"
                  className="grid h-4 w-4 place-items-center rounded-full border border-slate-500 pb-px text-[0.65rem] font-black leading-none normal-case tracking-normal text-slate-400 transition hover:border-blue-300 hover:text-blue-200 focus:outline-none focus:ring-2 focus:ring-blue-400/60"
                  type="button"
                >
                  ?
                </button>
                <div
                  className={`absolute left-0 top-full z-50 w-[min(22rem,calc(100vw-3rem))] pt-2 text-left text-sm font-normal normal-case tracking-normal transition ${applicationIdHelp.open ? "visible translate-y-0 opacity-100" : "pointer-events-none invisible translate-y-1 opacity-0"}`}
                  id="planning-center-pat-help"
                  role="tooltip"
                >
                  <div className="rounded-xl border border-blue-300/20 bg-slate-950/95 p-4 text-slate-300 shadow-2xl shadow-black/50 backdrop-blur-xl">
                    <p className="font-semibold text-slate-100">Connect your Planning Center account</p>
                    <p className="mt-2 leading-relaxed">
                      Open Planning Center&apos;s{" "}
                      <a
                        className="font-semibold text-blue-300 underline decoration-blue-300/50 underline-offset-2 hover:text-blue-200"
                        href="https://api.planningcenteronline.com/personal_access_tokens"
                        onClick={(event) => {
                          event.preventDefault();
                          void openExternalUrl(event.currentTarget.href);
                        }}
                        rel="noreferrer"
                        target="_blank"
                      >
                        Personal Access Tokens page
                      </a>
                      , create a new token, and give it a recognizable name. Copy
                      its Client ID into Application ID and its Secret into the
                      Secret field. Save the settings, then load your service types
                      to keep StagePilot&apos;s Service Plan updated.
                    </p>
                  </div>
                </div>
              </div>
            </div>
            <input
              autoComplete="username"
              className="w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-slate-100 outline-none focus:border-blue-500/60"
              disabled={busy}
              id="planning-center-application-id"
              onChange={(event) => setAppId(event.target.value)}
              value={appId}
            />
          </div>
          <label className="text-sm text-slate-300">
            <span className="mb-1.5 block text-xs font-bold uppercase tracking-wider text-slate-500">Secret</span>
            <input
              autoComplete="current-password"
              className="w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-slate-100 outline-none focus:border-blue-500/60"
              disabled={busy || removeSecret}
              onChange={(event) => setSecret(event.target.value)}
              placeholder={status?.planning_center_secret_saved ? "Saved securely — leave blank to keep" : "Enter PAT secret"}
              type="password"
              value={secret}
            />
          </label>
          <label className="flex items-center gap-2 self-end rounded-lg border border-white/7 bg-black/20 px-3 py-2.5 text-sm text-slate-300">
            <input
              checked={removeSecret}
              disabled={busy || !status?.planning_center_secret_saved}
              onChange={(event) => {
                setRemoveSecret(event.target.checked);
                if (event.target.checked) setSecret("");
              }}
              type="checkbox"
            />
            Remove saved secret
          </label>
        </div>

        {!(connectionMethod === "oauth" && oauthConnected) && (
          <div className="mt-5 flex flex-wrap gap-2">
            <button
              className="rounded-lg border border-blue-500/30 bg-blue-500/10 px-3.5 py-2.5 text-sm font-semibold text-blue-200 transition hover:bg-blue-500/20 disabled:opacity-40"
              disabled={busy || (!secret && !status?.planning_center_secret_saved)}
              onClick={() => onTest(testInput())}
              type="button"
            >
              {pendingOperation === "test" ? "Testing…" : "Test connection"}
            </button>
          </div>
        )}
      </details>

      <div className="mt-5 rounded-xl border border-white/10 bg-black/10 p-4">
        <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400">
          Service Plan Settings
        </h3>
        <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <label className="text-sm text-slate-300">
            <span className="mb-1.5 block text-xs font-bold uppercase tracking-wider text-slate-500">Service type</span>
            <select
              className="w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-slate-100 disabled:opacity-50"
              disabled={busy || serviceTypes.length === 0}
              onChange={(event) => setServiceTypeId(event.target.value)}
              value={serviceTypeId}
            >
              <option value="">Load and choose a service type</option>
              <option value={ALL_SERVICE_TYPES_ID}>All service types (nearest upcoming plan)</option>
              {serviceTypeId && !selectedServiceTypeKnown && (
                <option value={serviceTypeId}>Saved service type ({serviceTypeId})</option>
              )}
              {serviceTypes.map((serviceType) => (
                <option key={serviceType.id} value={serviceType.id}>{serviceType.name}</option>
              ))}
            </select>
          </label>
          <label className="text-sm text-slate-300">
            <span className="mb-1.5 block text-xs font-bold uppercase tracking-wider text-slate-500">Timezone</span>
            <select
              className="w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-slate-100 disabled:opacity-50"
              disabled={busy}
              onChange={(event) => {
                const offsetMinutes = Number(event.target.value);
                const selected = TIMEZONE_OPTIONS.find((option) => (
                  option.offsetMinutes === offsetMinutes
                ));
                if (!selected) return;
                setTimezoneOffset(offsetMinutes);
                setTimezone(selected.timezone);
              }}
              value={timezoneOffset}
            >
              {TIMEZONE_OPTIONS.map((option) => (
                <option key={option.offsetMinutes} value={option.offsetMinutes}>
                  {option.city} ({formatOffset(option.offsetMinutes)})
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm text-slate-300">
            <span className="mb-1.5 block text-xs font-bold uppercase tracking-wider text-slate-500">Plan title preference</span>
            <input
              className="w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-slate-100 outline-none focus:border-blue-500/60"
              disabled={busy}
              onChange={(event) => setTitlePreference(event.target.value)}
              placeholder="Optional, for example Sunday Morning"
              value={titlePreference}
            />
          </label>
          <label className="text-sm text-slate-300">
            <span className="mb-1.5 block text-xs font-bold uppercase tracking-wider text-slate-500">Preferred service time</span>
            <input
              className="w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-slate-100 outline-none focus:border-blue-500/60"
              disabled={busy}
              onChange={(event) => setPreferredTime(event.target.value)}
              type="time"
              value={preferredTime}
            />
          </label>
        </div>

        <div className="mt-5 flex flex-wrap gap-2">
          <button
            className="rounded-lg border border-blue-500/30 bg-blue-500/10 px-3.5 py-2.5 text-sm font-semibold text-blue-200 transition hover:bg-blue-500/20 disabled:opacity-40"
            disabled={busy || !(status?.oauth_connected || status?.planning_center_secret_saved)}
            onClick={onLoadServiceTypes}
            type="button"
          >
            {pendingOperation === "load-types" ? "Loading…" : "Load service types"}
          </button>
          <button
            className="rounded-lg border border-blue-500/40 bg-blue-700 px-3.5 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-600 disabled:opacity-40"
            disabled={busy || !valid}
            onClick={save}
            type="button"
          >
            {pendingOperation === "save" ? "Saving…" : "Save settings"}
          </button>
          <button
            className="rounded-lg border border-blue-500/30 bg-blue-500/10 px-3.5 py-2.5 text-sm font-semibold text-blue-200 transition hover:bg-blue-500/20 disabled:opacity-40"
            disabled={pendingAction !== null || !state.plugins.planning_center}
            onClick={onReload}
            type="button"
          >
            {pendingAction === "reload_plan" ? "Loading…" : "Load today’s plan"}
          </button>
        </div>
      </div>

      {(error || message) && (
        <p className={`mt-4 rounded-lg border px-3 py-2 text-sm ${error ? "border-rose-400/20 bg-rose-400/10 text-rose-200" : "border-emerald-400/20 bg-emerald-400/10 text-emerald-200"}`}>
          {error ?? message}
        </p>
      )}
      {!state.plugins.planning_center && (
        <p className="mt-3 text-xs text-amber-200">
          Saving this configuration enables Planning Center. Restart StagePilot to start the connection.
        </p>
      )}

      <div className="mt-5 grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-lg border border-white/5 bg-black/20 px-3 py-3">
          <p className="text-xs uppercase tracking-wider text-slate-500">Connection</p>
          <p className="mt-1 capitalize text-slate-200">{status?.connection_status ?? state.planning_center_status}</p>
        </div>
        <div className="rounded-lg border border-white/5 bg-black/20 px-3 py-3">
          <p className="text-xs uppercase tracking-wider text-slate-500">Credential</p>
          <p className="mt-1 text-slate-200">{status?.planning_center_secret_saved ? "Saved securely" : "Not saved"}</p>
        </div>
        <div className="rounded-lg border border-white/5 bg-black/20 px-3 py-3">
          <p className="text-xs uppercase tracking-wider text-slate-500">Target date</p>
          <p className="mt-1 text-slate-200">{serviceLoad.target_date ?? "Not selected"}</p>
        </div>
        <div className="rounded-lg border border-white/5 bg-black/20 px-3 py-3">
          <p className="text-xs uppercase tracking-wider text-slate-500">Last successful sync</p>
          <p className="mt-1 text-slate-200">{formatTimestamp(state.last_successful_plan_reload_at)}</p>
        </div>
      </div>

      {serviceLoad.candidates.length > 0 && (
        <div className="mt-4 rounded-lg border border-amber-400/20 bg-amber-400/[0.06] p-3">
          <p className="text-xs font-bold uppercase tracking-wider text-amber-300">Matching plans</p>
          <div className="mt-2 grid gap-2 md:grid-cols-2">
            {serviceLoad.candidates.map((candidate) => (
              <div className="flex items-center justify-between gap-3 rounded-lg bg-black/20 px-3 py-2" key={candidate.id}>
                <div className="min-w-0">
                  <p className="truncate font-semibold text-slate-100">{candidate.title}</p>
                  <p className="text-xs text-slate-500">{candidate.service_type_name} · {candidate.service_times.join(", ")}</p>
                </div>
                <button
                  className="shrink-0 rounded-lg bg-blue-700 px-3 py-2 text-xs font-bold text-white transition hover:bg-blue-600 disabled:opacity-50"
                  disabled={pendingPlanId !== null}
                  onClick={() => onSelectPlan(candidate.id)}
                  type="button"
                >
                  {pendingPlanId === candidate.id ? "Loading…" : "Use plan"}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
