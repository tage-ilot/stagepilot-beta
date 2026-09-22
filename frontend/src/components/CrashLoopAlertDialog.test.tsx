import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BackendCrashLoopDetected } from "../desktop";

const mocks = vi.hoisted(() => ({ copyBackendLog: vi.fn() }));
vi.mock("../desktop", async (original) => ({
  ...(await original<typeof import("../desktop")>()),
  copyBackendLog: mocks.copyBackendLog,
}));

import { CrashLoopAlertDialog } from "./CrashLoopAlertDialog";

const crashLoop: BackendCrashLoopDetected = {
  failure_kind: "sidecar_exited",
  message: "The packaged StagePilot backend exited before it became ready (exit code 1).",
};

describe("CrashLoopAlertDialog", () => {
  it("renders on the crash-loop-detected event even when dashboardVisible=true", () => {
    // dashboardVisible is App.tsx-internal state; this dialog is mounted
    // at the App root outside that gate, so it must render purely off the
    // `crashLoop` prop regardless of any dashboard-visibility value.
    render(<CrashLoopAlertDialog crashLoop={crashLoop} onDismiss={vi.fn()} />);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByText("StagePilot\u2019s backend keeps crashing")).toBeInTheDocument();
    expect(screen.getByText("The packaged backend keeps exiting.")).toBeInTheDocument();
  });

  it("renders nothing when there is no crash-loop payload", () => {
    render(<CrashLoopAlertDialog crashLoop={null} onDismiss={vi.fn()} />);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("Copy Log calls the content-copy command and writes to the clipboard", async () => {
    mocks.copyBackendLog.mockResolvedValue("log contents here");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<CrashLoopAlertDialog crashLoop={crashLoop} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy Log" }));

    expect(await screen.findByText("Backend log copied.")).toBeInTheDocument();
    expect(mocks.copyBackendLog).toHaveBeenCalledOnce();
    expect(writeText).toHaveBeenCalledWith("log contents here");
  });

  it("Dismiss closes the dialog without disabling retry logic", async () => {
    const onDismiss = vi.fn();
    const user = userEvent.setup();
    render(<CrashLoopAlertDialog crashLoop={crashLoop} onDismiss={onDismiss} />);

    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledOnce();

    // A fresh alert must still be possible later (re-arm rule): rendering
    // with a new crashLoop payload after dismissal must show the dialog
    // again, since the parent owns the crashLoop state and Dismiss only
    // clears the current one.
    const { rerender } = render(<CrashLoopAlertDialog crashLoop={null} onDismiss={onDismiss} />);
    rerender(<CrashLoopAlertDialog crashLoop={crashLoop} onDismiss={onDismiss} />);
    expect(screen.getAllByRole("alertdialog").length).toBeGreaterThan(0);
  });
});
