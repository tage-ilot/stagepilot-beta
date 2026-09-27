import {
  createDefaultDashboardLayout,
  migrateDashboardOrder,
  parseDashboardLayout,
} from "./dashboardLayout";
import type { DashboardLayoutState } from "./dashboardLayoutTypes";

export const DASHBOARD_LAYOUT_KEY = "stagepilot.dashboard-layout.v2";
export const DASHBOARD_ORDER_V1_KEY = "stagepilot.dashboard-widget-order.v1";
export const DASHBOARD_LAYOUT_INVALID_KEY = "stagepilot.dashboard-layout.invalid";

export type DashboardLayoutStorage = Pick<Storage, "getItem" | "setItem">;
export type DashboardLayoutLoadSource = "saved" | "migrated" | "default";
export type DashboardLayoutLoadResult = {
  layout: DashboardLayoutState;
  source: DashboardLayoutLoadSource;
};

export const saveDashboardLayout = (
  storage: DashboardLayoutStorage,
  layout: DashboardLayoutState,
) => {
  try {
    storage.setItem(DASHBOARD_LAYOUT_KEY, JSON.stringify(layout));
    return true;
  } catch {
    return false;
  }
};

export const loadDashboardLayoutResult = (
  storage: DashboardLayoutStorage,
): DashboardLayoutLoadResult => {
  let savedValue: string | null = null;
  try {
    savedValue = storage.getItem(DASHBOARD_LAYOUT_KEY);
    if (savedValue) {
      const parsed = parseDashboardLayout(JSON.parse(savedValue));
      if (parsed) return { layout: parsed, source: "saved" };
      try {
        storage.setItem(DASHBOARD_LAYOUT_INVALID_KEY, savedValue);
      } catch {
        // Diagnostics are best effort; fallback must still work.
      }
    }
  } catch {
    if (savedValue) {
      try {
        storage.setItem(DASHBOARD_LAYOUT_INVALID_KEY, savedValue);
      } catch {
        // Diagnostics are best effort; fallback must still work.
      }
    }
    // Corrupt JSON or unavailable storage falls through to migration/defaults.
  }

  try {
    const oldValue = storage.getItem(DASHBOARD_ORDER_V1_KEY);
    const migrated = oldValue ? migrateDashboardOrder(JSON.parse(oldValue)) : null;
    if (migrated) {
      saveDashboardLayout(storage, migrated);
      return { layout: migrated, source: "migrated" };
    }
  } catch {
    // Invalid v1 data is intentionally retained but ignored.
  }

  const fallback = createDefaultDashboardLayout();
  saveDashboardLayout(storage, fallback);
  return { layout: fallback, source: "default" };
};

export const loadDashboardLayout = (
  storage: DashboardLayoutStorage,
): DashboardLayoutState => loadDashboardLayoutResult(storage).layout;
