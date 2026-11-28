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
