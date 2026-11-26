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
  // Once start-up's own requests are answered, nothing keeps an idle window.
  await expect.poll(() => releasable(page)).toBe(true);
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
  await expect.poll(() => releasable(page)).toBe(true);
  // A change sent and not yet answered.
  let release;
  await page.route(url => url.pathname === '/api/desktop/frame', route => new Promise(resolve => { release = () => resolve(route.fulfill({status: 200, body: '{}'})); }));
  await page.evaluate(() => { window.__held = window.downloadPulseAuth.fetch('/api/desktop/frame', {method: 'PUT', body: '{}'}); });
  await expect.poll(() => releasable(page)).toBe(false);
  await expect.poll(() => typeof release).toBe('function');
  release();
  await page.evaluate(() => window.__held);
  await expect.poll(() => releasable(page)).toBe(true);
  // An open popover.
  await page.evaluate(() => document.getElementById('stats-period-menu').showPopover());
  expect(await releasable(page)).toBe(false);
  await page.evaluate(() => document.getElementById('stats-period-menu').hidePopover());
  await expect.poll(() => releasable(page)).toBe(true);
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
  await expect.poll(() => releasable(page)).toBe(true);
  // The #modal shell: Details is brought back, file selection is not.
  await page.evaluate(() => window.DPModal.open({mode: 'details', title: 'x', closeLabel: 'Close', pending: true}));
  await expect.poll(() => releasable(page)).toBe(true);
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
  await expect.poll(() => releasable(page)).toBe(true);
  // A popover the state brings back is not in the way.
  await page.evaluate(() => { const menu = document.getElementById('stats-period-menu'); menu.setAttribute('data-dp-window-state-restorable', ''); menu.showPopover(); });
  await expect.poll(() => releasable(page)).toBe(true);
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
  await expect.poll(() => releasable(page)).toBe(true);
  await opener.click();
  await expect(page.locator('body')).toHaveClass(/dp-help-legal-modal-open/);
  expect(await releasable(page)).toBe(false);
});

