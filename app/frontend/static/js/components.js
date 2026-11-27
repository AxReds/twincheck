// Reusable UI pieces: job progress, polling, source picker, folder browser.
import { h, button, badge, progress, mount, toast, instant, field, modal } from './dom.js';
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

let dismissCombo = null;
document.addEventListener('pointerdown', (event) => { if (dismissCombo) dismissCombo(event); });

export function comboBox({ options, value = '', onChange = () => {}, searchKey = 'logs.search_events', emptyKey = 'logs.no_matching_event' }) {
  const id = 'combo-' + Math.random().toString(36).slice(2);
  const label = h('span', { class: 'combo-value' });
  const input = h('button', { type: 'button', class: 'combo-trigger', role: 'combobox',
    'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'aria-controls': id }, label, h('span', { 'aria-hidden': 'true' }, '▾'));
  const search = h('input', { type: 'search', role: 'combobox', autocomplete: 'off', placeholder: t(searchKey),
    'aria-label': t(searchKey), 'aria-autocomplete': 'list', 'aria-expanded': 'false', 'aria-controls': id });
  const list = h('div', { id, role: 'listbox', class: 'combo-list' });
  const clearSearch = h('button', { type: 'button', class: 'link combo-clear', 'aria-label': t('common.clear_search'),
    onclick: () => { search.value = ''; render(); search.focus(); } }, '✕');
  const popup = h('div', { class: 'combo-popup', hidden: true },
    h('div', { class: 'combo-search' }, h('span', { 'aria-hidden': 'true' }, '⌕'), search, clearSearch), list);
  const el = h('div', { class: 'combobox' }, input, popup);
  let committed = value;
  let active = -1;
  let filtered = [];
  const match = () => options.find((option) => option.value === committed);
  const mark = () => {
    const nodes = [...list.querySelectorAll('[role="option"]')];
    nodes.forEach((node, i) => node.classList.toggle('active', i === active));
    for (const control of [input, search]) {
      if (nodes[active] && !popup.hidden) control.setAttribute('aria-activedescendant', nodes[active].id);
      else control.removeAttribute('aria-activedescendant');
    }
    if (nodes[active] && !popup.hidden) nodes[active].scrollIntoView({ block: 'nearest' });
  };
  const close = (restoreFocus = false) => {
    popup.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    search.setAttribute('aria-expanded', 'false');
    search.value = '';
    dismissCombo = null;
    mark();
    if (restoreFocus) input.focus();
  };
  const choose = (option) => {
    committed = option.value;
    input.value = option.label;
    label.textContent = option.label;
    close(true);
    render();
    onChange(committed, true);
  };
  const render = () => {
    const term = search.value.trim().toLowerCase();
    filtered = options.filter((option) => (option.label + ' ' + option.value).toLowerCase().includes(term));
    active = filtered.findIndex((option) => option.value === committed);
    mount(list, filtered.length ? filtered.map((option, i) => [
      option.section && (i === 0 || option.section !== filtered[i - 1].section)
        ? h('div', { role: 'presentation', class: 'combo-section' }, t(option.section)) :
        option.muted && i > 0 && !filtered[i - 1].muted ? h('div', { role: 'separator', class: 'combo-separator' }) : null,
      h('div', { id: `${id}-${i}`, role: 'option', 'aria-selected': String(option.value === committed),
        class: 'combo-option' + (option.muted ? ' muted' : ''),
        onpointerdown: (event) => event.preventDefault(),
        onpointermove: () => { active = i; mark(); },
        onclick: () => choose(option) }, h('span', {}, option.label),
        option.value === committed ? h('span', { 'aria-hidden': 'true' }, '✓') : null),
    ]) : h('div', { class: 'hint', role: 'status' }, t(emptyKey)));
    queueMicrotask(mark);
  };
  const open = () => {
    if (!popup.hidden) return;
    if (dismissCombo) dismissCombo();
    popup.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    search.setAttribute('aria-expanded', 'true');
    render();
    popup.classList.remove('upward');
    const rect = input.getBoundingClientRect();
    if (innerHeight - rect.bottom < popup.getBoundingClientRect().height && rect.top > innerHeight - rect.bottom) popup.classList.add('upward');
    search.focus();
    dismissCombo = (event) => { if (!event || !el.contains(event.target)) close(); };
  };
  input.addEventListener('click', () => popup.hidden ? open() : close());
  input.addEventListener('keydown', (event) => {
    if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); open(); }
    else if (event.key === 'Escape') { event.preventDefault(); close(true); }
  });
  search.addEventListener('input', render);
  search.addEventListener('keydown', (event) => {
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault();
      active = filtered.length ? (active < 0
        ? (event.key === 'ArrowDown' ? 0 : filtered.length - 1)
        : (active + (event.key === 'ArrowDown' ? 1 : -1) + filtered.length) % filtered.length) : -1;
      mark();
    } else if (event.key === 'Enter' && filtered[active]) {
      event.preventDefault(); choose(filtered[active]);
    } else if (event.key === 'Escape') {
      event.preventDefault(); event.stopPropagation();
      close(true);
    } else if (event.key === 'Tab') {
      close();
    }
  });
  input.value = match()?.label || value;
  label.textContent = input.value;
  render();
  return { el, input, search, close, value: () => match()?.value, valid: () => Boolean(match()),
    setOptions(next) { options = next; render(); },
    setValue(next) {
      const option = options.find((option) => option.value === next);
      committed = next;
      label.textContent = input.value = option?.label || next;
      render();
    } };
}

