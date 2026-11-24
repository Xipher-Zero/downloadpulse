const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

function observeRuntime(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
  page.on('console', message => { if (message.type() === 'error') errors.push(`console: ${message.text()}`); });
  return errors;
}

const states = [
  ['downloading', 'Downloading', 'downloading'],
  ['paused', 'Paused', 'paused'],
  ['waiting_for_retry', 'Waiting for retry', 'queued'],
  ['waiting_for_provider', 'Waiting for provider', 'pending'],
  ['waiting_for_storage', 'Waiting for storage', 'pending'],
  ['recovering', 'Recovering', 'processing'],
  ['input_required', 'Input Required', 'input_required'],
  ['requires_attention', 'Requires attention', 'error'],
];

function rawStatus(state) {
  if (state === 'downloading') return 'downloading';
  if (state === 'paused') return 'paused';
  if (state === 'input_required') return 'input_required';
  return 'error';
}

function item(id, state, label, badge) {
  return {
    id, name: `Phase4 ${state}`, status: rawStatus(state),
    presentation_status: state, presentation_label: label, presentation_badge_status: badge,
    attention_required: state === 'requires_attention', progress: 50, retained_bytes: 512,
    size_bytes: 1024, source: 'manual', hash: '', label: '', created_at: '2026-09-08T00:00:00Z',
    current_source_identity: {kind: 'link'}, providers: [], historical_providers: [], delivering_provider_ids: [],
    input_required: state === 'input_required' ? {
      id:'phase4-input', generation:1, reason:'auth_required', origin:'provider',
      methods:[{method:'username_password',fields:[{name:'username',required:true},{name:'password',required:true}]}],
    } : null,
  };
}

test('Phase-4 canonical recovery states render from backend truth with retained progress', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const items = states.map(([state,label,badge], index) => item(940 + index, state, label, badge));
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({items, total: items.length}),
  }));
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await loadTorrents();
  });
  for (let index = 0; index < states.length; index += 1) {
    const [state, label] = states[index];
    const row = page.locator(`#t-tbody tr[data-torrent-id="${940 + index}"]`);
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute('data-presentation-status', state);
    const status = row.locator('[data-role="transfer-status"]');
    await expect(status).toContainText(label);
    await expect(status.locator(`[data-dp-lifecycle-status="${state}"]`)).toHaveCount(1);
    await expect(row.locator('[data-role="transfer-progress"]')).toContainText('50');
    if (state !== 'requires_attention') {
      await expect(status.locator('.badge-error')).toHaveCount(0);
      await expect(row.locator('.dp-terminal-error-progress')).toHaveCount(0);
    }
    if (['recovering','waiting_for_retry','waiting_for_provider','waiting_for_storage'].includes(state)) {
      await expect(row.locator('button[data-default-label="Retry"]')).toHaveCount(0);
    }
  }
  expect(errors).toEqual([]);
});

test('Recent Activity keeps retained progress visible while retry is quiescent', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const waiting = item(980, 'waiting_for_retry', 'Waiting for retry', 'queued');
  waiting.progress = 61;
  waiting.retained_bytes = 610;
  waiting.size_bytes = 1000;
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({items:[waiting],total:1}),
  }));
  await page.goto('/');
  await page.evaluate(async () => { await loadRecent(); });
  const row = page.locator('#dash-tbody tr[data-torrent-id="980"]');
  await expect(row.locator('[data-role="transfer-status"]')).toContainText('Waiting for retry');
  await expect(row.locator('[data-role="transfer-progress"]')).toContainText('61');
  await expect(row.locator('[data-role="transfer-progress"] .dp-terminal-error-progress')).toHaveCount(0);
  await expect(row.locator('.dash-row-bar')).not.toHaveClass(/\bis-empty\b/);
  await expect(row.locator('.dash-row-bar-fill')).toHaveAttribute('style', /width:61%/);
  expect(errors).toEqual([]);
});