test('W08: an owner whose part cannot be captured is never released with it missing', async ({page}) => {
  const seen = await asDesktop(page);
  await load(page);
  await page.evaluate(() => window.DPWindowState.register('spec-capture', {capture: () => { throw new Error('x'); }}));
  await expect.poll(() => releasable(page)).toBe(true);
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

// ── WS8b2: Downloads and Quick Add ──────────────────────────────────────
// Needs transfers on the server (the scratch server is seeded with 41).
// What the person sees on Downloads (its owner keeps its state private).
const downloads = page => page.evaluate(() => ({
  page: Number(document.querySelector('#torrent-page-btns [aria-current="page"]')?.textContent || 1),
  order: document.getElementById('torrent-order').value,
  filter: document.querySelector('#view-torrents .ftab.active')?.dataset.dpStatus ?? null,
  checked: [...document.querySelectorAll('.t-chk:checked')].map(box => Number(box.dataset.id)).sort((a, b) => a - b),
  search: document.getElementById('torrent-search').value}));
const rowIds = page => page.evaluate(() => [...document.querySelectorAll('#t-tbody tr[data-torrent-id]')].map(row => Number(row.dataset.torrentId)));

test('D01: Downloads comes back at the row it showed, in its order, with its selection reconciled', async ({page, request}) => {
  // Independent of the page: the newest-first list as the API orders it.
  const all = (await (await request.get('/api/torrents?order=newest&limit=100')).json()).items.map(item => item.id);
  const offset = 10;
  await asDesktop(page, {restore: {state: {route: '/downloads', owners: {downloads: {
    shown: true, filter: '', order: 'newest', offset, search: '', selection: [all[offset], 999999], scroll: 0}}}, since: 1}});
  await load(page, '/');
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect.poll(async () => (await rowIds(page)).includes(all[offset])).toBe(true);
  const shown = await rowIds(page);
  const state = await downloads(page);
  // Whatever the page size here: the page holding row `offset`, exactly as the API lists it.
  const size = shown.length;
  expect(state.page).toBe(Math.floor(offset / size) + 1);
  expect(shown).toEqual(all.slice((state.page - 1) * size, state.page * size));
  expect(state.order).toBe('newest');
  expect(state.checked).toEqual([all[offset]]);                   // 999999 is not there: dropped
});

test('D02: the filter and search come back; a place past the end shows the last page', async ({page, request}) => {
  const matching = (await (await request.get('/api/torrents?search=file-1&limit=100')).json()).items.map(item => item.id);
  await asDesktop(page, {restore: {state: {route: '/downloads', owners: {downloads: {
    shown: true, filter: '', order: 'queue', offset: 5000, search: 'file-1', selection: [], scroll: 0}}}, since: 1}});
  await load(page, '/');
  await expect(page.locator('#view-torrents .ftab[data-dp-status=""]')).toHaveClass(/\bactive\b/);
  const state = await downloads(page);
  expect(state.filter).toBe('');
  expect(state.search).toBe('file-1');
  // The last page, not an empty one past the end: rows are shown and nothing follows.
  await expect.poll(() => rowIds(page).then(ids => ids.length)).toBeGreaterThan(0);
  // The search is the one the list answers, not only the field's text.
  expect((await rowIds(page)).every(id => matching.includes(id))).toBe(true);
  expect(await page.evaluate(() => [...document.querySelectorAll('#torrent-page-btns button')]
    .some(button => button.dataset.dpPager === 'next' && !button.disabled))).toBe(false);
});

test('D03: a window that was not on Downloads keeps only what an entry keeps', async ({page}) => {
  await asDesktop(page, {restore: {state: {route: '/', owners: {downloads: {
    shown: false, filter: 'processing', order: 'newest', offset: 30, search: '', selection: [1], scroll: 0}}}, since: 1}});
  await load(page, '/');
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect.poll(() => downloads(page).then(state => state.page)).toBe(1);
  const state = await downloads(page);
  expect([state.filter, state.order, state.checked]).toEqual(['processing', 'newest', []]);
});

test('D04: what the person set on Downloads is what the answer carries', async ({page}) => {
  const seen = await asDesktop(page);
  await load(page, '/downloads');
  await page.locator('#torrent-order').selectOption('newest');
  await expect.poll(() => downloads(page).then(state => state.order)).toBe('newest');
  await page.locator('#torrent-page-btns button[data-dp-pager="next"]').click();
  await expect.poll(() => downloads(page).then(state => state.page)).toBe(2);
  const first = (await rowIds(page))[0];
  await page.locator(`.t-chk[data-id="${first}"]`).check();
  await expect.poll(() => releasable(page)).toBe(true);
  await page.evaluate(() => window.DPWindowState.answer('nonce-d'));
  await expect.poll(() => seen.puts.length).toBe(1);
  expect(seen.puts[0].releasable).toBe(true);
  expect(seen.puts[0].state.route).toBe('/downloads');
  const size = (await rowIds(page)).length;
  expect(seen.puts[0].state.owners.downloads).toMatchObject({shown: true, filter: '', order: 'newest', offset: size, selection: [first]});
});

test('Q01: the Quick Add draft and its caret come back, and are what the answer carries', async ({page}) => {
  await asDesktop(page, {restore: {state: {route: '/', owners: {'quick-add': {text: 'https://example.invalid/a\nhttps://example.invalid/b', start: 4, end: 9}}}, since: 1}});
  await load(page, '/');
  const field = page.locator('#q-transfer-input');
  await expect(field).toHaveValue('https://example.invalid/a\nhttps://example.invalid/b');
  expect(await field.evaluate(input => [input.selectionStart, input.selectionEnd])).toEqual([4, 9]);
});

test('Q02: a typed Quick Add draft is captured, and an empty one is not', async ({page}) => {
  const seen = await asDesktop(page);
  await load(page, '/');
  await expect.poll(() => releasable(page)).toBe(true);
  await page.evaluate(() => window.DPWindowState.answer('nonce-q0'));
  await expect.poll(() => seen.puts.length).toBe(1);
  expect(seen.puts[0].state.owners['quick-add']).toBeUndefined();
  await page.locator('#q-transfer-input').fill('magnet:?xt=urn:btih:spec');
  await expect.poll(() => releasable(page)).toBe(true);
  await page.evaluate(() => window.DPWindowState.answer('nonce-q1'));
  await expect.poll(() => seen.puts.length).toBe(2);
  expect(seen.puts[1].state.owners['quick-add']).toMatchObject({text: 'magnet:?xt=urn:btih:spec'});
});

test('D05: the table scroll comes back where the page can hold it', async ({page}) => {
  await asDesktop(page, {restore: {state: {route: '/downloads', owners: {downloads: {
    shown: true, filter: '', order: 'queue', offset: 0, search: '', selection: [], scroll: 60}}}, since: 1}});
  await load(page, '/', {height: 600});
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  const room = await page.evaluate(() => { const wrap = document.querySelector('#view-torrents .dp-downloads-table-wrap'); return wrap.scrollHeight - wrap.clientHeight; });
  await expect.poll(() => page.evaluate(() => document.querySelector('#view-torrents .dp-downloads-table-wrap').scrollTop))
    .toBe(Math.min(60, room));
});

test('D06: a slow restore settles before the interface is shown, and never overrides the person afterwards', async ({page}) => {
  await page.route(url => url.pathname === '/api/torrents', async route => { await new Promise(resolve => setTimeout(resolve, 1200)); route.continue(); });
  await asDesktop(page, {restore: {state: {route: '/downloads', owners: {downloads: {
    shown: true, filter: '', order: 'queue', offset: 20, search: '', selection: [], scroll: 0}}}, since: 1}});
  await page.addInitScript(() => {
    window.__readyPage = null;
    document.addEventListener('downloadpulse:interface-ready', () => {
      window.__readyPage = Number(document.querySelector('#torrent-page-btns [aria-current="page"]')?.textContent || 0);
    }, {once: true});
  });
  await load(page, '/');
  // When the interface is ready, the page is already the restored one (not page 1 on its way there).
  await expect.poll(() => page.evaluate(() => window.__readyPage), {timeout: 20000}).toBeGreaterThan(1);
  // The person's own entry afterwards is an ordinary one: page 1, nothing selected.
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect.poll(() => downloads(page).then(state => state.page), {timeout: 10000}).toBe(1);
  await page.waitForTimeout(3000);
  expect((await downloads(page)).page).toBe(1);
  expect((await downloads(page)).checked).toEqual([]);
});

test('D07: an open source chooser keeps the window', async ({page}) => {
  await asDesktop(page);
  await load(page, '/downloads');
  await expect.poll(() => rowIds(page).then(ids => ids.length)).toBeGreaterThan(0);
  await expect.poll(() => releasable(page)).toBe(true);
  const id = (await rowIds(page))[0];
  // The route status the chooser reads first (a labelled mock: the seeded direct links have no route to choose).
  await page.route(url => url.pathname === `/api/torrents/${id}/route`, route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({scope: 'artifact', options: [], current: null})}));
  await page.evaluate(id => window.DPRootProvider.open(id, document.querySelector(`#t-tbody tr[data-torrent-id="${id}"]`)), id);
  await expect.poll(() => page.evaluate(() => [...document.querySelectorAll('.dp-root-provider-menu')].some(menu => !menu.hidden))).toBe(true);
  expect(await releasable(page)).toBe(false);
});

