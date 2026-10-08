# Playback song times / local screenshot fixtures

16 PNGs in `shots/playback-planning-center-lengths/`: desktop1280 and phone390
for unavailable, dropdown, preview, success, denied, restore, indeterminate scan
progress and determinate scan progress. `capture-verification.json` records each
scenario, visible card text and a successful horizontal-overflow check.

These are the real React components with fake data. The fixture replaces fetch;
the Playwright capture runner independently blocks every non-loopback request.
No Planning Center or Playback service is connected.

To reproduce in this workspace: copy the two `fixtures/.planning-preview.*` files
into `repo/frontend/`, run Vite on loopback port 5199, and run `node capture.cjs` from the task workspace. It reuses the existing parent task's Playwright installation and Chromium.
Remove the temporary frontend fixture entry points afterwards. Publish this folder
on the screenshot branch, not in the feature PR. Captured from feature commit 5fec651; published on playback-ux-screenshots.
