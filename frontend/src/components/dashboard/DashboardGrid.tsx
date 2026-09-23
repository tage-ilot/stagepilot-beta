import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { GridStack, type GridItemHTMLElement } from "gridstack";
import "gridstack/dist/gridstack.min.css";

import {
  createDefaultDashboardLayout,
  createSpacer,
  dashboardModeForWidth,
  moveDashboardItem,
  orderedLayoutItems,
} from "./dashboardLayout";
import {
  loadDashboardLayout,
  saveDashboardLayout,
} from "./dashboardLayoutStorage";
import {
  DASHBOARD_COLUMNS,
  type DashboardItemId,
  type DashboardLayoutItem,
  type DashboardLayoutMode,
  type DashboardLayoutState,
  type DashboardSpacerId,
  type DashboardWidgetId,
} from "./dashboardLayoutTypes";
import { DASHBOARD_WIDGETS } from "./dashboardWidgetRegistry";
import { DashboardLayoutToolbar } from "./DashboardLayoutToolbar";
import { DashboardSpacer } from "./DashboardSpacer";
import { DashboardWidgetFrame } from "./DashboardWidgetFrame";

const CELL_HEIGHT = 28;
const GRID_ITEM_VERTICAL_INSET = 10;

const contentRows = (
  target: HTMLElement,
  constraints: { minH?: number; maxH?: number },
) => {
  const previousHeight = target.style.height;
  const previousMaxHeight = target.style.maxHeight;
  const previousOverflow = target.style.overflow;
  target.style.height = "auto";
  target.style.maxHeight = "none";
  target.style.overflow = "visible";

  let naturalHeight = target.scrollHeight;
  for (const child of target.querySelectorAll<HTMLElement>(
    ".overflow-auto, [data-autosize-content]",
  )) {
    naturalHeight += Math.max(0, child.scrollHeight - child.clientHeight);
  }

  target.style.height = previousHeight;
  target.style.maxHeight = previousMaxHeight;
  target.style.overflow = previousOverflow;

  const rows = Math.max(
    constraints.minH ?? 1,
    Math.ceil((naturalHeight + GRID_ITEM_VERTICAL_INSET) / CELL_HEIGHT),
  );
  return constraints.maxH ? Math.min(rows, constraints.maxH) : rows;
};

const bottom = (items: DashboardLayoutItem[]) =>
  items.reduce((maximum, item) => Math.max(maximum, item.y + item.h), 0);

const mobileItems = (layout: DashboardLayoutState): DashboardLayoutItem[] => {
  const desktopById = new Map(layout.desktop.map((item) => [item.id, item]));
  let y = 0;
  return layout.mobileOrder.map((id) => {
    const source = desktopById.get(id);
    const h = source?.kind === "spacer"
      ? Math.max(1, Math.min(source.h, 4))
      : Math.max(7, Math.min(source?.h ?? 10, 16));
    const item: DashboardLayoutItem = {
      id,
      kind: source?.kind ?? "widget",
      x: 0,
      y,
      w: 1,
      h,
      minW: 1,
      minH: source?.kind === "spacer" ? 1 : Math.min(source?.minH ?? 7, h),
      maxW: 1,
    };
    y += h;
    return item;
  });
};

const activeItems = (
  layout: DashboardLayoutState,
  mode: DashboardLayoutMode,
): DashboardLayoutItem[] => {
  if (mode === "mobile") return mobileItems(layout);
  return layout[mode];
};

const itemAttributes = (item: DashboardLayoutItem) => ({
  "gs-id": item.id,
  "gs-x": item.x,
  "gs-y": item.y,
  "gs-w": item.w,
  "gs-h": item.h,
  "gs-min-w": item.minW,
  "gs-min-h": item.minH,
  ...(item.maxW ? { "gs-max-w": item.maxW } : {}),
  ...(item.maxH ? { "gs-max-h": item.maxH } : {}),
});