// ── WS8b3: Activity Log and Statistics ──────────────────────────────────
const shownEvents = page => page.evaluate(() => [...document.querySelectorAll('#event-list .dp-activity-row')].map(row => row._dpEvent?.id));

test('A01: the Activity Log comes back to the same investigation and page', async ({page, request}) => {
  const first = await (await request.get('/api/events?limit=100')).json();
  const second = await (await request.get(`/api/events?limit=100&snapshot=${first.snapshot}&before=${first.next_before}`)).json();
  await asDesktop(page, {restore: {state: {route: '/activity', owners: {activity: {
    shown: true, applied: {timeframe: 'all', level: '', category: ''}, search: '',
    cursor: {snapshot: first.snapshot, windowStart: first.window_start ?? null},
    request: {kind: 'next', before: first.next_before, after: null}, nav: {page: 2, pages: null, lastUnavailable: null},
    open: [], scroll: 0}}}, since: 1}});
  await load(page, '/');
  await expect(page.locator('#view-events')).toHaveClass(/\bactive\b/);
  await expect.poll(() => shownEvents(page)).toEqual(second.items.map(item => item.id));
  await expect(page.locator('#ev-pagination [aria-current="page"], .dp-activity-pagination [aria-current="page"]').first()).toHaveText('2');
});