export function analysisSummary(population) {
  if (!population) return null;
  return h('div', { class: 'analysis-population' },
    h('p', { class: 'summary' }, t('meta.compact_summary', population), ' ',
      h('span', { tabindex: '0', role: 'note', title: t('meta.latest_population', population) + ' ' + t('meta.current_population', population),
        'aria-label': t('meta.latest_population', population) + ' ' + t('meta.current_population', population) }, 'ⓘ')));
}

export function tablePaging(userId, name, go) {
  const key = `tc-rows:${userId}:${name}`;
  let choice = '50';
  try { choice = localStorage.getItem(key) || choice; }
  catch (error) { toast(t('common.rows_storage_unavailable'), 'warn'); }
  if (!['10', '50', '100', 'all'].includes(choice)) choice = '50';
  const select = h('select', { class: 'rows-per-page', 'data-table': name },
    ['10', '50', '100', 'all'].map((value) => h('option', { value, selected: value === choice },
      value === 'all' ? t('common.all_rows') : value)));
  let total = null;
  let confirmed = false;
  let confirming = null;
  const range = h('div', { class: 'page-range' });
  const controls = h('div', { class: 'pager' }, field('common.rows_per_page', select), range);
  const approve = async () => {
    if (choice !== 'all' || total === null || total <= 1000 || confirmed) return true;
    if (!confirming) confirming = modal({ titleKey: 'common.all_rows', body: h('p', {}, t('common.confirm_all_rows', { count: total })) });
    confirmed = await confirming;
    confirming = null;
    return confirmed;
  };
  select.addEventListener('change', async () => {
    const previous = choice;
    choice = select.value; confirmed = false;
    if (!await approve()) { choice = previous; select.value = choice; return; }
    try { localStorage.setItem(key, choice); }
    catch (error) { toast(t('common.rows_storage_unavailable'), 'warn'); }
    go(0);
  });
  return {
    query: () => choice === 'all' ? (total !== null && (total <= 1000 || confirmed) ? 'all' : 50) : Number(choice),
    async check(count, loaded) {
      total = count;
      if (await approve()) {
        if (choice !== 'all' || loaded >= count) return true;
      } else {
        choice = '50'; select.value = choice;
      }
      go(0); return false;
    },
    render(count, offset) {
      total = count;
      controls.hidden = count === 0;
      mount(range,
        pager(count, offset, choice === 'all' ? Math.max(1, count) : Number(choice), go) ||
        h('span', {}, t('common.page_range', { from: count ? offset + 1 : 0, to: count, total: count })));
      return controls;
    },
  };
}

