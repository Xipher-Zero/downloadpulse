const { test, expect } = require('@playwright/test');

/* 1.0.5 WS8b1: the desktop window's state (ui-window-state.js DPWindowState).
 * A desktop window hidden in the tray may be released only when its own page
 * says nothing would be lost; the relaunched page comes back to the state it
 * gave. The server's half (the prompt, the window-only routes, the one-time
 * restore) is proven by backend/tests/test_v105_window_release.py; these cases
 * answer the server facts themselves -- a labelled mock of whether this is the
 * desktop application (the first-paint script's window.downloadPulseDesktop),
 * of the restore the relaunched page is handed, and the answer it sends -- so
 * the page's own behaviour is what they prove. */

async function asDesktop(page, {restore = null, holdMs = 0} = {}) {
  const seen = {gets: 0, puts: []};
  await page.route(url => url.pathname === '/ui-theme-bootstrap.js', async route => {
    const response = await route.fetch();
    route.fulfill({response, body: (await response.text()) + '\nwindow.downloadPulseDesktop = true;\n'});
  });
  // This scratch server has no aria2: its start-up check (a POST) would stay in
  // flight here, which is a fact of the environment, not of the page.
  await page.route(url => url.pathname === '/api/settings/test-aria2',
    route => route.fulfill({status: 200, contentType: 'application/json', body: '{"ok":true}'}));
  await page.route(url => url.pathname === '/api/desktop/window-state', async route => {
    if (route.request().method() === 'GET') {
      seen.gets += 1;
      if (holdMs) await new Promise(resolve => setTimeout(resolve, holdMs));
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({restore})});
    }
    seen.puts.push(route.request().postDataJSON());
    return route.fulfill({status: 200, contentType: 'application/json', body: '{"recorded":true}'});
  });
  return seen;
}