test('A02: filters and search come back; a window not on the Activity Log starts a new investigation with them', async ({page, request}) => {
  const newest = await (await request.get('/api/events?limit=100&category=routing')).json();
  await asDesktop(page, {restore: {state: {route: '/', owners: {activity: {
    shown: false, applied: {timeframe: 'all', level: '', category: 'routing'}, search: '',
    cursor: {snapshot: 1, windowStart: null}, request: {kind: 'next', before: 5, after: null}, nav: {page: 4, pages: null, lastUnavailable: null},
    open: [], scroll: 0}}}, since: 1}});
  await load(page, '/');
  await page.locator('#sidebar .nav-item[data-view="events"]').click();
  await expect(page.locator('#view-events')).toHaveClass(/\bactive\b/);
  await expect.poll(() => shownEvents(page)).toEqual(newest.items.map(item => item.id));
});

test('A03: an Auto Refresh switched off comes back off', async ({page}) => {
  await asDesktop(page, {restore: {state: {route: '/activity', owners: {'activity-live': false}}, since: 1}});
  await load(page, '/');
  await expect(page.locator('#view-events')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#ev-live')).not.toBeChecked();
});

test('A04: the investigation the person set is what the answer carries', async ({page, request}) => {
  const seen = await asDesktop(page);
  await load(page, '/activity');
  await expect.poll(() => shownEvents(page).then(ids => ids.length)).toBeGreaterThan(0);
  const first = await shownEvents(page);
  await page.locator('#ev-pagination button[data-dp-pager="next"], .dp-activity-pagination button[data-dp-pager="next"]').first().click();
  await expect.poll(() => shownEvents(page).then(ids => ids[0] !== first[0])).toBe(true);
  await page.locator('.dp-activity-live').click();
  await expect(page.locator('#ev-live')).not.toBeChecked();
  await expect.poll(() => releasable(page)).toBe(true);
  await page.evaluate(() => window.DPWindowState.answer('nonce-a'));
  await expect.poll(() => seen.puts.length).toBe(1);
  const part = seen.puts[0].state.owners.activity;
  expect(part.shown).toBe(true);
  expect(part.request.kind).toBe('next');
  expect(part.nav.page).toBe(2);
  expect(Number.isInteger(part.cursor.snapshot)).toBe(true);
  expect(seen.puts[0].state.owners['activity-live']).toBe(false);
});

test('A05: an open event detail comes back open', async ({page, request}) => {
  const first = await (await request.get('/api/events?limit=100')).json();
  const withDetail = first.items.find(item => item.detail);
  test.skip(!withDetail, 'no event with a detail on this server');
  await asDesktop(page, {restore: {state: {route: '/activity', owners: {activity: {
    shown: true, applied: {timeframe: 'all', level: '', category: ''}, search: '',
    cursor: {snapshot: first.snapshot, windowStart: first.window_start ?? null}, request: {kind: null, before: null, after: null},
    nav: {page: 1, pages: null, lastUnavailable: null}, open: [withDetail.id], scroll: 0}}}, since: 1}});
  await load(page, '/');
  await expect.poll(() => page.evaluate(id => [...document.querySelectorAll('#event-list .dp-activity-row')]
    .find(row => row._dpEvent?.id === id)?.querySelector('.dp-activity-detail')?.open, withDetail.id)).toBe(true);
});

test('S01: the Statistics period comes back, and is what the answer carries', async ({page}) => {
  const seen = await asDesktop(page, {restore: {state: {route: '/statistics', owners: {statistics: {period: '30d'}}}, since: 1}});
  await load(page, '/');
  await expect(page.locator('#view-stats')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#stats-period-tabs .ftab[data-period="30d"]')).toHaveClass(/\bactive\b/);
  await page.locator('#stats-period-tabs .ftab[data-period="24h"]').click();
  await expect.poll(() => releasable(page)).toBe(true);
  await page.evaluate(() => window.DPWindowState.answer('nonce-s'));
  await expect.poll(() => seen.puts.length).toBe(1);
  expect(seen.puts[0].state.owners.statistics).toEqual({period: '24h'});
});

test('A06: an opened detail stays open when the page is drawn again, and is what the answer carries', async ({page, request}) => {
  const first = await (await request.get('/api/events?limit=100')).json();
  const withDetail = first.items.find(item => item.detail);
  test.skip(!withDetail, 'no event with a detail on this server');
  const seen = await asDesktop(page);
  await load(page, '/activity');
  const detail = id => page.evaluate(id => [...document.querySelectorAll('#event-list .dp-activity-row')]
    .find(row => row._dpEvent?.id === id)?.querySelector('.dp-activity-detail'), id);
  await expect.poll(() => shownEvents(page).then(ids => ids.includes(withDetail.id))).toBe(true);
  await page.evaluate(id => { [...document.querySelectorAll('#event-list .dp-activity-row')].find(row => row._dpEvent?.id === id).querySelector('.dp-activity-detail > summary').click(); }, withDetail.id);
  await page.locator('#ev-refresh, [data-dp-click="loadActivityLog"]').first().click();
  await expect.poll(() => page.evaluate(id => [...document.querySelectorAll('#event-list .dp-activity-row')]
    .find(row => row._dpEvent?.id === id)?.querySelector('.dp-activity-detail')?.open, withDetail.id)).toBe(true);
  await page.evaluate(() => window.DPWindowState.answer('nonce-o'));
  await expect.poll(() => seen.puts.length).toBe(1);
  expect(seen.puts[0].state.owners.activity.open).toEqual([withDetail.id]);
});

// The page shown is what comes back -- never a page turn that was refused or failed.
for (const [name, from, refuse] of [['a refused Last', 'last', 409], ['a failed Next', 'next', 500]]) {
  test(`A07: after ${name}, the answer carries the page still shown`, async ({page, request}) => {
    const seen = await asDesktop(page);
    await load(page, '/activity');
    await page.locator('.dp-activity-live').click();
    await expect(page.locator('#ev-live')).not.toBeChecked();
    await expect.poll(() => shownEvents(page).then(ids => ids.length)).toBeGreaterThan(0);
    const shown = await shownEvents(page);
    await page.route(url => url.pathname === '/api/events' && url.searchParams.has(from === 'last' ? 'last' : 'before'),
      route => route.fulfill({status: refuse, contentType: 'application/json', body: JSON.stringify({detail: {message: 'refused'}})}));
    await page.locator(`.dp-activity-pagination button[data-dp-pager="${from}"], #ev-pagination button[data-dp-pager="${from}"]`).first().click();
    await expect.poll(() => releasable(page)).toBe(true);
    // A refused Last leaves the page as it was; a failed turn says it failed (nothing to bring back).
    if (from === 'last') expect(await shownEvents(page)).toEqual(shown);
    await page.evaluate(() => window.DPWindowState.answer('nonce-f'));
    await expect.poll(() => seen.puts.length).toBe(1);
    const part = seen.puts[0].state.owners.activity;
    expect(part.nav.page).toBe(1);
    expect(part.request.kind).not.toBe(from);
    // Restored from that answer, the same rows -- the API's own newest page in that snapshot.
    const newest = await (await request.get(`/api/events?limit=${shown.length}&snapshot=${part.cursor.snapshot}`)).json();
    expect(newest.items.map(item => item.id)).toEqual(shown);
    const again = await page.context().newPage();
    await asDesktop(again, {restore: {state: {route: '/activity', owners: {activity: part}}, since: 1}});
    await load(again, '/');
    await expect.poll(() => shownEvents(again)).toEqual(shown);
  });
}

test('A08: while a page is being asked for, the Activity Log keeps the window', async ({page}) => {
  await asDesktop(page);
  await load(page, '/activity');
  await expect.poll(() => shownEvents(page).then(ids => ids.length)).toBeGreaterThan(0);
  await expect.poll(() => releasable(page)).toBe(true);
  let release;
  await page.route(url => url.pathname === '/api/events', route => new Promise(resolve => { release = () => resolve(route.continue()); }));
  await page.locator('[data-dp-click="loadActivityLog"]').first().click();
  await expect.poll(() => typeof release).toBe('function');
  expect(await releasable(page)).toBe(false);
  release();
  await expect.poll(() => releasable(page)).toBe(true);
});
