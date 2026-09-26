import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApplicationState, PlanningCenterStatusResponse, SettingsResponse } from "../types";
import { PlanningCenterSetupPanel } from "./PlanningCenterSetupPanel";

const desktop = vi.hoisted(() => ({
  openExternalUrl: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../desktop", () => ({
  openExternalUrl: desktop.openExternalUrl,
}));

const settings: SettingsResponse = {
  settings: {
    schema_version: 1,
    onboarding: { general_completed: false },
    integration_modes: {
      service_source: "demo",
      midi_source: "simulated",
      timer_output: "simulated",
    },
    timezone: "America/Los_Angeles",
    log_level: "INFO",
    server_port: 8765,
    planning_center: {
      app_id: "saved-app-id",
      service_type_id: "sunday",
      plan_title_preference: "Sunday Morning",
      preferred_service_time: "09:00",
      upcoming_lookahead_days: 30,
      request_timeout_seconds: 10,
    },
    midi: {
      enabled: false,
      input_name: null,
      channel: 1,
      note: 112,
      mappings: {
        start_next: 100,
        restart_current: 101,
        previous: 102,
        next: 103,
        reload_plan: 104,
        stop_timer: 105,
      },
      debounce_ms: 250,
    },
    lights: {
      enabled: false,
      output_name: null,
      channel: 1,
      pulse_ms: 100,
      cue_maps: {},
    },
    propresenter: {
      enabled: false,
      host: "127.0.0.1",
      port: 1025,
      timer_name: "Song Countdown",
      request_timeout_seconds: 3,
      reconnect_initial_seconds: 1,
      reconnect_max_seconds: 30,
      health_check_interval_seconds: 10,
    },
  },
  planning_center_secret_saved: true,
  warning: null,
  restart_required: false,
};

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
    status: "idle",
    target_date: null,
    candidates: [],
    skipped_items: [],
    message: null,
    is_stale: false,
    last_attempt_at: null,
  },
  timer: {
    status: "idle",
    duration_seconds: null,
    started_at: null,
    last_error: null,
  },
  plugins: {},
  recent_events: [],
  recent_errors: [],
  last_successful_plan_reload_at: null,
  last_action: null,
};

function renderPanel({
  onTest = vi.fn(),
  onSave = vi.fn(),
  onLoadServiceTypes = vi.fn(),
  onSignInOAuth = vi.fn(),
  onDisconnectOAuth = vi.fn(),
  panelSettings = settings,
  status = {
    connection_status: "disconnected",
    configured: true,
    app_id: "saved-app-id",
    service_type_id: "sunday",
    planning_center_secret_saved: true,
    connection_method: "manual",
    oauth_connected: false,
    oauth_needs_reconnect: false,
    detail: null,
  } as PlanningCenterStatusResponse,
  pendingOperation = null,
} = {}) {
  render(
    <PlanningCenterSetupPanel
      error={null}
      message={null}
      onClose={vi.fn()}
      onDisconnectOAuth={onDisconnectOAuth}
      onLoadServiceTypes={onLoadServiceTypes}
      onReload={vi.fn()}
      onSave={onSave}
      onSelectPlan={vi.fn()}
      onSignInOAuth={onSignInOAuth}
      onTest={onTest}
      pendingAction={null}
      pendingOperation={pendingOperation}
      pendingPlanId={null}
      serviceTypes={[
        { id: "sunday", name: "Sunday Morning" },
        { id: "wednesday", name: "Wednesday Service" },
      ]}
      settings={panelSettings}
      state={state}
      status={status}
    />,
  );
}

