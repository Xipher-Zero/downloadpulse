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
 *   - after it is dismissed the badge stays and opens it again: an overlay
 *     over the page surface's bottom edge, right-aligned with the version
 *     below it, also in en-XA, and not shown on a handheld. */

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

test('U05: never over the setup wizard (mocked unfinished setup); it opens once setup finishes', async ({page}) => {
  // The setup resumes at its last page (a labelled mock of the server's record), whose Finish is mocked too.
  await page.route(url => url.pathname === '/api/setup/state', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({initial: {complete: false, phase: 'getting_started', administrator: 'created'}, rerun: null})}));
  await page.route(url => url.pathname === '/api/setup/initial/finish', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({initial: {complete: true}})}));
  let read = 0;
  await page.route(url => url.pathname === '/api/version/check', route => {
    read += 1;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(record())});
  });
  await load(page);
  const wizard = page.locator('.dp-modal-dialog.dp-setup-dialog');
  await expect(wizard).toBeVisible();
  await expect.poll(() => read).toBeGreaterThan(0);
  await page.waitForTimeout(500);
  await expect(notice(page)).toHaveCount(0);
  expect(await page.locator('.dp-modal-dialog').count()).toBe(1);
  // Setup finishes: the wizard closes, and the notice opens after it.
  await wizard.getByRole('button', {name: await t(page, 'setup.action.finish')}).click();
  await expect(wizard).toHaveCount(0);
  await expect(notice(page)).toBeVisible();
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

test('U09: the badge floats over the page surface\'s bottom edge, right-aligned with the version above it', async ({page}) => {
  await answer(page, record());
  await load(page, {width: 1440});
  await expect(notice(page)).toBeVisible();
  await notice(page).locator('[data-modal-cancel]').click();
  await expect(notice(page)).toHaveCount(0);
  await expect(badge(page)).toBeVisible();
  const place = () => page.evaluate(() => {
    // On a page taller than the window it is seen at the page's end, as the version is.
    document.getElementById('update-badge-group').scrollIntoView({block: 'nearest'});
    const box = document.getElementById('update-badge').getBoundingClientRect();
    const version = document.getElementById('sidebar-version').getBoundingClientRect();
    const surface = document.querySelector('#content > .view.active').getBoundingClientRect();
    return {fromVersionRight: version.right - box.right, aboveVersion: version.top - box.bottom,
      insideRight: surface.right - box.right, insideBottom: surface.bottom - box.bottom,
      inTopbar: !!document.getElementById('update-badge').closest('#topbar'),
      // Nothing on the page paints over it: the badge and its x are what lies at their own centres.
      onTop: [document.getElementById('update-badge'), document.getElementById('update-badge-dismiss')].every(item => {
        const r = item.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return Boolean(hit) && item.contains(hit);
      }),
      left: box.left, right: box.right, viewport: window.innerWidth,
      overflow: document.scrollingElement.scrollWidth - window.innerWidth};
  });
  const first = await place();
  expect(first.inTopbar).toBe(false);
  // Its right edge on the version's (and so the surface's) right edge, just inside the surface's bottom edge.
  expect(Math.abs(first.fromVersionRight)).toBeLessThan(1);
  expect(Math.abs(first.insideRight)).toBeLessThan(1);
  expect(first.insideBottom).toBeGreaterThan(0);
  expect(first.insideBottom).toBeLessThan(24);
  expect(first.aboveVersion).toBeGreaterThan(0);
  // One place relative to the version, whatever page is shown and however far it is scrolled.
  for (const view of ['torrents', 'stats', 'events', 'help', 'settings', 'dashboard']) {
    await page.locator(`#sidebar .nav-item[data-view="${view}"]`).click();
    await expect(page.locator(`#view-${view}`)).toHaveClass(/\bactive\b/);
    const shown = await place();
    expect(shown.onTop).toBe(true);
    expect(Math.abs(shown.fromVersionRight - first.fromVersionRight)).toBeLessThan(1);
    expect(Math.abs(shown.aboveVersion - first.aboveVersion)).toBeLessThan(1);
    expect(shown.insideBottom).toBeGreaterThan(0);
  }
  await page.locator('#content').evaluate(element => { element.scrollTop = element.scrollHeight; });
  const scrolled = await place();
  expect(Math.abs(scrolled.fromVersionRight - first.fromVersionRight)).toBeLessThan(1);
  expect(Math.abs(scrolled.aboveVersion - first.aboveVersion)).toBeLessThan(1);
  // The longer en-XA label still fits the window.
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  await expect(badge(page)).toContainText('⟦');
  const long = await place();
  expect(long.left).toBeGreaterThanOrEqual(0);
  expect(long.right).toBeLessThanOrEqual(long.viewport);
  expect(long.overflow).toBeLessThanOrEqual(0);
});

test('U09b: a handheld has no place for the badge: it is not shown below 780px', async ({page}) => {
  await answer(page, record());
  for (const [width, shown] of [[390, false], [779, false], [780, true]]) {
    await load(page, {width});
    if (await notice(page).count()) await notice(page).locator('[data-modal-cancel]').click();
    await expect(page.locator('#update-badge')).toBeAttached();
    if (shown) await expect(badge(page)).toBeVisible();
    else await expect(badge(page)).toBeHidden();
  }
});

