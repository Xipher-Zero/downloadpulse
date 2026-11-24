const { test, expect } = require('@playwright/test');

/* DP 1.0.3 WS20 -- Details stays live while it is open: a semantic
 * torrent_updated naming the open transfer (or a progress item whose status
 * changed, or a consolidation naming it its canonical target) re-reads it (one
 * read in flight, one trailing) and draws it again in place, the reader's
 * scroll and focus kept, held while a pointer is pressed or its menu is open;
 * a plain progress item reads nothing and re-says only Progress; another
 * transfer's change reads nothing, and a closed Details reads nothing more.
 * The stream is the one app.js EventSource, given its events by the test
 * (the fixture of consolidation-events.spec.js / file-selection-affordances). */

const detail = (id, extra) => ({
  id, name: `Release ${id}`, status: 'downloading', progress: 40, size_bytes: 1024, source: 'direct_link',
  created_at: '2026-01-01T00:00:00Z', completed_at: null, hash: '', source_outcomes: [],
  files: [{id: 1, filename: 'release.bin', size_bytes: 1024, status: 'downloading'}],
  // Enough events that the body scrolls.
  events: Array.from({length: 60}, (_, index) => ({level: 'info', message: `event ${index}`, created_at: '2026-01-01T00:00:00Z'})),
  executors: ['aria2'], route_attempts: [], execution_attempts: [],
  original_resource: 'https://files.example/release.bin',
  original_resource_sanitized_full: 'https://files.example/release.bin', original_resource_copy_allowed: true,
  ...extra,
});

async function boot(page) {
  await page.addInitScript(() => {
    class FakeEventSource {
      constructor(url) { this.url = url; this.listeners = new Map(); window.__dpFakeEventSource = this; }
      addEventListener(type, handler) { this.listeners.set(type, [...(this.listeners.get(type) || []), handler]); }
      close() {}
      emit(type, payload) { for (const handler of this.listeners.get(type) || []) handler({data: JSON.stringify(payload)}); }
    }
    FakeEventSource.CONNECTING = 0; FakeEventSource.OPEN = 1; FakeEventSource.CLOSED = 2;
    window.EventSource = FakeEventSource;
  });
  const state = {rows: new Map([[10, detail(10)], [11, detail(11)]]), reads: [], gate: null};
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => /^\/api\/torrents\/\d+$/.test(url.pathname), async route => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    state.reads.push(id);
    if (state.gate) await state.gate;
    const row = state.rows.get(id);
    return row ? route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(row)})
      : route.fulfill({status: 404, contentType: 'application/json', body: JSON.stringify({detail: 'Transfer not found'})});
  });
  await page.route(url => url.pathname === '/api/torrents', route => {
    const items = [...state.rows.values()];
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items, total: items.length})});
  });
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents tr[data-torrent-id="10"]:visible').first()).toBeVisible();
  await page.evaluate(() => window.__dpFakeEventSource.emit('connected', {}));
  return state;
}
const emit = (page, payload) => page.evaluate(data => window.__dpFakeEventSource.emit('torrent_updated', data), payload);
const reads = (state, id) => state.reads.filter(read => read === id).length;
const detailStatus = page => page.locator('#modal-body .detail-grid .badge').first();
const rowStatus = page => page.locator('#view-torrents tr[data-torrent-id="10"]:visible .badge').first();
async function open(page) {
  await page.locator('#view-torrents tr[data-torrent-id="10"]:visible').first().click();
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
}
/* The positive boundary after something that must read nothing: a semantic
 * change of the open transfer reads it exactly once and draws it. Events are
 * handled in the order they are sent and reads are coalesced, so once this
 * one's answer is drawn, every read an earlier event started has been made. */
async function probe(page, state, name = 'Probe release') {
  state.rows.set(10, detail(10, {name}));
  await emit(page, {id: 10, status: 'downloading'});
  await expect(page.locator('#modal-title')).toHaveText(name);
}
// The answer Details holds while a pointer is pressed or its menu is open.
const held = page => page.evaluate(() => dpDetailHeld?.t?.name ?? null);
// Every title Details says from now on.
const recordTitles = page => page.evaluate(() => {
  const title = document.getElementById('modal-title');
  window.__dpTitles = [];
  new MutationObserver(() => window.__dpTitles.push(title.textContent))
    .observe(title, {childList: true, characterData: true, subtree: true});
});
const titles = page => page.evaluate(() => window.__dpTitles);

test('a state change of the open transfer updates its status in Details, as the row says it, without reopening', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const before = await detailStatus(page).textContent();
  await expect(rowStatus(page)).toHaveText(before);

  state.rows.set(10, detail(10, {status: 'paused'}));
  await emit(page, {id: 10, status: 'paused'});
  await expect(rowStatus(page)).not.toHaveText(before);
  const now = (await rowStatus(page).textContent()).trim();
  await expect(detailStatus(page)).toHaveText(now);
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);

  // A progress batch naming it (with a status change) reads it too.
  const counted = reads(state, 10);
  state.rows.set(10, detail(10, {status: 'downloading', progress: 70}));
  await emit(page, {progress_only: true, items: [{id: 10, progress: 70, status_changed: true}]});
  await expect(detailStatus(page)).toHaveText(before);
  expect(reads(state, 10)).toBeGreaterThan(counted);
});