async function load(page, path = '/', {height = 900} = {}) {
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.setViewportSize({width: 1440, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto(path);
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

const releasable = page => page.evaluate(() => window.DPWindowState.releasable());

test('W01: anywhere but the desktop window it asks nothing and answers nothing', async ({page}) => {
  const asked = [];
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/desktop/window-state') asked.push(request.method()); });
  await load(page);
  expect(await page.evaluate(() => window.DPWindowState.active)).toBe(false);
  await page.evaluate(() => window.DPWindowState.answer('n'));
  expect(asked).toEqual([]);
});

test('W02: an idle window is releasable and answers with the page it shows', async ({page}) => {
  const seen = await asDesktop(page);
  await load(page, '/statistics');
  await expect(page.locator('#view-stats')).toHaveClass(/\bactive\b/);
  expect(seen.gets).toBe(1);
  expect(await releasable(page)).toBe(true);
  await page.evaluate(() => window.DPWindowState.answer('nonce-1'));
  await expect.poll(() => seen.puts.length).toBe(1);
  const [answer] = seen.puts;
  expect(answer.nonce).toBe('nonce-1');
  expect(answer.releasable).toBe(true);
  expect(answer.state.route).toBe('/statistics');
  expect(answer.state.owners.navigation).toEqual({content: expect.any(Number)});
  // Answered again with the same prompt: the latest answer is the one sent last.
  await page.evaluate(() => window.DPWindowState.answer('nonce-1'));
  await expect.poll(() => seen.puts.length).toBe(2);
});

test('W03: anything unfinished or open that a relaunch would lose keeps the window', async ({page}) => {
  const seen = await asDesktop(page);
  await load(page);
  expect(await releasable(page)).toBe(true);
  // A change sent and not yet answered.
  let release;
  await page.route(url => url.pathname === '/api/desktop/frame', route => new Promise(resolve => { release = () => resolve(route.fulfill({status: 200, body: '{}'})); }));
  await page.evaluate(() => { window.__held = window.downloadPulseAuth.fetch('/api/desktop/frame', {method: 'PUT', body: '{}'}); });
  await expect.poll(() => releasable(page)).toBe(false);
  release();
  await page.evaluate(() => window.__held);
  await expect.poll(() => releasable(page)).toBe(true);
  // An open popover.
  await page.evaluate(() => document.getElementById('stats-period-menu').showPopover());
  expect(await releasable(page)).toBe(false);
  await page.evaluate(() => document.getElementById('stats-period-menu').hidePopover());
  expect(await releasable(page)).toBe(true);
  // An open dialog.
  await page.evaluate(() => window.DPSettingsHelp.open(window.DPSettingsHelp.topics[0]));
  await expect.poll(() => releasable(page)).toBe(false);
  await page.keyboard.press('Escape');
  await expect.poll(() => releasable(page)).toBe(true);
  // An owner that says it has something unfinished.
  await page.evaluate(() => window.DPWindowState.register('spec-owner', {releasable: () => false}));
  expect(await releasable(page)).toBe(false);
  // Not releasable: the answer says so and gives no state.
  await page.evaluate(() => window.DPWindowState.answer('nonce-2'));
  await expect.poll(() => seen.puts.length).toBe(1);
  expect(seen.puts[0]).toEqual({nonce: 'nonce-2', releasable: false, state: {}});
});

test('W04: a released window comes back on the page it showed, its parts handed once', async ({page}) => {
  await asDesktop(page, {restore: {state: {route: '/activity', owners: {navigation: {content: 0}, spec: {x: 1}}}, since: 42}});
  await load(page, '/');
  await expect(page.locator('#view-events')).toHaveClass(/\bactive\b/);
  expect(new URL(page.url()).pathname).toBe('/activity');
  expect(await page.evaluate(() => [window.DPWindowState.since(), window.DPWindowState.take('spec'),
    window.DPWindowState.take('spec'), window.DPWindowState.take('absent')])).toEqual([42, {x: 1}, null, null]);
});

test('W05: an ordinary reload is never a restore', async ({page}) => {
  const seen = await asDesktop(page, {restore: null});
  await load(page, '/');
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  expect(seen.gets).toBe(1);
  expect(await page.evaluate(() => [window.DPWindowState.route(), window.DPWindowState.since(),
    window.DPWindowState.take('navigation')])).toEqual([null, null, null]);
});

test('W06: each owner that holds what a relaunch would lose keeps the window', async ({page}) => {
  await asDesktop(page);
  await load(page);
  expect(await releasable(page)).toBe(true);
  // The #modal shell: Details is brought back, file selection is not.
  await page.evaluate(() => window.DPModal.open({mode: 'details', title: 'x', closeLabel: 'Close', pending: true}));
  expect(await releasable(page)).toBe(true);
  await page.evaluate(() => window.DPModal.requestModalClose());
  await expect.poll(() => page.evaluate(() => window.DPModal.mode)).toBe(null);
  await page.evaluate(() => window.DPModal.open({mode: 'file-selection', title: 'x', closeLabel: 'Close', pending: true}));
  expect(await releasable(page)).toBe(false);
  await page.evaluate(() => window.DPModal.requestModalClose());
  await expect.poll(() => releasable(page)).toBe(true);
  // A sheet, through its one owner (the theme picker).
  await page.locator('#theme-picker-toggle').click();
  await expect(page.locator('#theme-picker')).toBeVisible();
  expect(await releasable(page)).toBe(false);
  await page.evaluate(() => window.DPShellSheet.close(document.getElementById('theme-picker')));
  await expect.poll(() => releasable(page)).toBe(true);
  // The context menu, through its own owner.
  await page.evaluate(() => dpOpenContextMenu(new MouseEvent('contextmenu', {clientX: 200, clientY: 200}),
    document.getElementById('page-title'), {label: 'x', items: [{label: 'y', run: () => {}}]}));
  expect(await releasable(page)).toBe(false);
  await page.evaluate(() => dpCloseContextMenu(false));
  expect(await releasable(page)).toBe(true);
  // A popover the state brings back is not in the way.
  await page.evaluate(() => { const menu = document.getElementById('stats-period-menu'); menu.setAttribute('data-dp-window-state-restorable', ''); menu.showPopover(); });
  expect(await releasable(page)).toBe(true);
  await page.evaluate(() => { const menu = document.getElementById('stats-period-menu'); menu.hidePopover(); menu.removeAttribute('data-dp-window-state-restorable'); });
  // An owner that cannot say is not releasable.
  await page.evaluate(() => window.DPWindowState.register('spec-throws', {releasable: () => { throw new Error('x'); }}));
  expect(await releasable(page)).toBe(false);
});

test('W07: a legal document open in Help keeps the window', async ({page}) => {
  await asDesktop(page);
  await load(page, '/help');
  await expect(page.locator('#view-help')).toHaveClass(/\bactive\b/);
  const opener = page.locator('[data-legal-document="gpl"]');
  await page.evaluate(() => window.DPHelpPage?.activateTab?.('license', false));
  await expect(opener).toBeVisible();
  expect(await releasable(page)).toBe(true);
  await opener.click();
  await expect(page.locator('body')).toHaveClass(/dp-help-legal-modal-open/);
  expect(await releasable(page)).toBe(false);
});

test('W08: an owner whose part cannot be captured is never released with it missing', async ({page}) => {
  const seen = await asDesktop(page);
  await load(page);
  await page.evaluate(() => window.DPWindowState.register('spec-capture', {capture: () => { throw new Error('x'); }}));
  expect(await releasable(page)).toBe(true);
  await page.evaluate(() => window.DPWindowState.answer('nonce-3'));
  await expect.poll(() => seen.puts.length).toBe(1);
  expect(seen.puts[0]).toEqual({nonce: 'nonce-3', releasable: false, state: {}});
});

test('W09: the restored page and its scroll are in place before the interface says it is ready, however late the state arrives', async ({page}) => {
  const target = 50;
  await asDesktop(page, {restore: {state: {route: '/', owners: {navigation: {content: target}}}, since: 1}, holdMs: 2500});
  await page.addInitScript(() => {
    window.__readyAt = null;
    document.addEventListener('downloadpulse:interface-ready', () => {
      window.__readyAt = {path: location.pathname, shown: document.querySelector('#sidebar .nav-item.active')?.dataset.view};
    }, {once: true});
  });
  await load(page, '/settings', {height: 520});
  await expect.poll(() => page.evaluate(() => window.__readyAt)).toEqual({path: '/', shown: 'dashboard'});
  await expect.poll(() => page.evaluate(() => document.getElementById('content').scrollTop)).toBe(target);
});

test('W10: a restored scroll the page cannot hold yet never lands on a page the person went to', async ({page}) => {
  await asDesktop(page, {restore: {state: {route: '/help', owners: {navigation: {content: 300}}}, since: 1}});
  await load(page, '/', {height: 520});
  await expect(page.locator('#view-help')).toHaveClass(/\bactive\b/);
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  // Long enough for the Dashboard to draw and for the restore's own bound to pass.
  await page.waitForTimeout(5500);
  expect(await page.evaluate(() => document.getElementById('content').scrollTop)).toBe(0);
});