test('U09c: its x hides the badge until the next update check, which shows it again', async ({page}) => {
  let checkedAt = 1;
  await page.route(url => url.pathname === '/api/version/check',
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(record({checked_at: checkedAt}))}));
  await page.clock.install();
  await load(page, {width: 1440});
  await expect(notice(page)).toBeVisible();
  await notice(page).locator('[data-modal-cancel]').click();
  await expect(notice(page)).toHaveCount(0);
  await expect(badge(page)).toBeVisible();
  const dismiss = page.locator('#update-badge-dismiss');
  await expect(dismiss).toHaveAttribute('aria-label', await t(page, 'update.badge.dismiss'));
  await dismiss.click();
  await expect(badge(page)).toBeHidden();
  await expect(notice(page)).toHaveCount(0);           // hiding the badge opens nothing
  // The same check's record keeps it hidden, a reload included.
  await page.reload();
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
  await expect(page.locator('#update-badge')).toBeAttached();
  await expect(badge(page)).toBeHidden();
  // The next check makes a newer record; the page's next read shows the badge again, unreloaded.
  checkedAt = 2;
  await page.clock.fastForward('30:01');
  await expect(badge(page)).toBeVisible();
});

test('U10: the desktop Quit prompt opens above an open notice', async ({page}) => {
  await answer(page, record());
  await load(page);
  await expect(notice(page)).toBeVisible();
  // The window was closed where no tray can open it again (the backend's desktop_quit_prompt).
  await page.evaluate(() => { window.downloadPulseDesktop = true; askDesktopQuit('nonce-1'); });
  const quit = page.locator('.dp-modal-dialog[role="alertdialog"]').filter({hasText: await t(page, 'desktop.quit.title')});
  await expect(quit).toBeVisible();
  const order = await page.evaluate(() => [...document.querySelectorAll('.dp-modal-overlay')]
    .map(overlay => overlay.querySelector('.dp-update-dialog') ? 'update' : 'other'));
  expect(order).toEqual(['update', 'other']);              // the prompt is the later, upper dialog
  await quit.locator('[data-modal-cancel]').click();
  await expect(quit).toHaveCount(0);
  await expect(notice(page)).toBeVisible();
});

test('U11: never over the page\'s own #modal shell (Details); it opens once that closes', async ({page}) => {
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route(url => url.pathname === '/api/version/check', async route => {
    await held;
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(record())});
  });
  await page.route(url => url.pathname === '/api/torrents/424242', route => route.fulfill({status: 404,
    contentType: 'application/json', body: JSON.stringify({detail: 'Transfer not found', code: 'transfer.not_found'})}));
  await load(page);
  await page.evaluate(() => { window.showDetail(424242); });
  await expect.poll(() => page.evaluate(() => window.DPModal.mode)).toBe('details');
  release();
  await page.waitForTimeout(500);
  await expect(notice(page)).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect.poll(() => page.evaluate(() => window.DPModal.mode)).toBe(null);
  await expect(notice(page)).toBeVisible();
});

test('U12: a download that waits on the person ends the follow; a paused one says paused', async ({page}) => {
  await answer(page, record(DESKTOP));
  const rows = [{status: 'paused', presentation_status: 'paused', progress: 10},
                {status: 'requires_attention', presentation_status: 'requires_attention', progress: 10,
                 presentation_error_category: 'source_not_found'}];
  let reads = 0;
  await page.route(url => url.pathname === '/api/desktop/update/download', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({id: 78, status: 'pending'})}));
  await page.route(url => url.pathname === '/api/torrents/78', route => {
    reads += 1;
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({id: 78, ...(rows.length > 1 ? rows.shift() : rows[0])})});
  });
  await load(page);
  await notice(page).getByRole('button', {name: await t(page, 'update.download')}).click();
  await expect(notice(page).locator('.dp-update-status')).toHaveText(await t(page, 'update.paused'));
  const reason = await page.evaluate(() => DPI18n.has('error_category.source_not_found')
    ? DPI18n.t('error_category.source_not_found') : '');
  await expect(notice(page)).toContainText(await t(page, 'update.download_failed'), {timeout: 10000});
  if (reason) await expect(notice(page).locator('.dp-update-status')).toHaveText(reason);
  await expect(notice(page).getByRole('button', {name: await t(page, 'update.download')})).toBeHidden();
  const settled = reads;
  await page.waitForTimeout(5000);
  expect(reads).toBe(settled);                            // nothing more is polled
});

test('U13: a newer release that arrives while the notice is open is shown once it closes', async ({page}) => {
  let latest = '9.9.9';
  await page.route(url => url.pathname === '/api/version/check', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify(record({latest}))}));
  await load(page);
  await expect(notice(page).locator('.dp-modal-title')).toHaveText(await t(page, 'update.title', {version: '9.9.9'}));
  latest = '9.9.10';
  await page.evaluate(() => window.DPUpdateNotice.read());   // what an update_status signal does
  await notice(page).locator('[data-modal-cancel]').click();
  await expect(notice(page).locator('.dp-modal-title')).toHaveText(await t(page, 'update.title', {version: '9.9.10'}));
});