test('reads coalesce: one in flight, and one trailing read for changes meanwhile', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const counted = reads(state, 10);
  let release;
  state.gate = new Promise(resolve => { release = resolve; });
  await emit(page, {progress_only: true, items: [{id: 10, progress: 41, status_changed: true}]});
  await expect.poll(() => reads(state, 10)).toBe(counted + 1);
  for (const progress of [42, 43, 44]) await emit(page, {progress_only: true, items: [{id: 10, progress, status_changed: true}]});
  state.gate = null;
  state.rows.set(10, detail(10, {progress: 44}));
  release();
  await expect.poll(() => reads(state, 10)).toBe(counted + 2);
  await probe(page, state);
  expect(reads(state, 10)).toBe(counted + 3);
});

test('another transfer\'s change reads nothing for the open Details', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const counted = reads(state, 10);
  state.rows.set(11, detail(11, {status: 'paused'}));
  await emit(page, {id: 11, status: 'paused'});
  await emit(page, {progress_only: true, items: [{id: 11, progress: 55}]});
  await expect(page.locator('#view-torrents tr[data-torrent-id="11"]:visible .badge').first()).toHaveText(/./);
  await probe(page, state);
  expect(reads(state, 10)).toBe(counted + 1);
  expect(reads(state, 11)).toBe(0);
});

test('after closing, a change of that transfer reads nothing, and a read answered after close draws nothing', async ({page}) => {
  const state = await boot(page);
  await open(page);
  // A read in flight when Details closes.
  let release;
  state.gate = new Promise(resolve => { release = resolve; });
  const counted = reads(state, 10);
  await emit(page, {id: 10, status: 'paused'});
  await expect.poll(() => reads(state, 10)).toBe(counted + 1);
  await page.keyboard.press('Escape');
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  await recordTitles(page);
  state.gate = null;
  const answered = page.waitForResponse(response => new URL(response.url()).pathname === '/api/torrents/10');
  release();
  await answered;
  await emit(page, {id: 10, status: 'paused'});
  await emit(page, {progress_only: true, items: [{id: 10, progress: 60}]});
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  // The boundary: opening it again reads it once and draws that answer alone.
  state.rows.set(10, detail(10, {name: 'Reopened release'}));
  await page.evaluate(() => showDetail(10));
  await expect(page.locator('#modal-title')).toHaveText('Reopened release');
  expect(reads(state, 10)).toBe(counted + 2);
  expect(await titles(page)).not.toContain('Release 10');
});

test('a refresh keeps the reader\'s scroll, focus and Original Resource element', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const body = page.locator('#modal-body');
  await body.evaluate(node => { node.scrollTop = 200; });
  const resource = page.locator('.dp-detail-resource');
  await resource.focus();
  await page.evaluate(() => { window.__dpResource = document.querySelector('.dp-detail-resource'); });

  const before = await detailStatus(page).textContent();
  state.rows.set(10, detail(10, {status: 'paused'}));
  await emit(page, {id: 10, status: 'paused'});
  await expect(rowStatus(page)).not.toHaveText(before);
  await expect(detailStatus(page)).toHaveText((await rowStatus(page).textContent()).trim());
  expect(await body.evaluate(node => node.scrollTop)).toBe(200);
  expect(await page.evaluate(() => document.activeElement === window.__dpResource && window.__dpResource.isConnected)).toBe(true);
});

test('a transfer deleted while open shows Details\' own failure and is not read again', async ({page}) => {
  const state = await boot(page);
  await open(page);
  state.rows.delete(10);
  await emit(page, {progress_only: true, items: [{id: 10, status_changed: true}]});
  await expect(page.locator('#modal-body .empty')).toBeVisible();
  const counted = reads(state, 10);
  // (A progress batch: Details' own signal. A semantic one is also Common
  // Sources' own re-read of its launcher, outside Details' refresh.)
  await emit(page, {progress_only: true, items: [{id: 10, progress: 80}]});
  // The boundary: opening it again (it is back) reads it once.
  state.rows.set(10, detail(10, {name: 'Restored release'}));
  await page.evaluate(() => showDetail(10));
  await expect(page.locator('#modal-title')).toHaveText('Restored release');
  expect(reads(state, 10)).toBe(counted + 1);
});

const item = (id, extra) => ({id, status: 'downloading', progress: 40, active_execution_progress: null,
  active_execution_basis: null, acquisition_eta: null, status_changed: false, ...extra});

