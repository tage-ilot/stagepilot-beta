import { useEffect, useState, type FormEvent, type ReactNode } from "react";

import { getDashboardAuthStatus, loginDashboard } from "../api";
import { isDesktopShell } from "../desktop";

export function WebDashboardPinGate({ children }: { children: ReactNode }) {
  const [authorized, setAuthorized] = useState(isDesktopShell());
  const [checking, setChecking] = useState(!isDesktopShell());
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (isDesktopShell()) return;
    let active = true;
    void getDashboardAuthStatus()
      .then((status) => {
        if (active) setAuthorized(status.authenticated || !status.required);
      })
      .catch((cause) => {
        if (active) {
          setError(cause instanceof Error ? cause.message : "Dashboard access check failed.");
        }
      })
      .finally(() => {
        if (active) setChecking(false);
      });
    return () => {
      active = false;
    };
  }, []);

  if (authorized) return children;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const status = await loginDashboard(pin);
      if (status.authenticated) setAuthorized(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Incorrect dashboard PIN.");
    } finally {
      setPending(false);
    }
  };

  return (
    <main className="grid min-h-screen place-items-center px-6 text-center">
      <form
        className="w-full max-w-sm rounded-2xl border border-white/10 bg-slate-950/75 p-7 shadow-2xl shadow-black/40 backdrop-blur-xl"
        onSubmit={(event) => void submit(event)}
      >
        <h1 className="select-none font-brand text-4xl leading-none text-white sm:text-6xl lg:text-7xl">StagePilot</h1>
        {checking && <span className="loading-spinner-circular loading-spinner-circular--mobile-only" aria-hidden="true" />}
        <p className="mt-5 text-sm text-slate-300">
          {checking ? "Checking dashboard access…" : "Enter the dashboard PIN to continue."}
        </p>
        {!checking && (
          <>
            <label className="mt-5 block text-left text-xs font-bold uppercase tracking-wider text-slate-400">
              Dashboard PIN
              <input
                autoComplete="current-password"
                autoFocus
                className="mt-2 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-3 text-center font-mono text-xl tracking-[0.35em] text-white outline-none focus:border-sky-300/50"
                disabled={pending}
                inputMode="numeric"
                maxLength={64}
                onChange={(event) => setPin(event.target.value)}
                required
                type="password"
                value={pin}
              />
            </label>
            <button
              className="mt-4 w-full rounded-lg bg-sky-400 px-4 py-3 text-sm font-bold text-slate-950 transition hover:bg-sky-300 disabled:opacity-50"
              disabled={pending || pin.length < 4}
              type="submit"
            >
              {pending ? "Checking…" : "Open dashboard"}
            </button>
          </>
        )}
        {error && <p aria-live="assertive" className="mt-4 text-sm text-rose-300">{error}</p>}
      </form>
    </main>
  );
}
