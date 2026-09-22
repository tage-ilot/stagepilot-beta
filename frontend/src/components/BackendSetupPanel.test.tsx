import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import * as desktop from "../desktop";
import type { ApplicationState } from "../types";
import { BackendSetupPanel } from "./BackendSetupPanel";

vi.mock("../api", async (original) => ({
  ...await original<typeof import("../api")>(),
  getRemoteStatus: vi.fn(), getRemoteUsers: vi.fn(), setRemoteEnabled: vi.fn(),
  bootstrapRemote: vi.fn(), createRemoteUser: vi.fn(),
  updateRemoteUser: vi.fn(), deleteRemoteUser: vi.fn(), regenerateRemote: vi.fn(),
}));
vi.mock("../desktop", async (original) => ({
  ...await original<typeof import("../desktop")>(),
  setRemoteAutostart: vi.fn(),
}));

const off: api.RemoteStatus = {available: true, provisioned: true, credential_available: true, enabled: false, state: "off", url: null, needs_operator: true, message: null, temporary_url: true};
const operator: api.RemoteUser = {id: "one", email: "operator@example.test", role: "Operator", enabled: true};

const state: ApplicationState = {
  revision: 1,
  updated_at: "2026-07-14T12:00:00Z",
  application_status: "running",
  plan: null,
  current_song: null,
  next_song: null,
  current_song_index: null,
  planning_center_status: "disconnected",
  midi_status: "disconnected",
  propresenter_status: "disconnected",
  lights_status: "disconnected",
  service_load: {
    status: "idle", target_date: null, candidates: [], skipped_items: [],
    message: null, is_stale: false, last_attempt_at: null,
  },
  timer: { status: "idle", duration_seconds: null, started_at: null, last_error: null },
  plugins: {},
  recent_events: [],
  recent_errors: [],
  last_successful_plan_reload_at: null,
  last_action: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getRemoteStatus).mockResolvedValue(off);
  vi.mocked(api.getRemoteUsers).mockResolvedValue([]);
  vi.mocked(desktop.setRemoteAutostart).mockResolvedValue();
});

function renderPanel() {
  render(
    <BackendSetupPanel
      state={state}
      health={null}
      live={false}
      onClose={vi.fn()}
      settings={null}
      error={null}
      message={null}
      pending={false}
      onSave={vi.fn()}
    />,
  );
}

describe("Remote Access checkbox", () => {
  it("checking the box enables Remote and expands the panel", async () => {
    vi.mocked(api.getRemoteStatus).mockResolvedValue({...off, needs_operator: false});
    vi.mocked(api.setRemoteEnabled).mockImplementation(async (enabled) => {
      const next = {...off, enabled, state: enabled ? "enabling" as const : "off" as const, needs_operator: false};
      vi.mocked(api.getRemoteStatus).mockResolvedValue(next);
      return next;
    });
    renderPanel();
    const checkbox = await screen.findByRole("checkbox", {name: /Remote Access/});
    expect(checkbox).not.toBeChecked();

    fireEvent.click(checkbox);
    await waitFor(() => expect(api.setRemoteEnabled).toHaveBeenCalledWith(true));
    expect(desktop.setRemoteAutostart).toHaveBeenCalledWith(true);
    await waitFor(() => expect(checkbox).toBeChecked());
    expect(checkbox.closest("label")).toHaveAttribute("aria-expanded", "true");
  });

  it("unchecking with confirmation disables Remote and collapses the panel", async () => {
    vi.mocked(api.getRemoteStatus).mockResolvedValue({...off, enabled: true, state: "connected", needs_operator: false, url: "https://test.trycloudflare.com"});
    vi.mocked(api.getRemoteUsers).mockResolvedValue([operator]);
    vi.mocked(api.setRemoteEnabled).mockImplementation(async (enabled) => {
      const next = {...off, enabled, state: enabled ? "connected" as const : "off" as const, needs_operator: false, url: enabled ? "https://test.trycloudflare.com" : null};
      vi.mocked(api.getRemoteStatus).mockResolvedValue(next);
      return next;
    });
    renderPanel();
    const checkbox = await screen.findByRole("checkbox", {name: /Remote Access/});
    await waitFor(() => expect(checkbox).toBeChecked());

    fireEvent.click(checkbox);
    expect(await screen.findByRole("group", {name: "Confirm disable Remote Access"})).toBeInTheDocument();
    expect(api.setRemoteEnabled).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", {name: "Confirm disable"}));
    await waitFor(() => expect(api.setRemoteEnabled).toHaveBeenCalledWith(false));
    expect(desktop.setRemoteAutostart).toHaveBeenCalledWith(false);
    await waitFor(() => expect(checkbox).not.toBeChecked());
    expect(checkbox.closest("label")).toHaveAttribute("aria-expanded", "false");
  });

  it("unchecking then cancelling reverts the checkbox to checked with Remote Access still enabled", async () => {
    vi.mocked(api.getRemoteStatus).mockResolvedValue({...off, enabled: true, state: "connected", needs_operator: false, url: "https://test.trycloudflare.com"});
    renderPanel();
    const checkbox = await screen.findByRole("checkbox", {name: /Remote Access/});
    await waitFor(() => expect(checkbox).toBeChecked());

    fireEvent.click(checkbox);
    expect(await screen.findByRole("group", {name: "Confirm disable Remote Access"})).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", {name: "Cancel"}));
    expect(api.setRemoteEnabled).not.toHaveBeenCalled();
    expect(checkbox).toBeChecked();
    expect(screen.queryByRole("group", {name: "Confirm disable Remote Access"})).not.toBeInTheDocument();
  });

  it("the needs_operator first-run path still surfaces the bootstrap UI instead of silently enabling", async () => {
    vi.mocked(api.getRemoteStatus).mockResolvedValue(off);
    renderPanel();
    const checkbox = await screen.findByRole("checkbox", {name: /Remote Access/});
    expect(checkbox).not.toBeChecked();

    fireEvent.click(checkbox);
    expect(await screen.findByRole("heading", {name: "Create first Operator"})).toBeInTheDocument();
    expect(api.setRemoteEnabled).not.toHaveBeenCalled();
    expect(checkbox).not.toBeChecked();

    vi.mocked(api.bootstrapRemote).mockResolvedValue(operator);
    vi.mocked(api.setRemoteEnabled).mockImplementation(async (enabled) => {
      const next = {...off, enabled, state: enabled ? "enabling" as const : "off" as const, needs_operator: false};
      vi.mocked(api.getRemoteStatus).mockResolvedValue(next);
      return next;
    });
    fireEvent.change(screen.getByLabelText("Email"), {target: {value: operator.email}});
    fireEvent.change(screen.getByLabelText("Password"), {target: {value: "long-test-password"}});
    fireEvent.click(screen.getByRole("button", {name: "Create Operator and enable"}));
    await waitFor(() => expect(api.setRemoteEnabled).toHaveBeenCalledWith(true));
    expect(api.bootstrapRemote).toHaveBeenCalledWith(operator.email, "long-test-password");
    await waitFor(() => expect(checkbox).toBeChecked());
  });
});