test('a progress item without a status change re-says Progress only: no read, the Files table kept', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const counted = reads(state, 10);
  await page.evaluate(() => { window.__dpFiles = document.querySelector('#modal-body .dp-detail-files-card table'); window.__dpGrid = document.querySelector('#modal-body .detail-grid'); });
  expect(await page.evaluate(() => Boolean(window.__dpFiles))).toBe(true);
  await emit(page, {progress_only: true, items: [item(10, {progress: 73.5})]});
  await expect(page.locator('#modal-body .dp-detail-progress .dv')).toContainText('73.5');
  await emit(page, {progress_only: true, items: [item(10, {progress: 74.5})]});
  await expect(page.locator('#modal-body .dp-detail-progress .dv')).toContainText('74.5');
  expect(await page.evaluate(() => window.__dpFiles.isConnected && window.__dpGrid.isConnected)).toBe(true);
  // No read was made for them (so no answer can redraw the table later).
  await probe(page, state);
  expect(reads(state, 10)).toBe(counted + 1);
});

test('a progress item whose status changed reads the transfer once', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const counted = reads(state, 10);
  state.rows.set(10, detail(10, {status: 'paused'}));
  await emit(page, {progress_only: true, items: [item(10, {status: 'paused', status_changed: true}), item(11, {progress: 50})]});
  await expect(detailStatus(page)).not.toHaveText(/Downloading/);
  await probe(page, state);
  expect(reads(state, 10)).toBe(counted + 2);
});

test('an answer arriving while a pointer is pressed in Details, or its menu is open, is drawn on release', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const title = page.locator('#modal-title');
  // A pressed pointer.
  const box = await page.locator('#modal-body .detail-grid').boundingBox();
  await page.mouse.move(box.x + 8, box.y + 8);
  await page.mouse.down();
  let counted = reads(state, 10);
  state.rows.set(10, detail(10, {name: 'Pressed release'}));
  await emit(page, {id: 10, status: 'downloading'});
  await expect.poll(() => held(page)).toBe('Pressed release');      // the answer is here, and held
  await expect(title).toHaveText('Release 10');
  await page.mouse.up();
  await expect(title).toHaveText('Pressed release');
  // An open Original Resource menu.
  await page.locator('.dp-detail-resource').click({button: 'right'});
  await expect(page.locator('.dp-row-menu')).toHaveCount(1);
  counted = reads(state, 10);
  state.rows.set(10, detail(10, {name: 'Menu release'}));
  await emit(page, {id: 10, status: 'downloading'});
  await expect.poll(() => held(page)).toBe('Menu release');
  await expect(title).toHaveText('Pressed release');
  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-row-menu')).toHaveCount(0);
  await expect(title).toHaveText('Menu release');
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
});

test('a press whose release is lost (the window blurs, no pointerup) does not stall the refresh', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const title = page.locator('#modal-title');
  const box = await page.locator('#modal-body .detail-grid').boundingBox();
  await page.mouse.move(box.x + 8, box.y + 8);
  await page.mouse.down();
  state.rows.set(10, detail(10, {name: 'Pressed release'}));
  await emit(page, {id: 10, status: 'downloading'});
  await expect.poll(() => held(page)).toBe('Pressed release');
  await expect(title).toHaveText('Release 10');
  // Let go outside the window: no pointerup or pointercancel reaches the page, only its blur.
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await expect(title).toHaveText('Pressed release');
  // And the next change is drawn at once, with no release.
  await probe(page, state, 'After blur release');
  await page.mouse.up();
});

test('an answer held for an earlier opening is never drawn over a reopening', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const title = page.locator('#modal-title');
  const box = await page.locator('#modal-body .detail-grid').boundingBox();
  await page.mouse.move(box.x + 8, box.y + 8);
  await page.mouse.down();
  const counted = reads(state, 10);
  state.rows.set(10, detail(10, {name: 'Stale release'}));
  await emit(page, {id: 10, status: 'downloading'});
  await expect.poll(() => reads(state, 10)).toBe(counted + 1);
  // Reopened (the same transfer) while that answer waits for the pointer.
  state.rows.set(10, detail(10, {name: 'Fresh release'}));
  await page.evaluate(() => showDetail(10));
  await expect(title).toHaveText('Fresh release');
  await recordTitles(page);
  await page.mouse.up();
  // A held answer is drawn in the frame after the release: the owner's own.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => resolve())));
  await expect(title).toHaveText('Fresh release');
  // Whatever the earlier opening's read answered has been handled before this one's.
  await probe(page, state);
  expect(await titles(page)).not.toContain('Stale release');
});

test('a consolidation into the open transfer re-reads it, though its torrent_updated names the source', async ({page}) => {
  const state = await boot(page);
  await open(page);
  const counted = reads(state, 10);
  state.rows.set(10, detail(10, {name: 'Consolidated release'}));
  await emit(page, {id: 11, status: 'completed'});
  await page.evaluate(() => window.__dpFakeEventSource.emit('duplicate_consolidated',
    {source_transfer_id: 11, canonical_transfer_ids: [10], material_count: 1, matched_count: 1}));
  await expect(page.locator('#modal-title')).toHaveText('Consolidated release');
  await probe(page, state);
  expect(reads(state, 10)).toBe(counted + 2);
});
