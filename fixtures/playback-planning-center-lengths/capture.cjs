const { chromium } = require('/home/agenthub/.hermes/kanban/boards/stagepilot/workspaces/t_6d921029/screenshot-tools/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: '/home/agenthub/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell', args: ['--no-sandbox'] });
  const dir = path.join(__dirname, '..', '..', 'shots', 'playback-planning-center-lengths');
  fs.mkdirSync(dir, { recursive: true });
  const results = [];
  for (const width of [1280, 390]) for (const scenario of ['unavailable', 'dropdown', 'preview', 'success', 'denied', 'restore', 'progress-indeterminate', 'progress-determinate']) {
    const page = await browser.newPage({ viewport: { width, height: 1100 } });
    await page.route('**/*', route => {
      const u = new URL(route.request().url());
      return u.hostname === '127.0.0.1' && u.port === '5199' ? route.continue() : route.abort('blockedbyclient');
    });
    await page.goto(`http://127.0.0.1:5199/.planning-preview.html?scenario=${scenario}`);
    const card = page.getByRole('region', { name: 'Song order' });
    await card.waitFor();
    if (scenario === 'dropdown') {
      await page.getByRole('button', { name: 'Choose plan category' }).click();
      await page.getByRole('menu', { name: 'Plan categories' }).waitFor();
    }
    if (['preview', 'success', 'denied', 'restore'].includes(scenario)) {
      await page.getByRole('button', { name: 'Update Planning Center', exact: true }).click();
      const dialog = page.getByRole('dialog');
      await dialog.waitFor();
      if (scenario !== 'preview') {
        await dialog.getByRole('button', { name: 'Update Planning Center', exact: true }).click();
        await dialog.waitFor({ state: 'hidden' });
        await page.getByText(scenario === 'denied' ? /Planning Center did not allow this change/ : /Updated 2 songs in Planning Center/).waitFor();
        if (scenario === 'restore') await page.getByRole('button', { name: 'Restore Planning Center times' }).waitFor();
      }
    }
    if (scenario.startsWith('progress')) {
      await page.getByRole('progressbar').waitFor();
      await page.waitForTimeout(1700);
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    if (overflow) throw new Error('Horizontal overflow at ' + width + ' / ' + scenario);
    const filename = `${width === 1280 ? 'desktop1280' : 'phone390'}-${scenario}.png`;
    if (['preview', 'dropdown'].includes(scenario)) await page.screenshot({ path: path.join(dir, filename), fullPage: true });
    else await card.screenshot({ path: path.join(dir, filename) });
    results.push({ width, scenario, overflow, filename, text: await card.innerText() });
    await page.close();
  }
  await browser.close();
  fs.writeFileSync(path.join(dir, 'capture-verification.json'), JSON.stringify(results, null, 2));
  if (results.length !== 16) throw new Error('Expected 16 screenshots');
  console.log(JSON.stringify({ screenshots: results.length, dir, horizontalOverflow: false }));
})();