export function jobBadge(status) { return badge(t('job.status.' + status), STATUS_CLASS[status] || ''); }

export function stepGuide(current, status) {
  const steps = [
    [1, 'analysis.step_analyze', '#/analyze'],
    [2, 'analysis.step_review', '#/duplicates'],
    [3, 'analysis.step_organize', '#/organize'],
    [4, 'analysis.step_report', '#/report'],
  ];
  return h('ol', { class: 'step-guide', 'aria-label': t('analysis.steps') },
    steps.map(([number, label, href]) => h('li', {
      class: number === current ? 'current' : (status?.exists && number < current ? 'done' : ''),
      'aria-current': number === current ? 'step' : undefined,
    }, number === current
      ? h('span', {}, t(label))
      : h('a', { href }, status?.exists && number < current ? t('analysis.step_done', { step: t(label) }) : t(label)))));
}

export function databaseNotice(state) {
  return h('div', { class: 'notice warn database-notice', role: 'status' },
    state.perms.switch_database
      ? h('a', { href: '#/admin/database' }, t('db.update_required'))
      : t('db.update_required'));
}

export function jobCard(job, onChange) {
  const actions = [];
  if (job.status === 'running' || job.status === 'queued') actions.push(button('job.pause', () => act(job, 'pause', onChange)));
  if (['paused', 'interrupted', 'failed'].includes(job.status) && !['export', 'import', 'migrate'].includes(job.kind)) {
    actions.push(button('job.resume', () => act(job, 'resume', onChange), 'primary'));
  }
  if (['running', 'queued', 'paused', 'interrupted'].includes(job.status)) actions.push(button('job.cancel', () => act(job, 'cancel', onChange), 'danger-outline'));
  if (job.status === 'completed' && job.apply_kind && (job.kind !== 'analyze_library' || job.result?.metadata?.fills > 0))
    actions.push(h('a', { class: 'btn primary', href: '#/plans/' + job.id }, t('job.open_plan')));
  const count = job.progress_total > 0
    ? t('job.progress_of', { done: job.progress_done, total: job.progress_total })
    : t('job.progress_items', { count: job.progress_done || 0 });
  const scanResult = job.kind === 'scan' && job.status === 'completed' && job.result
    ? t(job.result.reused === undefined ? 'job.scan_result_legacy' : 'job.scan_result', job.result) : null;
  const pipeline = job.kind === 'analyze_library' && job.status === 'completed' ? job.result?.scan : null;
  const pipelineResult = pipeline ? [
    t('job.library_result', { found: pipeline.found ?? pipeline.indexed, changed: (pipeline.new || 0) + (pipeline.changed || 0),
      unchanged: pipeline.unchanged ?? pipeline.reused, hashed: pipeline.hashed }),
    ' · ', pipeline.unreadable > 0
      ? h('a', { href: `#/logs?job_id=${job.id}&level=WARNING&event=FILE_UNREADABLE` }, t('job.unreadable', { count: pipeline.unreadable }))
      : t('job.unreadable', { count: 0 }),
  ] : null;
  const phaseMatch = String(job.phase || '').match(/^([1-5])\/5\s+(.+)$/);
  const phaseText = phaseMatch
    ? t('job.pipeline_phase', { phase: phaseMatch[1], name: t('job.analysis_phase.' + phaseMatch[2]) })
    : job.phase ? t('job.phase', { phase: job.phase }) : '';
  return h('div', { class: 'job-card' },
    h('div', { class: 'job-head' }, h('strong', {}, t('job.kind.' + job.kind)), ' ', h('span', { class: 'muted' }, '#' + job.id), ' ', jobBadge(job.status),
      job.owner ? h('span', { class: 'muted' }, ' · ' + job.owner) : null),
    job.status === 'running' || job.status === 'queued' ? progress(job.progress_done, job.progress_total) : null,
    h('div', { class: 'job-meta' }, pipelineResult || scanResult || [(phaseText ? phaseText + ' · ' : ''), count].join(''),
      job.updated_at ? [' · ', instant(job.updated_at)] : ''),
    job.status === 'interrupted' && !['scan', 'verify', 'reconcile'].includes(job.kind) ? h('p', { class: 'notice warn' }, t('job.needs_confirmation')) : null,
    job.error ? h('p', { class: 'notice error' }, h('code', {}, job.error)) : null,
    actions.length ? h('div', { class: 'actions' }, actions) : null);
}