describe("PlanningCenterSetupPanel", () => {
  it.each([
    ["manual", false],
    ["oauth", true],
  ] as const)("defaults Manual API Connection closed for %s connections", (connectionMethod, oauthConnected) => {
    renderPanel({
      status: {
        connection_status: oauthConnected ? "connected" : "disconnected",
        configured: true,
        app_id: oauthConnected ? null : "saved-app-id",
        service_type_id: "sunday",
        planning_center_secret_saved: !oauthConnected,
        connection_method: connectionMethod,
        oauth_connected: oauthConnected,
        oauth_needs_reconnect: false,
        detail: null,
      },
    });

    const summary = screen.getByText("Manual API Connection");
    const disclosure = summary.closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    expect(screen.queryByText("Advanced / manual connection")).not.toBeInTheDocument();
  });

  it("keeps shared service plan settings visible outside the closed manual disclosure", () => {
    renderPanel();

    const disclosure = screen.getByText("Manual API Connection").closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    expect(screen.getByRole("heading", { name: "Service Plan Settings" })).toBeInTheDocument();

    for (const label of [
      "Service type",
      "Timezone",
      "Plan title preference",
      "Preferred service time",
    ]) {
      expect(screen.getByLabelText(label).closest("details")).toBeNull();
    }
    for (const name of ["Load service types", "Save settings", "Load today’s plan"]) {
      expect(screen.getByRole("button", { name }).closest("details")).toBeNull();
    }
  });

  it("enables OAuth service actions without a manual Application ID or secret", async () => {
    const onLoadServiceTypes = vi.fn();
    const onSave = vi.fn();
    const user = userEvent.setup();
    renderPanel({
      onLoadServiceTypes,
      onSave,
      panelSettings: {
        ...settings,
        planning_center_secret_saved: false,
        settings: {
          ...settings.settings,
          planning_center: {
            ...settings.settings.planning_center,
            app_id: null,
          },
        },
      },
      status: {
        connection_status: "connected",
        configured: true,
        app_id: null,
        service_type_id: "sunday",
        planning_center_secret_saved: false,
        connection_method: "oauth",
        oauth_connected: true,
        oauth_needs_reconnect: false,
        detail: null,
      },
    });

    const loadButton = screen.getByRole("button", { name: "Load service types" });
    const saveButton = screen.getByRole("button", { name: "Save settings" });
    expect(loadButton).toBeEnabled();
    expect(saveButton).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Test connection" })).not.toBeInTheDocument();

    await user.click(loadButton);
    await user.click(saveButton);
    expect(onLoadServiceTypes).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ app_id: "", service_type_id: "sunday" }),
      "America/Los_Angeles",
    );
  });

  it("shows PAT instructions after a one-second hover and keeps the linked panel interactive", () => {
    vi.useFakeTimers();
    try {
      renderPanel();
      fireEvent.click(screen.getByText("Manual API Connection"));

      const helpButton = screen.getByRole("button", {
        name: "How to get a Planning Center Personal Access Token",
      });
      expect(helpButton).toHaveClass("leading-none", "pb-px");
      const hoverArea = helpButton.parentElement!;
      const tooltip = screen.getByRole("tooltip");

      fireEvent.mouseEnter(hoverArea);
      act(() => vi.advanceTimersByTime(499));
      expect(tooltip).toHaveClass("invisible");
      act(() => vi.advanceTimersByTime(1));
      expect(tooltip).toHaveClass("visible");

      const link = screen.getByRole("link", { name: "Personal Access Tokens page" });
      expect(link).toHaveAttribute(
        "href",
        "https://api.planningcenteronline.com/personal_access_tokens",
      );
      fireEvent.click(link);
      expect(desktop.openExternalUrl).toHaveBeenCalledWith(
        "https://api.planningcenteronline.com/personal_access_tokens",
      );
      fireEvent.mouseEnter(tooltip);
      expect(tooltip).toHaveClass("visible");

      fireEvent.mouseLeave(hoverArea);
      expect(tooltip).toHaveClass("invisible");
      fireEvent.click(helpButton);
      expect(tooltip).toHaveClass("visible");
    } finally {
      vi.useRealTimers();
    }
  });

  it("populates saved public settings while keeping the credential masked", () => {
    renderPanel();

    expect(screen.getByLabelText("Application ID")).toHaveValue("saved-app-id");
    expect(screen.getByLabelText("Secret")).toHaveValue("");
    expect(screen.getByLabelText("Secret")).toHaveAttribute(
      "placeholder",
      "Saved securely — leave blank to keep",
    );
    expect(screen.getByLabelText("Service type")).toHaveValue("sunday");
    expect(screen.getByLabelText("Timezone")).toHaveValue("America/Los_Angeles");
  });

  it("tests temporary credentials and saves a discovered service type", async () => {
    const onTest = vi.fn();
    const onSave = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onSave, onTest });

    await user.click(screen.getByText("Manual API Connection"));
    await user.type(screen.getByLabelText("Secret"), "replacement-secret");
    await user.click(screen.getByRole("button", { name: "Test connection" }));

    expect(onTest).toHaveBeenCalledWith({
      app_id: "saved-app-id",
      secret: "replacement-secret",
    });

    expect(screen.queryByLabelText("Service source")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Service type"), "wednesday");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        app_id: "saved-app-id",
        secret: "replacement-secret",
        service_type_id: "wednesday",
        plan_title_preference: "Sunday Morning",
        preferred_service_time: "09:00",
      }),
      "America/Los_Angeles",
    );
  });

  it("shows the sign-in button by default and triggers the start-flow call", async () => {
    const onSignInOAuth = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onSignInOAuth });

    const button = screen.getByRole("button", { name: "Sign in with Planning Center" });
    await user.click(button);

    expect(onSignInOAuth).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Connected to Planning Center")).not.toBeInTheDocument();
  });

  it("shows the connected state and hides the sign-in button once OAuth-connected", () => {
    renderPanel({
      status: {
        connection_status: "connected",
        configured: true,
        app_id: null,
        service_type_id: "sunday",
        planning_center_secret_saved: false,
        connection_method: "oauth",
        oauth_connected: true,
        oauth_needs_reconnect: false,
        detail: null,
      },
    });

    expect(screen.getByText("Connected to Planning Center")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Sign in with Planning Center" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeInTheDocument();
  });

  it("the advanced/manual section still works independently when disconnected", async () => {
    const onSave = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onSave });

    await user.click(screen.getByText("Manual API Connection"));
    await user.clear(screen.getByLabelText("Application ID"));
    await user.type(screen.getByLabelText("Application ID"), "manual-app-id");
    await user.type(screen.getByLabelText("Secret"), "manual-secret");
    await user.selectOptions(screen.getByLabelText("Service type"), "wednesday");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        app_id: "manual-app-id",
        secret: "manual-secret",
        service_type_id: "wednesday",
      }),
      "America/Los_Angeles",
    );
  });

  it("renders the reconnect-needed state distinctly from a generic error banner", () => {
    renderPanel({
      status: {
        connection_status: "error",
        configured: true,
        app_id: null,
        service_type_id: "sunday",
        planning_center_secret_saved: false,
        connection_method: "oauth",
        oauth_connected: false,
        oauth_needs_reconnect: true,
        detail: null,
      },
    });

    const reconnectAlert = screen.getByRole("alert");
    expect(reconnectAlert).toHaveTextContent("Your Planning Center connection has expired");
    expect(
      screen.getByRole("button", { name: "Reconnect to Planning Center" }),
    ).toBeInTheDocument();
    // The generic error/message banner (a <p>, not role="alert") is a
    // separate code path and must not render for this state.
    expect(screen.queryByText("Planning Center connection test failed.")).not.toBeInTheDocument();
  });
});
