const { test, expect } = require('@playwright/test');

/* WS27 update notice (app.js DPUpdateNotice). The backend's one release
 * owner keeps the latest check's record; the page reads it from
 * /api/version/check. These cases answer that read (a labelled mock of the
 * one server fact: whether a newer release exists, and whether this window
 * may download its installer), so the notice's own behaviour is what they
 * prove:
 *
 *   - it opens once per browser session, and again in a new one;
 *   - it never opens over another dialog (the setup wizard, browser pairing),
 *     and waits for the last one to close;
 *   - a server has no download, only Close and the release notes;
 *   - the desktop downloads through DownloadPulse, follows the transfer's row
 *     and then offers Show in folder;
 *   - after it is dismissed the topbar badge stays and opens it again, and
 *     fits at a phone's width and in en-XA. */

const RELEASE = 'https://github.com/Xipher-Zero/downloadpulse/releases/tag/v9.9.9';
const record = (extra = {}) => ({
  checked: true, checked_at: 1, current: '1.0.3', latest: '9.9.9', update_available: true,
  release_url: RELEASE, published_at: '2026-12-01', installer: null, download_offered: false, ...extra,
});
const DESKTOP = {installer: {name: 'DownloadPulse-9.9.9-linux-x86_64.flatpak', size: 1234}, download_offered: true};

async function answer(page, body) {
  await page.route(url => url.pathname === '/api/version/check',
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)}));
}

async function load(page, {width = 1280, height = 900, path = '/'} = {}) {
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.setViewportSize({width, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto(path);
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

const notice = page => page.locator('.dp-modal-dialog.dp-update-dialog');
const badge = page => page.locator('#update-badge');
const t = (page, key, values) => page.evaluate(([key, values]) => window.DPI18n.t(key, values), [key, values]);

test('U01: the notice opens once per browser session and again in a new one', async ({page, context}) => {
  await answer(page, record());
  await load(page);
  await expect(notice(page)).toBeVisible();
  await expect(notice(page).locator('.dp-modal-title')).toHaveText(await t(page, 'update.title', {version: '9.9.9'}));
  await expect(notice(page)).toContainText(await t(page, 'update.body', {current: '1.0.3'}));
  await notice(page).getByRole('button', {name: await t(page, 'dialog.close')}).click();
  await expect(notice(page)).toHaveCount(0);
  // The same tab session: not again, however often the page loads.
  await page.reload();
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await expect(badge(page)).toBeVisible();
  await expect(notice(page)).toHaveCount(0);
  // A new browser session (a new tab): once more.
  const fresh = await context.newPage();
  await answer(fresh, record());
  await load(fresh);
  await expect(notice(fresh)).toBeVisible();
  await fresh.close();
});

test('U02: a server only says so -- Close and the release notes, no download', async ({page}) => {
  await answer(page, record());
  await load(page);
  await expect(notice(page)).toBeVisible();
  const buttons = notice(page).locator('.dp-modal-footer button:visible');
  await expect(buttons).toHaveCount(1);
  await expect(buttons).toHaveText(await t(page, 'dialog.close'));
  await expect(notice(page).getByRole('button', {name: await t(page, 'update.download')})).toHaveCount(0);
  const link = notice(page).getByRole('link', {name: await t(page, 'update.release_notes')});
  await expect(link).toHaveAttribute('href', RELEASE);
  await expect(link).toHaveAttribute('rel', /noopener/);
});

test('U03: after Close the badge stays and opens the notice again', async ({page}) => {
  await answer(page, record());
  await load(page);
  await notice(page).getByRole('button', {name: await t(page, 'dialog.close')}).click();
  await expect(notice(page)).toHaveCount(0);
  await expect(badge(page)).toBeVisible();
  await expect(badge(page)).toHaveText(await t(page, 'update.badge.label', {version: '9.9.9'}));
  await expect(badge(page)).toHaveAttribute('aria-label', await t(page, 'update.badge.aria', {version: '9.9.9'}));
  await badge(page).click();
  await expect(notice(page)).toBeVisible();
});

test('U04: no notice and no badge when nothing newer exists or nothing was checked', async ({page}) => {
  await answer(page, {checked: false});
  await load(page);
  await page.waitForTimeout(500);
  await expect(notice(page)).toHaveCount(0);
  await expect(badge(page)).toBeHidden();
});

test('U05: never over the setup wizard (mocked unfinished setup)', async ({page}) => {
  await page.route(url => url.pathname === '/api/setup/state', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({initial: {complete: false, phase: 'welcome', administrator: 'none'}, rerun: null})}));
  let read = 0;
  await page.route(url => url.pathname === '/api/version/check', route => {
    read += 1;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(record())});
  });
  await load(page);
  await expect(page.locator('.dp-modal-dialog.dp-setup-dialog')).toBeVisible();
  await expect.poll(() => read).toBeGreaterThan(0);
  await page.waitForTimeout(500);
  await expect(notice(page)).toHaveCount(0);
  expect(await page.locator('.dp-modal-dialog').count()).toBe(1);
});

