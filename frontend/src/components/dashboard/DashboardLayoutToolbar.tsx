export function DashboardLayoutToolbar({
  editing,
  eventsHidden,
  onAddSpacer,
  onCompact,
  onDone,
  onEdit,
  onReset,
  onToggleEventsHidden,
}: {
  editing: boolean;
  eventsHidden: boolean;
  onAddSpacer: () => void;
  onCompact: () => void;
  onDone: () => void;
  onEdit: () => void;
  onReset: () => void;
  onToggleEventsHidden: () => void;
}) {
  if (!editing) {
    return (
      <div className="mt-3 flex justify-end">
        <button
          className="rounded-lg border border-white/10 bg-black/25 px-3 py-2 text-xs font-bold uppercase tracking-wider text-slate-300 transition hover:border-white/20 hover:bg-slate-950/70 hover:text-white"
          onClick={onEdit}
          type="button"
        >
          Edit layout
        </button>
      </div>
    );
  }

  return (
    <div
      aria-label="Dashboard layout tools"
      className="mt-3 flex flex-wrap items-center justify-end gap-2 rounded-xl border border-[#ff6238]/25 bg-slate-950/80 p-3 shadow-panel"
      role="toolbar"
    >
      <span className="mr-auto text-xs font-bold uppercase tracking-[0.18em] text-[#ff9b7e]">
        Layout editing
      </span>
      <button className="rounded-lg border border-white/15 px-3 py-2 text-xs font-semibold text-slate-200 hover:bg-white/10" onClick={onAddSpacer} type="button">
        Add spacer
      </button>
      <button className="rounded-lg border border-white/15 px-3 py-2 text-xs font-semibold text-slate-200 hover:bg-white/10" onClick={onToggleEventsHidden} type="button">
        {eventsHidden ? "Show event stream" : "Hide event stream"}
      </button>
      <button className="rounded-lg border border-white/15 px-3 py-2 text-xs font-semibold text-slate-200 hover:bg-white/10" onClick={onCompact} type="button">
        Compact layout
      </button>
      <button className="rounded-lg border border-rose-400/30 px-3 py-2 text-xs font-semibold text-rose-200 hover:bg-rose-400/20" onClick={onReset} type="button">
        Reset layout
      </button>
      <button className="rounded-lg bg-[#ff6238] px-3 py-2 text-xs font-bold text-slate-950 hover:bg-[#ff805e]" onClick={onDone} type="button">
        Done
      </button>
    </div>
  );
}
