const { test, expect } = require('@playwright/test');

/* WS-A3: the interface under its Content-Security-Policy (backend/main.py
 * APPLICATION_CONTENT_SECURITY_POLICY: script-src 'self', no inline handler or
 * script). The page's own responses carry it; where the server under test
 * predates it, the same policy is applied to them here, so this proves the
 * interface itself: an injected inline handler never runs, and every main
 * page, its navigation and its controls (wired through ui-actions.js) work
 * with no violation reported. The browser enforces the policy: bypassCSP is
 * never set. */

const POLICY = "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'";

async function underPolicy(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  // The page's own documents (never its live stream, which a fetch here would hold).
  await page.route(url => url.origin === new URL(process.env.DP_BASE_URL || 'http://127.0.0.1:8095').origin, async route => {
    if (route.request().resourceType() !== 'document') return route.continue();
    const response = await route.fetch();
    const headers = response.headers();
    if (!headers['content-security-policy']) headers['content-security-policy'] = POLICY;
    await route.fulfill({response, headers});
  });
  // Every violation the page reports, from before its first script.
  await page.addInitScript(() => {
    window.__dpViolations = [];
    document.addEventListener('securitypolicyviolation', event => window.__dpViolations.push(
      {directive: event.effectiveDirective, blocked: event.blockedURI, sample: event.sample}));
  });
}

async function open(page, path) {
  const response = await page.goto(path);
  expect(response.headers()['content-security-policy']).toBe(POLICY);
  await page.waitForFunction(() => typeof window.showDetail === 'function' && document.readyState === 'complete');
}

const violations = page => page.evaluate(() => window.__dpViolations);

test('the policy is enforced: an injected inline handler never runs and is reported', async ({page}) => {
  await underPolicy(page);
  await open(page, '/');
  const outcome = await page.evaluate(() => new Promise(resolve => {
    window.__dpInjected = false;
    const holder = document.createElement('div');
    holder.innerHTML = '<img src="/no-such-image.png" onerror="window.__dpInjected = true">';
    const image = holder.querySelector('img');
    // The error event itself fires; only the attribute's script is refused.
    image.addEventListener('error', () => setTimeout(() => resolve(window.__dpInjected), 50));
    document.body.appendChild(holder);
  }));
  expect(outcome).toBe(false);
  expect((await violations(page)).map(item => item.directive)).toContain('script-src-attr');
});

test('every main page, its navigation and its controls run with no violation', async ({page}) => {
  await underPolicy(page);
  for (const path of ['/', '/downloads', '/activity', '/statistics', '/settings', '/help']) {
    await open(page, path);
  }
  await open(page, '/');
  // Navigation through the sidebar's declared actions (app.js's shell table).
  for (const [view, address] of [['torrents', /\/downloads$/], ['events', /\/activity$/], ['stats', /\/statistics$/],
    ['help', /\/help$/], ['dashboard', /\/$/]]) {
    await page.locator(`#sidebar .nav-item[data-view="${view}"]`).click();
    await expect(page).toHaveURL(address);
    await expect(page.locator(`#view-${view}`)).toHaveClass(/active/);
  }
  // A Downloads filter (ui-downloads.js's toolbar table) and a Statistics period (the shell's).
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await page.locator('#view-torrents .ftab[data-dp-status="paused"]').click();
  await expect(page.locator('#view-torrents .ftab[data-dp-status="paused"]')).toHaveClass(/active/);
  await page.locator('#sidebar .nav-item[data-view="stats"]').click();
  await page.locator('#stats-period-tabs .ftab[data-period="24h"]').click();
  await expect(page.locator('#stats-period-tabs .ftab[data-period="24h"]')).toHaveClass(/active/);
  // The speed cap menu opens from its toggle and closes from outside it.
  await page.locator('#runtime-cap-toggle').click();
  await expect(page.locator('#runtime-cap-toggle')).toHaveAttribute('aria-expanded', 'true');
  await page.locator('#topbar .dp-page-heading').click();
  await expect(page.locator('#runtime-cap-toggle')).toHaveAttribute('aria-expanded', 'false');
  expect(await violations(page)).toEqual([]);
});

test('an unknown declared action is ignored and never resolved from a global', async ({page}) => {
  await underPolicy(page);
  await open(page, '/');
  const warnings = [];
  page.on('console', message => { if (message.type() === 'warning') warnings.push(message.text()); });
  const ran = await page.evaluate(() => {
    window.__dpGadget = () => { window.__dpGadgetRan = true; };
    const button = document.createElement('button');
    button.setAttribute('data-dp-click', '__dpGadget');
    document.body.appendChild(button);
    button.click();
    button.remove();
    return window.__dpGadgetRan === true;
  });
  expect(ran).toBe(false);
  await expect.poll(() => warnings.some(text => text.includes('__dpGadget'))).toBe(true);
  expect(await violations(page)).toEqual([]);
});
