import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PlaybackController } from "../hooks/usePlaybackInput";
import { playbackSettings, playbackState, playbackStatus } from "../test/playbackFixtures";
import { MidiSetupPanel } from "./MidiSetupPanel";
import { PlaybackApiPanel } from "./PlaybackApiPanel";
import { validPlaybackHost } from "./playbackStatus";
import { SetupChecklist } from "./SetupChecklist";
import { buildConnectionCardViews, buildReadinessChecks } from "./dashboard/dashboardReadiness";

const controller = (overrides: Partial<PlaybackController> = {}): PlaybackController => ({
  status: playbackStatus(), events: [], error: null, message: null, pending: null,
  save: vi.fn(), selectSource: vi.fn(), scan: vi.fn(), discover: vi.fn(), ...overrides,
});
const panel = (playback = controller()) => render(<MidiSetupPanel
  playback={playback} midi={null} messages={[]} error={null} message={null}
  pendingOperation={null} pendingCue={null} onRefresh={vi.fn()} onSelect={vi.fn()} onSimulate={vi.fn()}
  settings={playbackSettings} onSaveSettings={vi.fn()}
/>);

describe("Playback configuration", () => {
  it("defaults to API, starts Advanced collapsed, expands/switches source and collapses again", async () => {
    const playback = controller();
    const user = userEvent.setup();
    const view = panel(playback);
    expect(screen.getByText("Playback API")).toBeVisible();
    expect(screen.queryByLabelText("Playback source")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("MIDI channel")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByRole("button", { name: /Advanced/ }));
    expect(screen.getByLabelText("Playback source")).toHaveValue("playback_api");
    expect(screen.getByLabelText("MIDI channel")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Playback source"), "real");
    expect(playback.selectSource).toHaveBeenCalledWith("real");
    await user.selectOptions(screen.getByLabelText("Playback source"), "playback_api");
    expect(playback.selectSource).toHaveBeenLastCalledWith("playback_api");
    view.unmount();
    panel(playback);
    expect(screen.queryByLabelText("Playback source")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start next" })).not.toBeInTheDocument();
  });

  it("shows scan pending/result and respects saved manual override", async () => {
    const playback = controller();
    const view = render(<PlaybackApiPanel playback={playback} />);
    fireEvent.click(screen.getByRole("button", { name: "Find / Scan" }));
    expect(playback.scan).toHaveBeenCalledOnce();
    view.rerender(<PlaybackApiPanel playback={controller({ pending: "scan" })} />);
    expect(screen.getByRole("button", { name: "Scanning…" })).toBeDisabled();
    view.rerender(<PlaybackApiPanel playback={controller({ message: "Found Playback at 192.0.2.10:8080." })} />);
    expect(screen.getByRole("status")).toHaveTextContent("Found Playback");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Manual address"), "192.0.2.10");
    expect(screen.getByRole("button", { name: "Find / Scan" })).toBeDisabled();
    view.rerender(<PlaybackApiPanel playback={playback} />);
    fireEvent.click(screen.getByRole("button", { name: "Save address" }));
    expect(playback.save).toHaveBeenCalledWith({ enabled: true, host: "192.0.2.10", port: 8080, auto_scan: true });
    fireEvent.change(screen.getByLabelText("Manual address"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save address" }));
    expect(playback.save).toHaveBeenLastCalledWith({ enabled: true, host: null, port: 8080, auto_scan: true });
    fireEvent.click(screen.getByLabelText("Playback API on"));
    expect(playback.save).toHaveBeenLastCalledWith({ enabled: false, host: null, port: 8080, auto_scan: true });
  });

  it.each(["http://192.0.2.10", "192.0.2.10:8080", "bad host", "999.0.2.10", "a/b"])('rejects invalid manual address %s', (value) => {
    const playback = controller();
    render(<PlaybackApiPanel playback={playback} />);
    fireEvent.change(screen.getByLabelText("Manual address"), { target: { value } });
    expect(screen.getByRole("alert")).toHaveTextContent("without a URL");
    expect(screen.getByRole("button", { name: "Save address" })).toBeDisabled();
    expect(playback.save).not.toHaveBeenCalled();
  });
  it.each(["", "192.0.2.10", "playback.local", "127.0.0.1", "::1"])("accepts bare host %s", (host) => expect(validPlaybackHost(host)).toBe(true));

  it("requires confirmation, supports Cancel, sends only the discovery operation", async () => {
    const playback = controller();
    const user = userEvent.setup();
    render(<PlaybackApiPanel playback={playback} />);
    await user.click(screen.getByRole("button", { name: "Discover Song Order" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Next/Previous");
    expect(dialog).toHaveTextContent("2 seconds per song");
    expect(dialog).toHaveTextContent("must be stopped");
    expect(playback.discover).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discover Song Order" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Discover Song Order" }));
    expect(playback.discover).toHaveBeenCalledOnce();
  });

  it.each([
    { status: playbackStatus({ sources: { playback_api: { connected: false, reason: "Offline" }, midi: { connected: true, reason: "Connected" } }, active_source: "midi" }), reason: "Connect Playback API first." },
    { status: playbackStatus({ playing: true }), reason: "Stop Playback first." },
    { status: playbackStatus({ selected: false }), reason: "Select and enable Playback API first." },
  ])("disables discovery with API-only reason $reason", ({ status, reason }) => {
    render(<PlaybackApiPanel playback={controller({ status })} />);
    expect(screen.getByRole("button", { name: "Discover Song Order" })).toBeDisabled();
    expect(screen.getByText(reason)).toBeVisible();
  });

  it("rechecks playing state while the confirmation dialog is open", () => {
    const view = render(<PlaybackApiPanel playback={controller()} />);
    fireEvent.click(screen.getByRole("button", { name: "Discover Song Order" }));
    view.rerender(<PlaybackApiPanel playback={controller({ status: playbackStatus({ playing: true }) })} />);
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: "Discover Song Order" })).toBeDisabled();
  });

  it("renders progress, result, stale order and clear failure", () => {
    const view = render(<PlaybackApiPanel playback={controller({ status: playbackStatus({ discovery: "running", progress: 4 }) })} />);
    expect(screen.getByRole("status")).toHaveTextContent("4 steps");
    view.rerender(<PlaybackApiPanel playback={controller({ status: playbackStatus() })} />);
    expect(screen.getByRole("list", { name: "Discovered song order" })).toHaveTextContent("1 → ID 101");
    view.rerender(<PlaybackApiPanel playback={controller({ status: playbackStatus({ stale: true, discovery: "failed", last_error: "Playback disconnected during discovery." }) })} />);
    expect(screen.getByText(/Song order stale/)).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("disconnected during discovery");
  });

  it("renders typed recent events with song numbers and discovery tags, never invents unknown numbers", () => {
    const event = { type: "song.started", timestamp: 123.4, song_id: 202, position: 0, previous_song_id: null, continues_playing: false, playing: true, pad: false, setlist_version: 1, previous_version: null, section_id: null, active: null, reason: null, message_kind: null };
    render(<PlaybackApiPanel playback={controller({ events: [{ event, discovery: false }, { event: { ...event, type: "song.selected", song_id: 999 }, discovery: true }] })} />);
    const rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("song.selected (discovery)");
    expect(rows[1]).toHaveTextContent("Unknown");
    expect(rows[2]).toHaveTextContent("123.4song.started2");
  });
});

const scenarios = [
  { name: "API only", api: true, midi: false, active: "playback_api" as const },
  { name: "MIDI only", api: false, midi: true, active: "midi" as const },
  { name: "both", api: true, midi: true, active: "playback_api" as const },
  { name: "neither", api: false, midi: false, active: "none" as const },
];
describe("Unified Playback wording", () => {
  it.each(scenarios)("agrees across panel, dashboard and checklist: $name", ({ api, midi, active }) => {
    const connected = api || midi;
    const reason = active === "playback_api" ? "Connected via Playback API." : active === "midi" ? "Connected via MIDI." : "Playback disconnected; connect Playback API or MIDI.";
    const status = playbackStatus({ connected, active_source: active, reason, sources: {
      playback_api: { connected: api, reason: api ? "API healthy" : "API disconnected" },
      midi: { connected: midi, reason: midi ? "MIDI connected" : "MIDI disconnected" },
    } });
    panel(controller({ status }));
    expect(screen.getByText(reason)).toBeVisible();
    if (!api) expect(screen.getByText("Playback API not connected")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Advanced/ }));
    if (api && midi) expect(screen.getByText("MIDI connected - standing by (Playback API is in use)")).toBeVisible();
    const views = buildConnectionCardViews({ state: playbackState, settings: playbackSettings, playback: status, midi: null, propresenter: null, lights: null });
    expect(views.midi.status).toBe(connected ? "connected" : "disconnected");
    expect(views.midi.detail).toBe(reason);
    const checks = buildReadinessChecks({ state: playbackState, settings: playbackSettings, propresenter: null, live: true, views });
    expect(checks.find((check) => check.id === "midi")).toMatchObject({ passed: connected, detail: reason });
    render(<SetupChecklist state={playbackState} settings={playbackSettings} playback={status} midi={null} propresenter={null} live onOpen={vi.fn()} />);
    expect(screen.getAllByText(reason)).toHaveLength(2);
  });

  it.each([{ stale: true, song_order: [101, 202] }, { stale: false, song_order: [] }])("keeps connection green and action readiness separate for invalid API order %o", (mapping) => {
    const status = playbackStatus(mapping);
    const views = buildConnectionCardViews({ state: playbackState, settings: playbackSettings, playback: status, midi: null, propresenter: null, lights: null });
    const checks = buildReadinessChecks({ state: playbackState, settings: playbackSettings, live: true, propresenter: null, views });
    expect(views.midi.status).toBe("connected");
    expect(checks.find((check) => check.id === "midi")?.passed).toBe(true);
    expect(checks.find((check) => check.id === "playback-order")?.passed).toBe(false);
    panel(controller({ status }));
    expect(screen.getByText("Connected via Playback API.")).toBeVisible();
    render(<SetupChecklist state={playbackState} settings={playbackSettings} playback={status} midi={null} propresenter={null} live onOpen={vi.fn()} />);
    expect(screen.getAllByText(/Song order (stale|unknown)/)).toHaveLength(2);
  });

  it("does not gate MIDI action readiness on invalid API order", () => {
    const status = playbackStatus({ stale: true, active_source: "midi", reason: "Connected via MIDI." });
    const views = buildConnectionCardViews({ state: playbackState, settings: playbackSettings, playback: status, midi: null, propresenter: null, lights: null });
    expect(views.midi.actionNotice).toBeNull();
  });
});
