import { useState } from "react";
import { createRoot } from "react-dom/client";
import "./src/index.css";
import { PlaybackApiPanel } from "./src/components/PlaybackApiPanel";
import { playbackStatus } from "./src/test/playbackFixtures";
import type { PlaybackStatusResponse } from "./src/types";
const scenario = new URLSearchParams(location.search).get("scenario") ?? "unavailable";
const preview = { token: "local-only", category: "Sunday Service", plan_title: "Sunday Worship", plan_date: "2026-10-11", message: "", items: [{ item_id: "1", title: "Worship", old_length: 245, new_length: 268, status: "updated", reason: null }, { item_id: "2", title: "To Worship You I Live", old_length: 315, new_length: 261, status: "updated", reason: null }, { item_id: "3", title: "Already The Same", old_length: 282, new_length: 282, status: "skipped", reason: "Already the same." }] };
const denied = "Planning Center did not allow this change. The account or token used by StagePilot can't edit this plan.";
const success = "Updated 2 songs in Planning Center: Worship 4:05 -> 4:28, To Worship You I Live 5:15 -> 4:21. Left 1 songs as they were (already the same). StagePilot reloaded the plan and verified the song times.";
const status = playbackStatus({ planning_center_connected: true, discovery: "done", captured_at: "2026-10-07T12:00:00Z", planning_center_update_service_type_id: "sunday", song_order: [101, 202, 303], song_lengths: [267.6, 260.2, 282.16], plan_songs: [{ title: "Worship", duration_seconds: 245 }, { title: "To Worship You I Live", duration_seconds: 315 }, { title: "Already The Same", duration_seconds: 282 }], ...(scenario === "unavailable" ? { captured_at: null, song_order: [], song_lengths: [] } : {}), ...(scenario.startsWith("progress") ? { discovery: "running", discovery_song: 2, discovery_total: scenario === "progress-determinate" ? 5 : 0 } : {}) });
// These fixture requests NEVER reach any service. The Playwright runner also
// rejects every non-loopback request as a second independent safety boundary.
window.fetch = async (input, options) => {
 const path = String(input);
 let data: unknown = path.endsWith("/categories") ? [{ id: "sunday", name: "Sunday Service" }, { id: "youth", name: "Youth Night" }, { id: "midweek", name: "Midweek Service" }] : path.endsWith("/preview") ? preview : { ...status, planning_center_undo_available: scenario !== "denied", planning_center_lengths: { status: scenario === "denied" ? "failed" : "done", message: scenario === "denied" ? denied : success, items: [], reload: scenario === "denied" ? "not_needed" : "verified" } };
 if (path.endsWith("/category")) data = { ...status, planning_center_update_service_type_id: JSON.parse(String(options?.body)).service_type_id };
 return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
};
function Fixture() {
 const [current, setCurrent] = useState<PlaybackStatusResponse>(status);
 return <main className="mx-auto max-w-4xl p-4"><h1 className="text-xl font-bold text-white">StagePilot · Playback</h1><p className="text-xs text-slate-400">Local fake data — no service connections</p><PlaybackApiPanel playback={{ status: current, events: [], error: null, message: null, pending: null, save: async () => true, selectSource: () => {}, scan: () => {}, cancelScan: () => {}, discover: () => {}, acceptStatus: setCurrent }} /></main>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
