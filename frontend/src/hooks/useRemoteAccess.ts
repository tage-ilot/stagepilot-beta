import { useEffect, useRef, useState } from "react";
import { useDashboardAccess } from "../access/AccessContext";
import { invalidateAccess } from "../access/accessState";
import {
  ApiError, getRemoteStatus, getRemoteUsers, setRemoteEnabled,
  type RemoteStatus, type RemoteUser,
} from "../api";
import { setRemoteAutostart } from "../desktop";

export function friendlyRemoteError(cause: unknown): string {
  if (cause instanceof ApiError) {
    if (cause.status === 401 || cause.status === 403) return "Your access has changed. Please sign in again.";
    if (cause.status === 409) return "The change could not be applied. Keep at least one enabled Operator and use a unique email. Refresh before trying again.";
    if (cause.status === 422) return "Use a valid email and a password of at least 12 characters.";
    if (cause.status === 429) return "Too many requests. Please wait before trying again.";
    // The backend already translates enable/regenerate failures (enrollment
    // limit, unreachable control plane, revoked credential, etc.) into a
    // short, specific, end-user-safe message -- surface it verbatim rather
    // than collapsing everything to the generic fallback below.
    if (cause.status === 503 && cause.message) return cause.message;
  }
  return "Remote Access is unavailable. Local StagePilot is unaffected. Try again shortly.";
}

/**
 * Single source of truth for Remote Access status and the enable/disable
 * lifecycle. Shared between the BackendSetupPanel checkbox (which owns
 * enabling/disabling and expand/collapse) and RemoteAccessPanel (which
 * renders status, users, and link management using the same state so the
 * visual checkbox state and the real enabled state never diverge).
 */
export function useRemoteAccess() {
  const access = useDashboardAccess();
  const [status, setStatus] = useState<RemoteStatus | null>(null);
  const [users, setUsers] = useState<RemoteUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [bootstrap, setBootstrap] = useState(false);
  const [confirmDisable, setConfirmDisable] = useState(false);
  const [checkboxBusy, setCheckboxBusy] = useState(false);
  const alive = useRef(true);
  const inFlight = useRef(false);
  const canManage = access.authenticated && access.capabilities.canConfigure;
  const local = access.mode !== "remote";

  useEffect(() => {
    alive.current = true;
    if (!canManage) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!inFlight.current) {
        try {
          const next = await getRemoteStatus();
          if (!cancelled) setStatus(next);
        } catch (cause) {
          if (!cancelled) { setStatus(null); setError(friendlyRemoteError(cause)); }
        }
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 2000);
    };
    void poll();
    void getRemoteUsers().then((next) => { if (!cancelled) setUsers(next); }).catch((cause) => {
      if (!cancelled) setError(friendlyRemoteError(cause));
    });
    return () => { cancelled = true; alive.current = false; clearTimeout(timer); };
  }, [canManage]);

  async function run(work: () => Promise<unknown>, selfChange = false) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(null); setNotice(null);
    try {
      await work();
      if (selfChange && !local) { invalidateAccess(); return; }
      const [next, nextUsers] = await Promise.all([getRemoteStatus(), getRemoteUsers()]);
      if (alive.current) { setStatus(next); setUsers(nextUsers); setNotice("Changes saved."); }
    } catch (cause) {
      if (alive.current) setError(friendlyRemoteError(cause));
      throw cause;
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(false);
    }
  }

  /** The checkbox was checked. Enables Remote, or opens the bootstrap flow
   * when there is no Operator yet. Never marks the checkbox checked
   * prematurely -- it stays reflecting status.enabled, set only once the
   * enable call actually succeeds. */
  function requestEnable() {
    if (status?.enabled) return;
    if (status?.needs_operator) {
      if (local) setBootstrap(true);
      return;
    }
    setCheckboxBusy(true);
    void run(async () => {
      await setRemoteEnabled(true);
      await setRemoteAutostart(true);
    }).catch(() => undefined).finally(() => setCheckboxBusy(false));
  }

  /** The checkbox was unchecked. Opens the disable confirmation; the real
   * disable only happens once the user confirms. */
  function requestDisable() {
    if (!status?.enabled) return;
    setConfirmDisable(true);
  }

  function cancelDisable() {
    setConfirmDisable(false);
  }

  function confirmDisableAccept() {
    setConfirmDisable(false);
    setCheckboxBusy(true);
    void run(async () => {
      await setRemoteEnabled(false);
      await setRemoteAutostart(false);
    }, true).catch(() => undefined).finally(() => setCheckboxBusy(false));
  }

  const enabled = Boolean(status?.enabled);
  // The setup panel should stay expanded whenever Remote Access is actually
  // enabled, while a toggle is in flight, or while the confirm/bootstrap
  // dialogs need to be visible -- collapsing those would hide them.
  const panelOpen = enabled || checkboxBusy || confirmDisable || bootstrap;

  return {
    access, status, users, setUsers, error, setError, notice, setNotice,
    busy, bootstrap, setBootstrap, confirmDisable, checkboxBusy,
    canManage, local, run,
    enabled, panelOpen,
    requestEnable, requestDisable, cancelDisable, confirmDisableAccept,
  };
}

export type RemoteAccessControl = ReturnType<typeof useRemoteAccess>;