test('Details uses canonical artifact state and does not turn recovery into attention', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const transfer = item(990, 'recovering', 'Recovering', 'processing');
  transfer.files = [{
    id: 1, filename: 'payload.bin', size_bytes: 1024, status: 'error', progress: 50,
    retained_bytes: 512, presentation_status: 'waiting_for_retry', presentation_label: 'Waiting for retry',
    presentation_badge_status: 'queued', attention_required: false, blocked: false, block_reason: null,
  }];
  transfer.route_attempts = []; transfer.execution_attempts = []; transfer.candidate_bindings = []; transfer.source_outcomes = []; transfer.events = [];
  await page.route(url => url.pathname === '/api/torrents/990', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(transfer),
  }));
  await page.goto('/');
  await page.evaluate(async () => { await showDetail(990); });
  await expect(page.locator('#modal-body')).toContainText('Recovering');
  await expect(page.locator('#modal-body')).toContainText('Waiting for retry');
  await expect(page.locator('#modal-body [data-dp-lifecycle-status="requires_attention"]')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('A row names the failure its status is about, with the real next attempt, and nothing without one', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await page.setViewportSize({width: 1600, height: 900});
  const retryAt = Date.UTC(2031, 0, 2, 3, 4) / 1000;
  const failed = Object.assign(item(1001, 'failed', 'Failed', 'error'), {presentation_error_category: 'rate_limited', presentation_retry_at: null});
  const attention = Object.assign(item(1002, 'requires_attention', 'Requires attention', 'error'), {presentation_error_category: 'connection_refused', presentation_retry_at: null});
  const waiting = Object.assign(item(1003, 'waiting_for_retry', 'Waiting for retry', 'queued'), {presentation_error_category: 'rate_limited', presentation_retry_at: retryAt});
  // No category, or one the catalog does not know: nothing is added, never a substituted one.
  const plain = Object.assign(item(1004, 'waiting_for_retry', 'Waiting for retry', 'queued'), {presentation_error_category: null, presentation_retry_at: retryAt});
  const unknown = Object.assign(item(1005, 'requires_attention', 'Requires attention', 'error'), {presentation_error_category: 'not_a_category', presentation_retry_at: null});
  const items = [failed, attention, waiting, plain, unknown];
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({items, total: items.length}),
  }));
  await page.goto('/');
  await page.evaluate(async () => {
    await window.DPI18n.ready;
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await loadTorrents();
  });
  const status = id => page.locator(`#t-tbody tr[data-torrent-id="${id}"] [data-role="transfer-status"]`);
  const reason = id => status(id).locator('[data-dp-status-reason]');
  const when = await page.evaluate(at => window.downloadsDateLabel(new Date(at * 1000).toISOString()), retryAt);
  await expect(reason(1001)).toHaveText('Rate Limited');
  await expect(status(1001).locator('[data-dp-failure-code]')).toHaveAttribute('data-dp-failure-code', 'rate_limited');
  await expect(status(1001).locator('.badge')).toHaveAttribute('data-tooltip', 'Failed · Rate Limited');
  await expect(reason(1002)).toHaveText('Connection Refused');
  await expect(reason(1003)).toHaveText(`Rate Limited · next attempt: ${when}`);
  for (const id of [1004, 1005]) {
    await expect(reason(id)).toHaveCount(0);
    await expect(status(id).locator('[data-dp-failure-code]')).toHaveCount(0);
  }
  await expect(status(1005)).not.toContainText('Internal');
  // Dashboard Recent Activity draws the same status through the same owner.
  await page.evaluate(async () => { await loadRecent(); });
  await expect(page.locator('#dash-tbody tr[data-torrent-id="1003"] [data-role="transfer-status"] [data-dp-status-reason]'))
    .toHaveText(`Rate Limited · next attempt: ${when}`);
  // A reason that fits shows whole, so its tooltip would only repeat it: hovering it shows none.
  expect(await reason(1001).evaluate(line => line.scrollWidth <= line.clientWidth)).toBe(true);
  await reason(1001).hover();
  await expect(page.locator('#dp-tooltip.is-shown')).toHaveCount(0);
  // The expanded pseudo locale: the reason stays one line inside the status column, never
  // widening it or growing the row; the words that do not fit end in an ellipsis, and the
  // reason's own tooltip says them whole.
  const rowHeight = () => page.locator('#t-tbody tr[data-torrent-id="1003"]').evaluate(row => row.getBoundingClientRect().height);
  const before = await rowHeight();
  await page.evaluate(() => window.DPI18n.setLocale('en-XA'));
  await page.evaluate(async () => { await loadTorrents(); });
  await expect(reason(1003)).toContainText('⟦');
  const fit = await status(1003).evaluate(cell => {
    const line = cell.querySelector('[data-dp-status-reason]');
    const style = getComputedStyle(line);
    return {cell: cell.getBoundingClientRect(), line: line.getBoundingClientRect(), overflow: cell.scrollWidth - cell.clientWidth,
            lineHeight: parseFloat(style.lineHeight), ellipsis: style.textOverflow, cut: line.scrollWidth > line.clientWidth};
  });
  expect(fit.overflow).toBeLessThanOrEqual(0);
  expect(fit.line.left).toBeGreaterThanOrEqual(fit.cell.left - 0.5);
  expect(fit.line.right).toBeLessThanOrEqual(fit.cell.right + 0.5);
  expect(fit.line.height).toBeLessThanOrEqual(fit.lineHeight + 0.5);
  expect(fit.ellipsis).toBe('ellipsis');
  expect(fit.cut).toBe(true);
  expect(await rowHeight()).toBeLessThanOrEqual(before + 0.5);
  await expect(reason(1003)).toHaveAttribute('data-tooltip', await reason(1003).textContent());
  await reason(1003).hover();
  await expect(page.locator('#dp-tooltip.is-shown')).toHaveText(await reason(1003).textContent());
  expect(errors).toEqual([]);
});
