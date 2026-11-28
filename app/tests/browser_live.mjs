// Run against an isolated, signed-in application using a Playwright page.
export async function runLiveRegressions(page) {
  const base = new URL(page.url()).origin;
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const preview = await page.evaluate(async () => (await fetch('/api/organize/preview')).json());
  assert(preview.preview && !preview.preview.stale, 'A fresh synthetic Organize preview is required');
  const path = '/api/plans/' + preview.preview.id;
  let refreshes = 0;
  const response = (res) => { if (new URL(res.url()).pathname === path) refreshes++; };
  page.on('response', response);
  try {
    await page.goto(base + '/#/plans/' + preview.preview.id);
    await page.locator('main .table-wrap').waitFor();
    await page.locator('main .table-wrap').evaluate((node) => { node.scrollTop = 200; });
    const scroll = await page.locator('main .table-wrap').evaluate((node) => node.scrollTop);
    assert(scroll > 0, 'Synthetic preview must contain enough rows to scroll');
    const filter = page.locator('main select');
    await filter.click();
    assert(await filter.evaluate((node) => node.matches(':open')), 'Native preview select did not open');
    const before = refreshes;
    await page.waitForTimeout(8300);
    assert(refreshes >= before + 2, 'At least two real preview refreshes are required');
    assert(await filter.evaluate((node) => node.matches(':open')), 'Polling closed the native select');
    assert(await page.locator('main .table-wrap').evaluate((node) => node.scrollTop) === scroll, 'Polling reset table scroll');
    await page.keyboard.press('Escape');
  } finally { page.off('response', response); }

  for (let n = 0; n < 30; n++) {
    if (n % 2 === 0) await page.reload({ waitUntil: 'domcontentloaded' });
    else {
      await page.goto(base + '/#/' + ['home', 'jobs', 'duplicates', 'metadata', 'report'][n % 5],
        { waitUntil: 'domcontentloaded' });
    }
    await page.locator('.mainnav').waitFor();
  }
  const timings = {};
  for (const path of ['/health', '/']) {
    const result = await page.evaluate(async (path) => {
      const started = performance.now();
      const response = await fetch(path, { signal: AbortSignal.timeout(2000) });
      await response.text();
      return { status: response.status, duration: performance.now() - started };
    }, path);
    assert(result.status === 200 && result.duration < 2000, path + ' must answer within two seconds after 30 reloads/navigations');
    timings[path] = result.duration;
  }
  await page.goto(base + '/#/jobs?status=completed');
  await page.locator('main select').first().waitFor();
  await page.reload();
  await page.locator('main select').first().waitFor();
  assert(await page.locator('main select').first().inputValue() === 'completed', 'F5 lost the Jobs URL filter');
  await page.locator('.mainnav a[href="#/jobs"]').click();
  await page.waitForFunction(() => document.querySelector('main select')?.value === 'open');
  return { previewRefreshes: refreshes, nativeSelectPreserved: true, reloads: 30, responseMilliseconds: timings, urlFilters: true };
}

