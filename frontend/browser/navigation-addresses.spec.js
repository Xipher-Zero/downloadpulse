const { test, expect } = require('@playwright/test');

/* WS11 Navigation. Each page has its own address (app.js VIEW_PATHS, served
 * by main.py SHELL_SURFACE_PATHS): choosing a page writes it through the
 * History API, Back and Forward show the page their entry names, and a reload
 * opens the page its address names. Escape closes the Details window through
 * the shared modal owner (DPModal), focus returning to the row that opened it
 * -- or to the row drawn in its place if the list was redrawn meanwhile. */

const transfer = {
  id: 7, name: 'Transfer 7', display_name: 'Transfer 7', status: 'completed', progress: 100, size_bytes: 1024,
  source: 'direct_link', hash: 'hash-7', created_at: '2026-10-01T12:00:00+00:00', completed_at: '2026-10-01T12:00:00+00:00',
  files: [], source_outcomes: [], executors: ['aria2'], route_attempts: [], execution_attempts: [], events: [],
};

async function open(page, path) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto(path);
  await page.waitForFunction(() => typeof showDetail === 'function' && document.readyState === 'complete');
}
const activeView = page => page.locator('.view.active').getAttribute('id');

test('choosing a page writes its address; Back and Forward switch pages; reload restores the page', async ({page}) => {
  await open(page, '/');
  expect(await activeView(page)).toBe('view-dashboard');
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page).toHaveURL(/\/downloads$/);
  await expect(page.locator('#view-torrents')).toHaveClass(/active/);
  await page.locator('#sidebar .nav-item[data-view="stats"]').click();
  await expect(page).toHaveURL(/\/statistics$/);
  await page.locator('#sidebar .nav-item[data-view="help"]').click();
  await expect(page).toHaveURL(/\/help$/);

  await page.goBack();
  await expect(page).toHaveURL(/\/statistics$/);
  await expect(page.locator('#view-stats')).toHaveClass(/active/);
  await expect(page.locator('#sidebar .nav-item[data-view="stats"]')).toHaveClass(/active/);
  await page.goBack();
  await expect(page.locator('#view-torrents')).toHaveClass(/active/);
  await page.goForward();
  await expect(page.locator('#view-stats')).toHaveClass(/active/);

  await page.reload();
  await page.waitForFunction(() => document.readyState === 'complete');
  await expect(page).toHaveURL(/\/statistics$/);
  await expect(page.locator('#view-stats')).toHaveClass(/active/);
  await expect(page.locator('#page-title')).toHaveText('Statistics');

  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await expect(page).toHaveURL(/127\.0\.0\.1:\d+\/$/);
});

test('a bookmarked page address opens that page', async ({page}) => {
  for (const [path, view] of [['/settings', 'view-settings'], ['/activity', 'view-events'], ['/downloads', 'view-torrents']]) {
    await open(page, path);
    await expect(page.locator(`#${view}`)).toHaveClass(/active/);
    await expect(page).toHaveURL(new RegExp(`${path}$`));
  }
});

test('Escape closes Details and focus returns to the invoking row, even one redrawn meanwhile', async ({page}) => {
  await page.route(url => /^\/api\/torrents\/7$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(transfer)}));
  await open(page, '/');
  await page.evaluate(() => {
    const table = document.createElement('table');
    table.id = 'ws11-rows';
    table.innerHTML = '<tbody><tr data-torrent-id="7" tabindex="0"><td>Transfer 7</td></tr></tbody>';
    document.body.append(table);
    table.querySelector('tr').focus();
  });
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#overlay')).toHaveClass(/open/);
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(page.locator('#overlay')).not.toHaveClass(/open/);
  await expect.poll(() => page.evaluate(() => document.activeElement?.closest('#ws11-rows') !== null)).toBe(true);

  // Opened again, then the list redrawn while it is open: focus goes to the new row.
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
  await page.evaluate(() => {
    const body = document.querySelector('#ws11-rows tbody');
    body.innerHTML = '<tr data-torrent-id="7" tabindex="0" data-redrawn=""><td>Transfer 7</td></tr>';
  });
  await page.keyboard.press('Escape');
  await expect(page.locator('#overlay')).not.toHaveClass(/open/);
  await expect.poll(() => page.evaluate(() => document.activeElement?.hasAttribute('data-redrawn'))).toBe(true);
});

test('Escape inside a menu above Details closes the menu, not Details', async ({page}) => {
  await page.route(url => /^\/api\/torrents\/7$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(transfer)}));
  await open(page, '/');
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
  // Any owner that takes Escape first (preventDefault) keeps Details open.
  await page.evaluate(() => {
    const menu = document.createElement('button');
    menu.id = 'ws11-menu';
    menu.addEventListener('keydown', event => { if (event.key === 'Escape') event.preventDefault(); });
    document.getElementById('modal-body').append(menu);
    menu.focus();
  });
  await page.keyboard.press('Escape');
  await expect(page.locator('#overlay')).toHaveClass(/open/);
});
