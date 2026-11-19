// Reusable UI pieces: job progress, polling, source picker, folder browser.
import { h, button, badge, progress, mount, toast } from './dom.js';
import { t, fmt } from './i18n.js';
import { get, post, ApiError } from './api.js';

const pollers = new Set();
let pollingEnabled = false;
let pollingGeneration = 0;
export function resumePolling() { pollingEnabled = true; }
export function stopPolling() {
  pollingEnabled = false;
  pollingGeneration++;
  for (const stop of pollers) stop();
}

const STATUS_CLASS = { queued: 'muted', running: 'info', paused: 'warn', interrupted: 'warn', completed: 'ok', failed: 'error', cancelled: 'muted' };

export function jobBadge(status) { return badge(t('job.status.' + status), STATUS_CLASS[status] || ''); }

export function jobCard(job, onChange) {
  const actions = [];
  if (job.status === 'running' || job.status === 'queued') actions.push(button('job.pause', () => act(job, 'pause', onChange)));
  if (['paused', 'interrupted', 'failed'].includes(job.status) && !['export', 'import', 'migrate'].includes(job.kind)) {
    actions.push(button('job.resume', () => act(job, 'resume', onChange), 'primary'));
  }
  if (['running', 'queued', 'paused', 'interrupted'].includes(job.status)) actions.push(button('job.cancel', () => act(job, 'cancel', onChange), 'danger-outline'));
  if (job.status === 'completed' && job.apply_kind) actions.push(h('a', { class: 'btn primary', href: '#/plans/' + job.id }, t('job.open_plan')));
  const count = job.progress_total > 0
    ? t('job.progress_of', { done: job.progress_done, total: job.progress_total })
    : t('job.progress_items', { count: job.progress_done || 0 });
  const scanResult = job.kind === 'scan' && job.status === 'completed' && job.result
    ? t(job.result.reused === undefined ? 'job.scan_result_legacy' : 'job.scan_result', job.result) : null;
  return h('div', { class: 'job-card' },
    h('div', { class: 'job-head' }, h('strong', {}, t('job.kind.' + job.kind)), ' ', h('span', { class: 'muted' }, '#' + job.id), ' ', jobBadge(job.status),
      job.owner ? h('span', { class: 'muted' }, ' · ' + job.owner) : null),
    job.status === 'running' || job.status === 'queued' ? progress(job.progress_done, job.progress_total) : null,
    h('div', { class: 'job-meta' }, scanResult || [(job.phase ? t('job.phase', { phase: job.phase }) + ' · ' : ''), count].join(''),
      job.updated_at ? ' · ' + fmt.date(job.updated_at) : ''),
    job.status === 'interrupted' && !['scan', 'verify', 'reconcile'].includes(job.kind) ? h('p', { class: 'notice warn' }, t('job.needs_confirmation')) : null,
    job.error ? h('p', { class: 'notice error' }, h('code', {}, job.error)) : null,
    actions.length ? h('div', { class: 'actions' }, actions) : null);
}

async function act(job, action, onChange) {
  try { await post(`/api/jobs/${job.id}/${action}`); if (onChange) onChange(); } catch (e) { toast(e.message, 'error'); }
}

// Polls a function while the tab is visible; returns a stop function.
export function poll(fn, ms) {
  if (!pollingEnabled) return () => {};
  let timer = null; let stopped = false; let running = false;
  const tick = async () => {
    if (stopped || running) return;
    running = true;
    if (!document.hidden) { try { await fn(); } catch (e) { /* keep polling */ } }
    running = false;
    if (!stopped) timer = setTimeout(tick, ms);
  };
  const vis = () => { if (!document.hidden && !stopped) { clearTimeout(timer); tick(); } };
  const stop = () => { stopped = true; clearTimeout(timer); document.removeEventListener('visibilitychange', vis); pollers.delete(stop); };
  pollers.add(stop);
  document.addEventListener('visibilitychange', vis);
  tick();
  return stop;
}

export function jobList(container, query, opts = {}) {
  const box = h('div', { class: 'jobs' });
  container.appendChild(box);
  const refresh = async () => {
    const data = await get('/api/jobs?' + query);
    mount(box, data.items.length ? data.items.map((j) => jobCard(j, refresh)) : h('p', { class: 'muted' }, t(opts.emptyKey || 'job.none')));
    if (opts.onData) opts.onData(data.items);
  };
  return { refresh, stop: poll(refresh, opts.interval || 1500) };
}

export async function waitForJob(id, onUpdate) {
  const generation = pollingGeneration;
  for (;;) {
    if (!pollingEnabled || generation !== pollingGeneration) throw new ApiError(401, 'unauthenticated');
    const j = await get('/api/jobs/' + id);
    if (onUpdate) onUpdate(j);
    if (['completed', 'failed', 'cancelled', 'paused', 'interrupted'].includes(j.status)) return j;
    await new Promise((resolve, reject) => {
      const stop = () => { clearTimeout(timer); pollers.delete(stop); reject(new ApiError(401, 'unauthenticated')); };
      const timer = setTimeout(() => { pollers.delete(stop); resolve(); }, document.hidden ? 3000 : 800);
      pollers.add(stop);
    });
  }
}

export async function sourceSelect(allowAll = false) {
  const data = await get('/api/sources');
  const sel = h('select', {},
    allowAll ? h('option', { value: '' }, t('sources.all')) : null,
    data.sources.map((s) => h('option', { value: s.id }, s.label + (s.same_as_source ? '' : ' → ' + s.dest))));
  return { select: sel, sources: data.sources, canBrowse: data.can_browse };
}

export function folderBrowser(onPick, start = '') {
  const box = h('div', { class: 'browser' });
  const load = async (path) => {
    let data;
    try { data = await get('/api/fs/browse?path=' + encodeURIComponent(path)); } catch (e) { toast(e.message, 'error'); return; }
    const parent = data.path.includes('/') ? data.path.slice(0, data.path.lastIndexOf('/')) : '';
    mount(box,
      h('div', { class: 'browser-head' }, h('code', {}, '/data/' + data.path),
        button('browser.choose', () => onPick(data.path), 'primary small')),
      h('ul', { class: 'browser-list' },
        data.path ? h('li', {}, h('button', { type: 'button', class: 'link', onclick: () => load(parent) }, '..')) : null,
        data.dirs.map((d) => h('li', {}, h('button', { type: 'button', class: 'link', onclick: () => load((data.path ? data.path + '/' : '') + d) }, d + '/')))));
  };
  load(start);
  return box;
}

export function kv(pairs) {
  return h('dl', { class: 'kv' }, pairs.map(([k, v]) => [h('dt', {}, t(k)), h('dd', {}, v === null || v === undefined || v === '' ? t('common.na') : v)]));
}

export function pager(total, offset, limit, go) {
  if (total <= limit) return null;
  return h('div', { class: 'pager' },
    h('button', { type: 'button', class: 'btn small', disabled: offset <= 0, onclick: () => go(Math.max(0, offset - limit)) }, t('common.previous')),
    h('span', {}, t('common.page_range', { from: offset + 1, to: Math.min(total, offset + limit), total })),
    h('button', { type: 'button', class: 'btn small', disabled: offset + limit >= total, onclick: () => go(offset + limit) }, t('common.next')));
}
