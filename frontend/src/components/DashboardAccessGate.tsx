import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import { getAccess, loginDashboard, loginRemote, logoutRemote } from "../api";
import { isDesktopShell } from "../desktop";
import { AccessContext } from "../access/AccessContext";
import { DESKTOP_ACCESS, NO_CAPABILITIES, invalidateAccess, onAccessInvalidated, setApiAccess } from "../access/accessState";
import type { DashboardAccess } from "../types";

export function DashboardAccessGate({ children }: { children: ReactNode }) {
  const [access, setAccess] = useState<DashboardAccess | null>(() => isDesktopShell() ? DESKTOP_ACCESS : null);
  const [checking, setChecking] = useState(!isDesktopShell());
  const [pin, setPin] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const requestId = useRef(0);

  const accept = useCallback((next: DashboardAccess) => {
    setApiAccess(next);
    setAccess(next);
    setChecking(false);
  }, []);

  const check = useCallback(async () => {
    const id = ++requestId.current;
    setChecking(true);
    setError(null);
    try {
      const next = await getAccess();
      if (id === requestId.current) accept(next);
    } catch (cause) {
      if (id === requestId.current) {
        setError(cause instanceof Error ? cause.message : "Dashboard access check failed.");
      }
    } finally {
      if (id === requestId.current) setChecking(false);
    }
  }, [accept]);

  useEffect(() => {
    const unsubscribe = onAccessInvalidated(() => {
      requestId.current += 1;
      setAccess((current) => current ? {
        ...current, authenticated: false, capabilities: NO_CAPABILITIES, csrf_token: null,
      } : null);
      setChecking(false);
      setPending(false);
      setPin("");
      setPassword("");
      setError("Your access has ended. Please sign in again.");
    });
    if (isDesktopShell()) setApiAccess(DESKTOP_ACCESS);
    else void check();
    return () => {
      requestId.current += 1;
      unsubscribe();
      setApiAccess(null);
    };
  }, [check]);

  useEffect(() => {
    if (!access?.authenticated || !access.expires_at) return;
    const timer = window.setTimeout(() => invalidateAccess(),
      Math.max(0, access.expires_at * 1_000 - Date.now()));
    return () => window.clearTimeout(timer);
  }, [access]);

  const signOut = async () => {
    setPending(true);
    setError(null);
    try {
      await logoutRemote();
      invalidateAccess();
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign out failed. Try again.");
    } finally {
      setPending(false);
    }
  };

  if (access?.authenticated && access.capabilities.canRead) {
    return (
      <AccessContext.Provider value={access}>
        {access.mode === "remote" && (
          <div className="flex flex-wrap items-center justify-end gap-3 px-6 py-2 text-xs text-slate-300">
            <span>Remote · {access.user?.role} · {access.user?.email}</span>
            {!access.capabilities.canOperate && <span>Read-only access</span>}
            <button className="rounded border border-white/20 px-3 py-1" disabled={pending}
              onClick={() => void signOut()} type="button">Sign out</button>
            {error && <span role="alert">{error}</span>}
          </div>
        )}
        {children}
      </AccessContext.Provider>
    );
  }

  const remote = access?.authentication === "password";
  const hasLogin = remote || access?.authentication === "pin";
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!hasLogin || pending) return;
    const id = ++requestId.current;
    setPending(true);
    setError(null);
    try {
      if (remote) await loginRemote(email, password);
      else await loginDashboard(pin);
      const next = await getAccess();
      if (id === requestId.current) {
        accept(next);
        if (!next.authenticated) setError("Sign-in could not be confirmed. Please try again.");
      }
    } catch (cause) {
      if (id === requestId.current) setError(cause instanceof Error ? cause.message : "Sign in failed.");
    } finally {
      if (id === requestId.current) {
        setPassword("");
        setPin("");
        setPending(false);
      }
    }
  };

  const inputClass = "mt-2 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-3 text-white outline-none focus:border-sky-300/50";
  return (
    <main className="grid min-h-screen place-items-center px-6 text-center">
      <form className="w-full max-w-sm rounded-2xl border border-white/10 bg-slate-950/75 p-7 shadow-2xl shadow-black/40 backdrop-blur-xl"
        onSubmit={(event) => void submit(event)}>
        <h1 className="select-none font-brand text-4xl leading-none text-white sm:text-6xl lg:text-7xl">StagePilot</h1>
        {checking && <span className="loading-spinner-circular loading-spinner-circular--mobile-only" aria-hidden="true" />}
        <p className="mt-5 text-sm text-slate-300">
          {checking ? "Checking dashboard access…" : remote ? "Sign in to StagePilot Remote."
            : hasLogin ? "Enter the dashboard PIN to continue." : "Dashboard access is unavailable."}
        </p>
        {!checking && hasLogin && <>
          {remote ? <>
            <label className="mt-5 block text-left text-xs font-bold uppercase tracking-wider text-slate-400">
              Email
              <input autoComplete="username" autoFocus className={inputClass} disabled={pending}
                maxLength={254} onChange={(event) => setEmail(event.target.value)} required type="email" value={email} />
            </label>
            <label className="mt-5 block text-left text-xs font-bold uppercase tracking-wider text-slate-400">
              Password
              <input autoComplete="current-password" className={inputClass} disabled={pending}
                maxLength={1024} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} />
            </label>
          </> : <label className="mt-5 block text-left text-xs font-bold uppercase tracking-wider text-slate-400">
            Dashboard PIN
            <input autoComplete="current-password" autoFocus
              className={`${inputClass} text-center font-mono text-xl tracking-[0.35em]`} disabled={pending}
              inputMode="numeric" maxLength={64} onChange={(event) => setPin(event.target.value)} required type="password" value={pin} />
          </label>}
          <button className="mt-4 w-full rounded-lg bg-sky-400 px-4 py-3 text-sm font-bold text-slate-950 transition hover:bg-sky-300 disabled:opacity-50"
            disabled={pending || (remote ? !email || !password : pin.length < 4)} type="submit">
            {pending ? "Checking…" : remote ? "Sign in" : "Open dashboard"}
          </button>
        </>}
        {!checking && !hasLogin && <button className="mt-4 rounded border border-white/20 px-4 py-2"
          onClick={() => void check()} type="button">Retry access check</button>}
        {error && <p aria-live="assertive" className="mt-4 text-sm text-rose-300">{error}</p>}
      </form>
    </main>
  );
}
