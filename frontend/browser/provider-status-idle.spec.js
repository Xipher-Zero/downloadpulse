// @ts-check
/* Provider Status's Idle state (1.0.5 WS9): a provider whose service is
 * stopped while it has nothing to do, started when work needs it. Its own
 * blue dot and words; as ready as healthy (in a group, and when its
 * configuration is unproven -- a warning alike). Rendered from explicit
 * canonical integrations (settingsData), as provider-status-hierarchy does. */
const {test, expect} = require('@playwright/test');

const usenet = (status, extra = {}) => ({
  enabled: true, priority: 0, name: 'Usenet', kind: 'provider_executor', configured: true, ...extra,
  presentation: {status_name: 'Usenet', premium: true, status_endpoint: null, static_status: status, display_order: 900,
    status_group: null, status_group_label: null, status_tier: 'premium_service', status_tier_label: 'Premium Services'},
  options: {servers: [{id: 'a', host: 'news.example.com'}]},
});
const member = (id, name, status) => ({
  enabled: true, priority: 0, name, kind: 'provider', configured: true,
  presentation: {status_name: name, premium: false, status_endpoint: null, static_status: status, display_order: 910,
    status_group: 'direct_sources', status_group_label: 'Network Sources', status_tier: 'general_family',
    status_tier_label: 'Standard Services'},
  options: {},
});

async function render(page, integrations) {
  await page.evaluate(next => { settingsData = {...(settingsData || {}), integrations: next}; }, integrations);
  await page.evaluate(() => window.DPProviderStatus.refresh());
}
const row = (page, name) => page.locator('#provider-status-list .conn-row', {hasText: name}).first();

test.beforeEach(async ({page}) => {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await expect(page.locator('#provider-status-list')).toBeVisible();
});

test('Idle is its own blue state, said as ready and starting when needed', async ({page}) => {
  await render(page, {usenet: usenet('idle')});
  const usenetRow = row(page, 'Usenet');
  await expect(usenetRow).toHaveAttribute('data-provider-state', 'idle');
  const dot = usenetRow.locator('.dot');
  await expect(dot).toHaveClass(/\bidle\b/);
  await expect(dot).toHaveAttribute('aria-label', 'Idle (starts when needed)');
  const colours = await page.evaluate(() => {
    const dot = document.querySelector('#provider-status-list .conn-row[data-provider-state="idle"] .dot');
    const probe = document.createElement('div');
    probe.style.color = 'var(--blue)';
    document.body.appendChild(probe);
    const blue = getComputedStyle(probe).color;
    probe.remove();
    return {dot: getComputedStyle(dot).backgroundColor, blue};
  });
  expect(colours.dot).toBe(colours.blue);
});

test('an unproven configuration warns while idle, as it does while healthy', async ({page}) => {
  await render(page, {usenet: usenet('idle', {verification_applicable: true, verified: false})});
  await expect(row(page, 'Usenet')).toHaveAttribute('data-provider-state', 'unverified');
  await render(page, {usenet: usenet('idle', {verification_applicable: true, verified: true})});
  await expect(row(page, 'Usenet')).toHaveAttribute('data-provider-state', 'idle');
});

test('a group is Idle only when all its members are; with healthy ones it is healthy', async ({page}) => {
  await render(page, {http: member('http', 'HTTP(S)', 'idle'), ftp: member('ftp', '(S)FTP', 'idle')});
  await expect(row(page, 'Network Sources')).toHaveAttribute('data-provider-state', 'idle');
  await render(page, {http: member('http', 'HTTP(S)', 'idle'), ftp: member('ftp', '(S)FTP', 'healthy')});
  await expect(row(page, 'Network Sources')).toHaveAttribute('data-provider-state', 'healthy');
  await render(page, {http: member('http', 'HTTP(S)', 'idle'), ftp: member('ftp', '(S)FTP', 'unhealthy')});
  await expect(row(page, 'Network Sources')).toHaveAttribute('data-provider-state', 'unhealthy');
});