async function act(job, action, onChange) {
  try { await post(`/api/jobs/${job.id}/${action}`); if (onChange) onChange(); } catch (e) { toast(e.message, 'error'); }
}

// Polls a function while the tab is visible; returns a stop function.
export function refreshBlocked(container) {
  return Boolean(container && (container.contains(document.activeElement) &&
    document.activeElement.matches('input, select, textarea, button, summary, [role="combobox"]') ||
    container.querySelector('details[open], [aria-expanded="true"]') ||
    document.querySelector('.modal-overlay, .menu-panel:not([hidden])')));
}

export function poll(fn, ms, container = null) {
  if (!pollingEnabled) return () => {};
  let timer = null; let stopped = false; let running = false;
  const tick = async () => {
    if (stopped || running) return;
    running = true;
    if (!document.hidden && !refreshBlocked(container)) { try { await fn(); } catch (e) { /* keep polling */ } }
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
  const refresh = async (automatic = false) => {
    const data = await get('/api/jobs?' + query);
    if (!box.isConnected || automatic && refreshBlocked(container)) return;
    const scroll = window.scrollY;
    mount(box, data.items.length ? data.items.map((j) => jobCard(j, refresh)) : h('p', { class: 'muted' }, t(opts.emptyKey || 'job.none')));
    window.scrollTo(0, scroll);
    if (opts.onData) opts.onData(data.items);
  };
  return { refresh, stop: poll(() => refresh(true), opts.interval || 1500, container) };
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
  return { select: sel, sources: data.sources, canBrowse: data.can_browse, scanSelection: data.scan_selection };
}

export function folderBrowser(onPick, start = '', selectedPath = null) {
  const box = h('div', { class: 'browser' });
  let currentPath = null;
  let choose = null;
  const mark = () => {
    if (!choose || !selectedPath) return;
    const selected = currentPath === selectedPath();
    choose.classList.toggle('primary', selected);
    choose.setAttribute('aria-pressed', String(selected));
  };
  box.addEventListener('po:folder-selected', mark);
  const load = async (path) => {
    if (path === null) { mount(box); return; }
    let data;
    try { data = await get('/api/fs/browse?path=' + encodeURIComponent(path)); } catch (e) { toast(e.message, 'error'); return; }
    const parent = data.path.includes('/') ? data.path.slice(0, data.path.lastIndexOf('/')) : '';
    currentPath = data.path;
    choose = button('browser.choose', () => { onPick(data.path); mark(); }, selectedPath ? 'small' : 'primary small');
    mount(box,
      h('div', { class: 'browser-head' }, h('code', {}, data.display_path || data.path || '.'),
        choose),
      h('ul', { class: 'browser-list' },
        data.path && data.path !== data.root ? h('li', {}, h('button', { type: 'button', class: 'link', onclick: () => load(parent) }, '..')) : null,
        data.dirs.map((d) => h('li', {}, h('button', { type: 'button', class: 'link', onclick: () => load((data.path ? data.path + '/' : '') + d) }, d + '/')))));
    mark();
  };
  box.addEventListener('po:folder-browse', (event) => load(event.detail));
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