export async function runRc13Regressions(page) {
  const base = new URL(page.url()).origin;
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const go = async (route) => {
    const url = base + '/' + route;
    if (page.url() === url) await page.reload();
    else await page.goto(url);
    await page.locator('main .filters').first().waitFor();
    await page.waitForFunction(() => document.activeElement === document.querySelector('main'));
  };
  const calls = [];
  const record = (request) => { if (request.url().includes('/api/')) calls.push(new URL(request.url())); };
  page.on('request', record);
  const last = (path) => calls.filter((url) => url.pathname === path).at(-1)?.searchParams;
  const waitQuery = async (path, key, value, action) => {
    const response = page.waitForResponse((res) => {
      const url = new URL(res.url());
      return url.pathname === path && url.searchParams.get(key) === value;
    });
    await action();
    await response;
  };
  const choose = async (trigger, value) => {
    await trigger.click();
    await page.locator('.combo-option').filter({ hasText: value }).first().click();
  };
  try {
    await go('#/jobs');
    await page.keyboard.press('Tab');
    assert(await page.evaluate(() => document.activeElement.matches('a, button, input, select') &&
      document.activeElement.matches(':focus-visible') && getComputedStyle(document.activeElement).outlineStyle === 'solid'),
    'keyboard controls lost visible focus');
    for (let n = 0; n < 8 && !await page.locator('.filters select').first().evaluate((node) => node === document.activeElement); n++)
      await page.keyboard.press('Tab');
    assert(await page.locator('.filters select').first().evaluate((node) =>
      node.matches(':focus-visible') && getComputedStyle(node).outlineStyle === 'solid'), 'keyboard select lost visible focus');
    assert(await page.locator('main').evaluate((node) => getComputedStyle(node).outlineStyle === 'none'), 'main has a navigation ring');
    const immediate = [
      ['#/jobs', '/api/jobs', 'status', 'completed', 'open'],
      ['#/duplicates', '/api/duplicates', 'scope', 'global', 'user'],
      ['#/metadata', '/api/review', 'status', 'decided', 'pending'],
    ];
    for (const [route, endpoint, key, value, reset] of immediate) {
      await go(route);
      assert(await page.locator('.filter-apply').count() === 0, 'unnecessary Apply on ' + route);
      await waitQuery(endpoint, key, value, () => page.locator('.filters select').first().selectOption(value));
      await page.reload();
      await page.locator('.filters select').first().waitFor();
      assert(await page.locator('.filters select').first().inputValue() === value, 'F5 lost immediate filter on ' + route);
      await page.locator('.mainnav a[href="' + route + '"]').click();
      await page.waitForFunction((value) => document.querySelector('main .filters select')?.value === value, reset);
      await page.locator('.filters button').filter({ hasText: /Clear/ }).first().click();
    }

    await go('#/logs?job_id=1');
    await page.locator('.log-export-note').waitFor();
    const job = page.locator('.log-job-filter');
    const apply = page.locator('.filter-apply');
    const before = calls.filter((url) => url.pathname === '/api/logs').length;
    await job.fill('99');
    await page.waitForTimeout(200);
    assert(calls.filter((url) => url.pathname === '/api/logs').length === before, 'typing queries Diagnostics');
    assert(await apply.evaluate((node) => node.classList.contains('pending')), 'Apply lacks pending highlight');
    await waitQuery('/api/logs', 'level', 'WARNING', () => page.locator('.filters select').first().selectOption('WARNING'));
    assert(last('/api/logs').get('job_id') === '1' && await job.inputValue() === '99', 'dropdown applies or overwrites pending number');
    await waitQuery('/api/logs', 'job_id', '99', () => job.press('Enter'));
    await job.fill('14');
    await waitQuery('/api/logs', 'job_id', '14', () => apply.click());
    await page.reload();
    await job.waitFor();
    assert(await job.inputValue() === '14', 'F5 lost applied number');
    await page.locator('.filters button').filter({ hasText: /Clear/ }).first().click();
    await page.waitForFunction(() => location.hash === '#/logs');
    await page.locator('.log-only-present').uncheck();
    await page.waitForFunction(() => location.hash.includes('only_present=0'));
    await waitQuery('/api/logs', 'event', 'FILE_UNREADABLE', () => choose(page.locator('.log-event-filter'), 'FILE_UNREADABLE'));

    await go('#/security-activity');
    assert(await page.locator('.filter-apply').count() === 0, 'unnecessary Security Apply');
    await waitQuery('/api/admin/audit', 'event', 'LOGIN_FAILED', () => choose(page.locator('main .combo-trigger'), 'LOGIN_FAILED'));
    await waitQuery('/api/admin/audit', 'user', 'pippo', () => page.locator('.filters select').selectOption('pippo'));
    await waitQuery('/api/admin/audit', 'since', '2026-11-24T00:00:00Z', () => page.locator('.filters input[type="date"]').first().fill('2026-11-24'));
    await page.reload();
    await page.locator('.filters input[type="date"]').first().waitFor();
    assert(await page.locator('.filters input[type="date"]').first().inputValue() === '2026-11-24', 'F5 lost Security date');

    for (const route of ['#/admin/users', '#/admin/groups']) {
      await go(route);
      const search = page.locator('.filters input[type="search"]');
      await search.fill('missing-name');
      assert(await page.locator('.filter-apply').evaluate((node) => node.classList.contains('pending')), 'admin Apply is not pending');
      await page.locator('.filters select').last().selectOption('1');
      await page.waitForFunction(() => location.hash.includes('active=1'));
      assert(!new URL(page.url()).hash.includes('search='), 'state applies pending admin text');
      assert(await search.inputValue() === 'missing-name', 'state overwrites pending admin text');
      await search.press('Enter');
      await page.waitForFunction(() => location.hash.includes('search=missing-name'));
      await page.reload();
      await search.waitFor();
      assert(await search.inputValue() === 'missing-name', 'F5 loses admin text');
      await search.fill('');
      await page.locator('.filter-apply').click();
      await page.waitForFunction(() => !location.hash.includes('search='));
      await page.locator('.filters button').filter({ hasText: /Clear/ }).first().click();
      await page.waitForFunction(() => !location.hash.includes('?'));
    }

    await page.goto(base + '/#/admin/settings');
    await page.locator('.exclusions-list').waitFor();
    assert(await page.locator('.exclusions-list .badge').count() === 7, 'built-in patterns are not labelled');
    await page.locator('.exclusions-list button').filter({ hasText: /^Add pattern$/ }).click();
    await page.locator('.modal').waitFor();
    await page.getByLabel('Pattern', { exact: true }).fill('*');
    await page.locator('.modal button').filter({ hasText: /^Save$/ }).click();
    await page.getByText('This pattern would exclude every name. Use a more specific pattern.', { exact: true }).first().waitFor();
    assert(await page.locator('.modal').count() === 1, 'invalid pattern closed dialog');
    await page.getByLabel('Pattern', { exact: true }).fill('Backup');
    await page.getByLabel('Test a name', { exact: true }).fill('Backup');
    await page.locator('.exclusion-dialog [role="status"]').filter({ hasText: 'Excluded by: Backup' }).waitFor();
    await page.locator('.modal button').filter({ hasText: /^Save$/ }).click();
    await page.locator('.modal').waitFor({ state: 'hidden' });
    await page.locator('main .form > .actions button').first().click();
    await page.locator('.modal-body').filter({ hasText: 'Added: Backup' }).waitFor();
    const writes = calls.filter((url) => url.pathname === '/api/admin/settings').length;
    await page.locator('.modal button').filter({ hasText: /^Cancel$/ }).click();
    assert(calls.filter((url) => url.pathname === '/api/admin/settings').length === writes, 'cancelled save writes exclusions');
    await page.goto(base + '/#/home');
    await page.locator('main .flow').waitFor();
    const banner = page.locator('.security-banner');
    if (await banner.count()) {
      assert(await banner.locator('.banner-close').evaluate((button) => {
        const rect = button.getBoundingClientRect(), parent = button.parentElement.getBoundingClientRect();
        return rect.top >= parent.top && rect.bottom <= parent.bottom && rect.right <= parent.right;
      }), 'native banner close is outside');
    }
    return { filterPages: 7, immediateAndDeferred: true, urlReloadAndClear: true, exclusionsDialog: true, keyboardFocus: true };
  } finally { page.off('request', record); }
}
