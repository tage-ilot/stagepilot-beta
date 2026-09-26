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
  const [timezone, setTimezone] = useState("America/Los_Angeles");
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
    setTimezone(settings.settings.timezone);
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
            <input
              className="w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-slate-100 outline-none focus:border-blue-500/60"
              disabled={busy}
              onChange={(event) => setTimezone(event.target.value)}
              value={timezone}
            />
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