export function DashboardGrid({
  widgets,
  canEditLayout = true,
}: {
  widgets: Record<DashboardWidgetId, ReactNode>;
  canEditLayout?: boolean;
}) {
  const [layout, setLayout] = useState(() =>
    loadDashboardLayout(window.localStorage));
  const [mode, setMode] = useState(() => dashboardModeForWidth(window.innerWidth));
  const [editingRequested, setEditing] = useState(false);
  const editing = canEditLayout && editingRequested;
  const [interactingId, setInteractingId] = useState<DashboardItemId | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const gridElement = useRef<HTMLDivElement>(null);
  const gridRef = useRef<GridStack | null>(null);
  const initialSizingDone = useRef(new Set<DashboardLayoutMode>());
  const layoutRef = useRef(layout);
  const modeRef = useRef(mode);
  const editingRef = useRef(editing);

  layoutRef.current = layout;
  modeRef.current = mode;
  editingRef.current = editing;

  const hiddenWidgets = Object.entries(widgets).filter(([, content]) => content == null).map(([id]) => id).join(",");
  const items = useMemo(() => activeItems(layout, mode).filter(
    (item) => !hiddenWidgets.split(",").includes(item.id),
  ), [layout, mode, hiddenWidgets]);
  const orderedIds = useMemo(
    () => orderedLayoutItems(items).map(({ id }) => id),
    [items],
  );

  const commitLayout = useCallback((next: DashboardLayoutState) => {
    layoutRef.current = next;
    setLayout(next);
    saveDashboardLayout(window.localStorage, next);
  }, []);

  const readGridItems = useCallback((): DashboardLayoutItem[] => {
    const grid = gridRef.current;
    if (!grid) return activeItems(layoutRef.current, modeRef.current);
    const current = new Map(
      activeItems(layoutRef.current, modeRef.current).map((item) => [item.id, item]),
    );
    return grid.getGridItems().flatMap((element) => {
      const id = element.getAttribute("gs-id") as DashboardItemId | null;
      const node = (element as GridItemHTMLElement).gridstackNode;
      const source = id ? current.get(id) : null;
      if (!id || !node || !source) return [];
      return [{
        ...source,
        x: node.x ?? source.x,
        y: node.y ?? source.y,
        w: node.w ?? source.w,
        h: node.h ?? source.h,
      }];
    });
  }, []);

  const saveGridResult = useCallback(() => {
    const currentMode = modeRef.current;
    if (currentMode === "mobile") return;
    const nextItems = readGridItems();
    const current = layoutRef.current;
    commitLayout({ ...current, [currentMode]: nextItems });
  }, [commitLayout, readGridItems]);

  const compactGrid = useCallback((announce = true) => {
    const grid = gridRef.current;
    if (!grid || modeRef.current === "mobile") return;
    grid.compact("compact");
    saveGridResult();
    if (announce) {
      setAnnouncement("Dashboard layout compacted");
    }
  }, [saveGridResult]);

  useEffect(() => {
    const handleResize = () => setMode(dashboardModeForWidth(window.innerWidth));
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  useLayoutEffect(() => {
    if (!gridElement.current || gridRef.current) return;
    const grid = GridStack.init({
      animate: true,
      cellHeight: CELL_HEIGHT,
      column: DASHBOARD_COLUMNS[modeRef.current],
      disableDrag: true,
      disableResize: true,
      draggable: { handle: ".dashboard-widget-handle" },
      float: false,
      margin: 10,
      resizable: { handles: "se" },
    }, gridElement.current);
    if (!grid) return;
    const initializedGrid = grid;
    gridRef.current = initializedGrid;

    const start = (_event: Event, element: GridItemHTMLElement) => {
      setInteractingId(element.getAttribute("gs-id") as DashboardItemId | null);
    };
    const stop = () => {
      setInteractingId(null);
      initializedGrid.compact("compact");
      window.requestAnimationFrame(saveGridResult);
    };
    initializedGrid.on("dragstart", start);
    initializedGrid.on("resizestart", start);
    initializedGrid.on("dragstop", stop);
    initializedGrid.on("resizestop", stop);

    return () => {
      initializedGrid.offAll();
      initializedGrid.destroy(false);
      gridRef.current = null;
    };
  }, [saveGridResult]);

  useLayoutEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const desiredIds = new Set(items.map(({ id }) => id));
    grid.batchUpdate();
    grid.column(DASHBOARD_COLUMNS[mode], "none");
    for (const element of grid.getGridItems()) {
      if (!desiredIds.has(element.getAttribute("gs-id") as DashboardItemId)) {
        grid.removeWidget(element, false);
      }
    }
    for (const item of items) {
      const element = gridElement.current?.querySelector<GridItemHTMLElement>(
        `[gs-id="${CSS.escape(item.id)}"]`,
      );
      if (!element) continue;
      if (!element.gridstackNode) grid.makeWidget(element);
      grid.update(element, item);
    }
    grid.batchUpdate(false);
  }, [items, mode]);

  const fitLoadedWidgetsToContent = useCallback((targetMode: DashboardLayoutMode) => {
    const activeGrid = gridRef.current;
    if (!activeGrid) return;
    initialSizingDone.current.add(targetMode);
    activeGrid.batchUpdate();
    for (const element of activeGrid.getGridItems()) {
      const id = element.getAttribute("gs-id") as DashboardItemId | null;
      if (!id || id === "events" || id.startsWith("spacer-")) continue;
      const target = element.querySelector<HTMLElement>(".widget-autosize-target");
      const node = element.gridstackNode;
      if (!target || !node) continue;

      activeGrid.update(element, { h: contentRows(target, node) });
    }
    activeGrid.batchUpdate(false);
    activeGrid.compact("compact");
    if (targetMode !== "mobile") saveGridResult();
  }, [saveGridResult]);

  useEffect(() => {
    const grid = gridRef.current;
    if (!grid || initialSizingDone.current.has(mode)) return;
    let cancelled = false;

    const sizeLoadedWidgets = () => {
      if (cancelled || !gridRef.current || initialSizingDone.current.has(mode)) return;
      fitLoadedWidgetsToContent(mode);
    };

    const timer = window.setTimeout(() => {
      const fontsReady = document.fonts?.ready ?? Promise.resolve();
      void fontsReady.then(sizeLoadedWidgets);
    }, 360);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [items, mode, fitLoadedWidgetsToContent]);

  useEffect(() => {
    const grid = gridRef.current;
    if (
      !grid || typeof ResizeObserver === "undefined"
      || typeof MutationObserver === "undefined"
    ) return;

    const timers = new Map<HTMLElement, number>();
    const widths = new Map<HTMLElement, number>();
    const observed = grid.getGridItems().flatMap((element) => {
      const id = element.getAttribute("gs-id") as DashboardItemId | null;
      const target = element.querySelector<HTMLElement>(".widget-autosize-target");
      if (!id || id === "events" || id.startsWith("spacer-") || !target) return [];
      widths.set(target, target.getBoundingClientRect().width);
      return [{ element, target }];
    });

    const fit = (element: GridItemHTMLElement, target: HTMLElement) => {
      timers.delete(target);
      const activeGrid = gridRef.current;
      const node = element.gridstackNode;
      if (!activeGrid || !node || interactingId === element.getAttribute("gs-id")) return;
      const rows = contentRows(target, node);
      if (rows === node.h) return;
      activeGrid.update(element, { h: rows });
      activeGrid.compact("compact");
      if (modeRef.current !== "mobile") saveGridResult();
    };
    const scheduleFit = (
      element: GridItemHTMLElement,
      target: HTMLElement,
      delay = 80,
    ) => {
      const timer = timers.get(target);
      if (timer !== undefined) window.clearTimeout(timer);
      timers.set(target, window.setTimeout(() => fit(element, target), delay));
    };

    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const target = entry.target as HTMLElement;
        const observedWidget = observed.find((item) => item.target === target);
        if (!observedWidget) continue;
        const nextWidth = entry.contentRect.width;
        const previousWidth = widths.get(target) ?? nextWidth;
        if (Math.abs(nextWidth - previousWidth) < 1) continue;
        widths.set(target, nextWidth);
        scheduleFit(observedWidget.element, target, 180);
      }
    });
    const mutationObserver = new MutationObserver((mutations) => {
      const changedTargets = new Set(
        mutations.flatMap((mutation) => observed
          .filter(({ target }) => target.contains(mutation.target))
          .map(({ target }) => target)),
      );
      for (const target of changedTargets) {
        const observedWidget = observed.find((item) => item.target === target);
        if (observedWidget) scheduleFit(observedWidget.element, target);
      }
    });
    for (const { target } of observed) resizeObserver.observe(target);
    mutationObserver.observe(gridElement.current!, {
      characterData: true,
      childList: true,
      subtree: true,
    });

    return () => {
      for (const timer of timers.values()) window.clearTimeout(timer);
      resizeObserver.disconnect();
      mutationObserver.disconnect();
    };
  }, [interactingId, mode, saveGridResult]);

  useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const enabled = editing && mode !== "mobile";
    grid.enableMove(enabled);
    grid.enableResize(enabled);
  }, [editing, mode]);

  const moveItem = useCallback((id: DashboardItemId, offset: -1 | 1) => {
    const current = layoutRef.current;
    const currentMode = modeRef.current;
    if (currentMode === "mobile") {
      const order = [...current.mobileOrder];
      const index = order.indexOf(id);
      const target = index + offset;
      if (index < 0 || target < 0 || target >= order.length) return;
      [order[index], order[target]] = [order[target]!, order[index]!];
      commitLayout({ ...current, mobileOrder: order });
    } else {
      commitLayout({
        ...current,
        [currentMode]: moveDashboardItem(current[currentMode], id, offset),
      });
      window.setTimeout(() => compactGrid(false), 0);
    }
    setAnnouncement(`${id.startsWith("spacer-") ? "Spacer" : DASHBOARD_WIDGETS[id as DashboardWidgetId].title} moved ${offset < 0 ? "earlier" : "later"}`);
  }, [commitLayout, compactGrid]);

  const addSpacer = useCallback(() => {
    const current = layoutRef.current;
    const spacer = createSpacer([
      ...current.desktop.map(({ id }) => id),
      ...current.tablet.map(({ id }) => id),
    ]);
    const desktop = { ...spacer, y: bottom(current.desktop) };
    const tablet = {
      ...spacer,
      y: bottom(current.tablet),
      w: Math.min(spacer.w, DASHBOARD_COLUMNS.tablet),
    };
    commitLayout({
      ...current,
      desktop: [...current.desktop, desktop],
      tablet: [...current.tablet, tablet],
      mobileOrder: [...current.mobileOrder, spacer.id],
    });
    setAnnouncement("Dashboard spacer added");
  }, [commitLayout]);

  const removeSpacer = useCallback((id: DashboardSpacerId) => {
    const element = gridElement.current?.querySelector<GridItemHTMLElement>(
      `[gs-id="${CSS.escape(id)}"]`,
    );
    if (element && gridRef.current) gridRef.current.removeWidget(element, false);
    const current = layoutRef.current;
    commitLayout({
      ...current,
      desktop: current.desktop.filter((item) => item.id !== id),
      tablet: current.tablet.filter((item) => item.id !== id),
      mobileOrder: current.mobileOrder.filter((itemId) => itemId !== id),
    });
    setAnnouncement("Dashboard spacer removed");
  }, [commitLayout]);

  const resetLayout = useCallback(() => {
    commitLayout(createDefaultDashboardLayout());
    initialSizingDone.current.delete(modeRef.current);
    setAnnouncement("Dashboard layout reset");
    window.requestAnimationFrame(() => {
      const fontsReady = document.fonts?.ready ?? Promise.resolve();
      void fontsReady.then(() => fitLoadedWidgetsToContent(modeRef.current));
    });
  }, [commitLayout, fitLoadedWidgetsToContent]);

  return (
    <section aria-label="Customizable dashboard" className="mt-5">
      <p aria-live="polite" className="sr-only">{announcement}</p>
      <div
        className={`grid-stack stagepilot-dashboard-grid ${editing ? "dashboard-layout-editing" : ""}`}
        data-layout-mode={mode}
        ref={gridElement}
      >
        {items.map((item) => {
          const index = orderedIds.indexOf(item.id);
          const label = item.kind === "widget"
            ? DASHBOARD_WIDGETS[item.id as DashboardWidgetId].title
            : "Dashboard spacer";
          return (
            <div
              className={`grid-stack-item ${interactingId === item.id ? "dashboard-grid-item-interacting" : ""}`}
              data-testid={`dashboard-widget-${item.id}`}
              key={item.id}
              {...itemAttributes(item)}
            >
              <div className="grid-stack-item-content">
                {item.kind === "spacer" ? (
                  <DashboardWidgetFrame
                    editing={editing}
                    first={index === 0}
                    id={item.id}
                    label={label}
                    last={index === orderedIds.length - 1}
                    onMove={moveItem}
                  >
                    <DashboardSpacer
                      editing={editing}
                      id={item.id as DashboardSpacerId}
                      onRemove={removeSpacer}
                    />
                  </DashboardWidgetFrame>
                ) : (
                  <DashboardWidgetFrame
                    editing={editing}
                    first={index === 0}
                    id={item.id}
                    label={label}
                    last={index === orderedIds.length - 1}
                    onMove={moveItem}
                  >
                    {widgets[item.id as DashboardWidgetId]}
                  </DashboardWidgetFrame>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {canEditLayout && <DashboardLayoutToolbar
        editing={editing}
        onAddSpacer={addSpacer}
        onCompact={() => compactGrid(true)}
        onDone={() => setEditing(false)}
        onEdit={() => setEditing(true)}
        onReset={resetLayout}
      />}
    </section>
  );
}
