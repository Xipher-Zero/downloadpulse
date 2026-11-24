const { test, expect } = require('@playwright/test');

/* DP 1.0.3 WS25 -- Details' Escape order with the real owners: the one
 * application tooltip (ui-tooltip.js) and the one application menu (app.js,
 * the Original Resource's Copy URL). Each Escape is taken by the newest thing
 * open above Details -- a shown tooltip, an open menu, and the tooltip its
 * returned focus says again; only an Escape nobody else took closes Details. */

const FULL = 'https://files.example/a-release-directory/Release.Name.2026.part01.rar';
const detail = id => ({
  id, name: 'Release', status: 'downloading', progress: 40, size_bytes: 1024, source: 'direct_link',
  created_at: '2026-01-01T00:00:00Z', completed_at: null, hash: '', files: [], source_outcomes: [],
  events: [], executors: ['aria2'], route_attempts: [], execution_attempts: [],
  original_resource: FULL, original_resource_sanitized_full: FULL, original_resource_copy_allowed: true,
});

async function boot(page) {
  const item = detail(7);
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => /^\/api\/torrents\/\d+$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(item)}));
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: [item], total: 1})}));
  await page.goto('/');
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
}
const value = page => page.locator('.dp-detail-resource');
const tip = page => page.locator('#dp-tooltip');
const menu = page => page.locator('.dp-row-menu');
const overlayOpen = page => page.locator('#overlay.open');

test('Escape dismisses a tooltip shown in Details, then closes an open menu, and only then closes Details', async ({page}) => {
  await boot(page);

  // A tooltip shown inside Details: this Escape is the tooltip's alone.
  await value(page).hover();
  await expect(tip(page)).toBeVisible();
  await expect(tip(page)).toHaveText(FULL);
  await page.keyboard.press('Escape');
  await expect(tip(page)).toBeHidden();
  await expect(overlayOpen(page)).toHaveCount(1);
  expect(await page.evaluate(() => DPModal.mode)).toBe('details');

  // The Original Resource menu open: this Escape closes the menu alone.
  await value(page).click({button: 'right'});
  await expect(menu(page)).toHaveCount(1);
  await expect(menu(page).locator('[role="menuitem"]')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(menu(page)).toHaveCount(0);
  await expect(overlayOpen(page)).toHaveCount(1);
  expect(await page.evaluate(() => DPModal.mode)).toBe('details');

  // Focus is back on the value, which says its tooltip again: that Escape is
  // the tooltip's alone, as before.
  await expect(value(page)).toBeFocused();
  await expect(tip(page)).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(tip(page)).toBeHidden();
  await expect(overlayOpen(page)).toHaveCount(1);
  expect(await page.evaluate(() => DPModal.mode)).toBe('details');

  // Nothing else is open above Details: the next Escape closes it.
  await page.keyboard.press('Escape');
  await expect(overlayOpen(page)).toHaveCount(0);
  expect(await page.evaluate(() => DPModal.mode)).toBe(null);
});
