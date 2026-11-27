const { test, expect } = require('@playwright/test');

/* DownloadPulse 1.0.8 (WS-D1): the Dashboard is one workspace at every desktop
 * geometry. The view is exactly the content box #content leaves, the KPI
 * strip and Quick Add keep their heights, and Download Activity takes what
 * they leave: its card ends at the content box's inset, its table viewport is
 * the one that scrolls, and the page itself never does -- on a fresh load at
 * the window's size and after a load at another size (a window laid out
 * hidden, then shown, or maximized and restored). Only a window too short for
 * the strip, Quick Add and Download Activity's floor (220px) lets the page
 * scroll, with nothing clipped. Mocked rows only; everything is read from
 * rendered geometry. */

function row(id) {
  return {
    id, name: `Dashboard.Fit.Row.${id}.mkv`, status: 'downloading', presentation_status: 'downloading',
    presentation_label: 'Downloading', presentation_badge_status: 'downloading', attention_required: false,
    progress: 40, active_execution_progress: 40, active_execution_basis: 'bytes', size_bytes: 1.5 * 1024 ** 3,
    source: 'manual', hash: 'a1b2c3d4e5f60718293a4b5c6d7e8f90', label: '', created_at: '2025-12-28T22:54:00Z',
    current_source_identity: {kind: 'link'}, providers: [], historical_providers: [], delivering_provider_ids: [],
    active_provider_id: 'general_http', active_provider_name: 'HTTP & HTTPS', input_required: null,
    acquisition_eta: {seconds: 2318, held: false},
  };
}
const ITEMS = Array.from({length: 40}, (_, k) => row(7000 + k));

async function open(page, {width, height}) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => url.pathname === '/api/torrents', route => {
    const limit = Number(new URL(route.request().url()).searchParams.get('limit')) || 25;
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: ITEMS.slice(0, limit), total: ITEMS.length})});
  });
  await page.setViewportSize({width, height});
  await page.goto('/');
  await expect(page.locator('#dash-tbody tr[data-torrent-id]').first()).toBeVisible();
}

// The workspace as it stands: the page's own scroll, the card against the
// content box, the table viewport and its rows.
const workspace = page => page.evaluate(() => {
  const content = document.getElementById('content'), cs = getComputedStyle(content);
  const view = document.getElementById('view-dashboard'), card = document.getElementById('dash-activity-card');
  const wrap = card.querySelector('.dash-activity-table-wrap');
  const rows = [...wrap.querySelectorAll('#dash-tbody tr[data-torrent-id]')];
  const box = content.getBoundingClientRect(), c = card.getBoundingClientRect(), w = wrap.getBoundingClientRect();
  return {
    pageOverflow: content.scrollHeight - content.clientHeight,
    contentOverflowY: cs.overflowY,
    viewHeight: view.getBoundingClientRect().height,
    contentBox: content.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom),
    cardBottom: c.bottom, cardHeight: c.height,
    contentBottom: box.top + content.clientTop + content.clientHeight - parseFloat(cs.paddingBottom),
    wrapOverflowY: getComputedStyle(wrap).overflowY, wrapBottom: w.bottom,
    wrapScroll: wrap.scrollHeight - wrap.clientHeight,
    rows: rows.length, lastBottom: rows.length ? rows.at(-1).getBoundingClientRect().bottom : null,
  };
});

// The fitted rows have settled (the resize fit answers after 120 ms, then renders once).
async function settled(page) {
  let previous = null;
  await expect.poll(async () => {
    const now = JSON.stringify(await workspace(page));
    const same = now === previous;
    previous = now;
    return same;
  }, {intervals: [300, 300, 300, 300, 300, 300, 300, 300, 300, 300]}).toBe(true);
  return workspace(page);
}

function fits(w, where) {
  expect(w.pageOverflow, `${where}: the page scrolls`).toBeLessThanOrEqual(1);
  expect(Math.abs(w.viewHeight - w.contentBox), `${where}: the view is the content box`).toBeLessThanOrEqual(1);
  expect(Math.abs(w.cardBottom - w.contentBottom), `${where}: Download Activity ends at the content inset`).toBeLessThanOrEqual(1);
  expect(w.wrapOverflowY, `${where}: the table viewport scrolls itself`).toBe('auto');
  expect(w.rows, `${where}: rows drawn`).toBeGreaterThanOrEqual(1);
  // The rows drawn are the ones the table viewport shows whole; any more scroll inside it, never the page.
  if (w.wrapScroll <= 0) expect(w.lastBottom, `${where}: last row inside the table viewport`).toBeLessThanOrEqual(w.wrapBottom + 0.5);
}

const DESKTOP = [[1260, 780], [1260, 810], [1280, 820], [1440, 900], [1024, 700], [1920, 1080], [1920, 1140]];

for (const [width, height] of DESKTOP) {
  test(`the Dashboard fits a ${width}x${height} window with no page scroll, on a fresh load`, async ({page}) => {
    await open(page, {width, height});
    fits(await settled(page), `${width}x${height}`);
  });
}

test('the Dashboard fits its window after a load at another size: hidden-then-shown, maximize then restore', async ({page}) => {
  test.setTimeout(90_000);
  // A window laid out small (hidden), then shown at its size; maximized, then restored.
  await open(page, {width: 800, height: 600});
  for (const [width, height] of [[1260, 810], [1920, 1140], [1260, 810], [1024, 700], [1440, 900], [1260, 780]]) {
    await page.setViewportSize({width, height});
    fits(await settled(page), `after resize to ${width}x${height}`);
  }
});

test('the Dashboard drawn into the content box keeps no stale height when it is shown again', async ({page}) => {
  await open(page, {width: 1260, height: 810});
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.setViewportSize({width: 1280, height: 820});
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
  fits(await settled(page), 'back on the Dashboard at 1280x820');
});

test('a window too short for the Dashboard\'s sections lets the page scroll, Download Activity at its floor, nothing clipped', async ({page}) => {
  await open(page, {width: 1260, height: 560});
  const w = await settled(page);
  expect(w.contentOverflowY).toBe('auto');
  expect(w.pageOverflow).toBeGreaterThan(0);
  expect(Math.round(w.cardHeight)).toBe(220);
  expect(w.rows).toBeGreaterThanOrEqual(1);
  const reached = await page.evaluate(() => {
    const content = document.getElementById('content');
    content.scrollTop = content.scrollHeight;
    const card = document.getElementById('dash-activity-card').getBoundingClientRect();
    const box = content.getBoundingClientRect();
    return card.bottom <= box.bottom - content.clientTop + 0.5;
  });
  expect(reached, 'Download Activity reachable by the page scroll').toBe(true);
});
