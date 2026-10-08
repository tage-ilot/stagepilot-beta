import { afterEach, describe, expect, it, vi } from "vitest";

import { apiOrigin, discoverPlaybackSongOrder, findPlayback, getPlaybackEvents, getPlaybackStatus, updatePlaybackSettings } from "./api";
import { setApiAccess } from "./access/accessState";
import { playbackStatus } from "./test/playbackFixtures";

afterEach(() => vi.unstubAllGlobals());

describe("Playback typed HTTP contract", () => {
  it("uses existing authenticated request boundary and exact receive/discovery routes", async () => {
    setApiAccess(null);
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => playbackStatus() });
    vi.stubGlobal("fetch", fetch);
    await getPlaybackStatus();
    await getPlaybackEvents();
    await findPlayback();
    await updatePlaybackSettings({ enabled: true, host: "192.0.2.10", port: 8080, auto_scan: true, fast_transport: true });
    await discoverPlaybackSongOrder();
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "status", "events", "find", "settings", "discover-song-order",
    ].map((path) => `${apiOrigin}/api/v1/playback-api/${path}`));
    expect(fetch.mock.calls.every(([, options]) => options.credentials === "include")).toBe(true);
    expect(fetch.mock.calls[2]?.[1]).toMatchObject({ method: "POST" });
    expect(fetch.mock.calls[3]?.[1]).toMatchObject({ method: "PUT", body: JSON.stringify({ enabled: true, host: "192.0.2.10", port: 8080, auto_scan: true, fast_transport: true }) });
    expect(fetch.mock.calls[4]?.[1]).toMatchObject({ method: "POST", body: '{"confirm":true}' });
  });
});