test('U06: never over browser pairing; it opens once that dialog closes', async ({page}) => {
  // The check answers only once the pairing dialog shows, so the order is the case's own.
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route(url => url.pathname === '/api/version/check', async route => {
    await held;
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(record())});
  });
  await load(page, {path: '/?connect-browser=NOPE-0000'});
  const pairing = page.locator('.dp-modal-dialog.dp-browser-pairing-dialog');
  await expect(pairing).toBeVisible();
  release();
  await page.waitForTimeout(500);
  await expect(notice(page)).toHaveCount(0);
  await pairing.locator('[data-modal-cancel]').click();
  await expect(pairing).toHaveCount(0);
  await expect(notice(page)).toBeVisible();
});

test('U07: the desktop downloads through DownloadPulse, follows the row, then offers Show in folder', async ({page}) => {
  await answer(page, record(DESKTOP));
  const rows = [{status: 'downloading', presentation_status: 'downloading', progress: 42},
                {status: 'completed', presentation_status: 'completed', progress: 100}];
  const posted = [];
  await page.route(url => url.pathname === '/api/desktop/update/download', route => {
    posted.push(route.request().postData());
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({id: 77, status: 'pending'})});
  });
  await page.route(url => url.pathname === '/api/torrents/77', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({id: 77, ...(rows.length > 1 ? rows.shift() : rows[0])})}));
  const revealed = [];
  await page.route(url => url.pathname === '/api/desktop/reveal', route => {
    revealed.push(JSON.parse(route.request().postData()));
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({revealing: true})});
  });
  await load(page);
  await expect(notice(page)).toBeVisible();
  await expect(notice(page).getByRole('button', {name: await t(page, 'update.not_now')})).toBeVisible();
  await notice(page).getByRole('button', {name: await t(page, 'update.download')}).click();
  // The page names nothing: no address, no hash.
  await expect.poll(() => posted).toEqual([null]);
  await expect(notice(page).locator('.dp-update-status')).toHaveText(await t(page, 'update.downloading', {percent: 42}));
  await expect(notice(page).locator('.dp-update-status')).toHaveText(await t(page, 'update.downloaded'), {timeout: 10000});
  const show = notice(page).getByRole('button', {name: await t(page, 'transfer.action.show_in_folder')});
  await show.click();
  await expect.poll(() => revealed).toEqual([{transfer_id: 77}]);
  await expect(notice(page)).toHaveCount(0);
});

test('U08: a refused download says why and points at the release notes', async ({page}) => {
  await answer(page, record(DESKTOP));
  await page.route(url => url.pathname === '/api/desktop/update/download', route => route.fulfill({status: 403,
    contentType: 'application/json', body: JSON.stringify({detail: 'Updates are downloaded only in the DownloadPulse window.',
      code: 'desktop.update_window_only', params: {}})}));
  await load(page);
  await notice(page).getByRole('button', {name: await t(page, 'update.download')}).click();
  await expect(notice(page).locator('.dp-update-status')).toHaveText(await t(page, 'error.desktop.update_window_only'));
  await expect(notice(page)).toContainText(await t(page, 'update.download_failed'));
  await expect(notice(page).getByRole('button', {name: await t(page, 'update.download')})).toBeHidden();
});

test('U09: the badge fits at a phone\'s width and in en-XA', async ({page}) => {
  await answer(page, record());
  for (const width of [390, 1180]) {
    await load(page, {width});
    if (await notice(page).count()) await notice(page).locator('[data-modal-cancel]').click();
    for (const locale of ['en', 'en-XA']) {
      expect(await page.evaluate(locale => window.DPI18n.setLocale(locale), locale)).toBe(locale);
      await expect(badge(page)).toBeVisible();
      if (locale === 'en-XA') await expect(badge(page)).toContainText('⟦');
      const fit = await badge(page).evaluate(element => {
        const box = element.getBoundingClientRect();
        return {left: box.left, right: box.right, width: box.width, viewport: window.innerWidth,
          overflow: document.scrollingElement.scrollWidth - window.innerWidth};
      });
      expect(fit.width).toBeGreaterThan(0);
      expect(fit.left).toBeGreaterThanOrEqual(0);
      expect(fit.right).toBeLessThanOrEqual(fit.viewport);
      expect(fit.overflow).toBeLessThanOrEqual(0);
    }
  }
});
