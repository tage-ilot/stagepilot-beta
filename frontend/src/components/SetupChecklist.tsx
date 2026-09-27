import { useEffect, useState } from "react";

import type {
  ApplicationState,
  MidiInputsResponse,
  PlanningCenterStatusResponse,
  ProPresenterStatusResponse,
  SettingsResponse,
} from "../types";

type SetupPanel = "planning-center" | "midi" | "propresenter" | "backend";

export function SetupChecklist({
  state,
  settings,
  planningCenterStatus,
  midi,
  propresenter,
  live,
  onOpen,
}: {
  state: ApplicationState;
  settings: SettingsResponse | null;
  planningCenterStatus?: PlanningCenterStatusResponse | null;
  midi: MidiInputsResponse | null;
  propresenter: ProPresenterStatusResponse | null;
  live: boolean;
  onOpen: (panel: SetupPanel) => void;
}) {
  const [visible, setVisible] = useState(true);
  const saved = settings?.settings;
  const generalComplete = saved?.onboarding.general_completed ?? false;
  const planningAuthComplete = Boolean(
    (planningCenterStatus?.connection_method === "oauth"
      && planningCenterStatus.oauth_connected)
    || (saved?.planning_center.app_id && settings?.planning_center_secret_saved),
  );
  const planningCenterComplete = Boolean(
    saved
    && saved.integration_modes.service_source === "planning_center"
    && planningAuthComplete
    && saved.planning_center.service_type_id,
  );
  const midiComplete = Boolean(
    saved
    && saved.integration_modes.midi_source === "real"
    && saved.midi.input_name,
  );
  const proPresenterComplete = Boolean(
    saved?.integration_modes.timer_output === "propresenter"
    && saved.propresenter.host
    && saved.propresenter.timer_name,
  );
  const connectionsComplete = live
    && state.planning_center_status === "connected"
    && state.midi_status === "connected"
    && state.propresenter_status === "connected"
    && Boolean(midi?.inputs.some((input) => input.connected))
    && Boolean(propresenter?.timer_found);
  const ready = connectionsComplete
    && state.service_load.status === "loaded"
    && !state.service_load.is_stale
    && Boolean(state.plan?.songs.length)
    && Boolean(state.plan?.songs.every((song) => (song.duration_seconds ?? 0) > 0));

  const steps: Array<{
    label: string;
    detail: string;
    complete: boolean;
    panel?: SetupPanel;
  }> = [
    {
      label: "General settings",
      detail: generalComplete ? "Timezone, logging, and server port saved" : "Review and save startup defaults",
      complete: generalComplete,
      panel: "backend",
    },
    {
      label: "Planning Center",
      detail: planningCenterComplete ? "Credentials and service type saved" : "Save credentials and select a service type",
      complete: planningCenterComplete,
      panel: "planning-center",
    },
    {
      label: "MIDI / Playback",
      detail: midiComplete ? "Real MIDI input selected" : "Save cue mapping, restart, and select an input",
      complete: midiComplete,
      panel: "midi",
    },
    {
      label: "ProPresenter",
      detail: proPresenterComplete ? "Real timer output configured" : "Save the API and countdown timer settings",
      complete: proPresenterComplete,
      panel: "propresenter",
    },
    {
      label: "Connection test",
      detail: connectionsComplete ? "All integrations are connected" : "Connect Planning Center, MIDI, and ProPresenter",
      complete: connectionsComplete,
    },
    {
      label: "Ready",
      detail: ready ? "A current or upcoming service is loaded" : "Load a valid service plan and confirm song durations",
      complete: ready,
    },
  ];

  const completeCount = steps.filter((step) => step.complete).length;
  const setupComplete = completeCount === steps.length;

  useEffect(() => {
    if (!setupComplete || !visible) return;

    const timeout = window.setTimeout(() => setVisible(false), 2500);
    return () => window.clearTimeout(timeout);
  }, [setupComplete, visible]);

  if (!visible) return null;

  return (
    <section className="relative mb-5 rounded-xl border border-sky-400/20 bg-slate-950/70 p-4 shadow-2xl shadow-black/20" aria-label="StagePilot setup progress">
      <button
        aria-label="Close first-launch setup"
        className="absolute right-3 top-3 grid h-8 w-8 place-items-center rounded-lg border border-white/10 bg-white/5 text-lg text-slate-400 transition hover:border-white/20 hover:bg-white/10 hover:text-white"
        onClick={() => setVisible(false)}
        title="Close"
        type="button"
      >
        ×
      </button>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="pr-10">
          <p className="text-[0.68rem] font-black uppercase tracking-[0.18em] text-sky-300">First-launch setup</p>
          <h2 className="mt-1 text-lg font-bold text-white">
            {setupComplete ? "StagePilot setup is complete" : "Finish configuring StagePilot"}
          </h2>
          <p aria-live="polite" className="mt-1 text-sm text-slate-300">
            {setupComplete
              ? "Everything is connected and ready. This checklist will close automatically."
              : "Select a setup step to open its connection panel."}
          </p>
        </div>
        <span className="rounded-full border border-sky-300/20 bg-slate-950/30 px-3 py-1.5 text-xs font-bold text-sky-200">
          {completeCount} of {steps.length} complete
        </span>
      </div>

      <ol className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {steps.map((step, index) => {
          const content = (
            <>
              <span className={`grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-black ${step.complete ? "bg-emerald-400 text-slate-950" : "bg-white/8 text-slate-400"}`}>
                {step.complete ? "✓" : index + 1}
              </span>
              <span className="min-w-0 text-left">
                <span className="block text-sm font-semibold text-slate-100">{step.label}</span>
                <span className="mt-0.5 block text-xs text-slate-400">{step.detail}</span>
              </span>
            </>
          );
          return (
            <li key={step.label}>
              {step.panel ? (
                <button
                  className="flex min-h-16 w-full items-center gap-3 rounded-lg border border-white/7 bg-slate-950/30 px-3 py-2.5 transition hover:border-sky-300/25 hover:bg-slate-950/50"
                  onClick={() => onOpen(step.panel!)}
                  type="button"
                >
                  {content}
                </button>
              ) : (
                <div className="flex min-h-16 items-center gap-3 rounded-lg border border-white/7 bg-slate-950/20 px-3 py-2.5">
                  {content}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
