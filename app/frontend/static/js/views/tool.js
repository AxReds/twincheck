// Tool pages: home, scan, duplicates, metadata, organise, plans, report & undo, jobs, logs, personal settings.
import { h, mount, button, field, table, modal, toast, badge } from '../dom.js';
import { t, fmt } from '../i18n.js';
import { get, post } from '../api.js';
import { jobList, sourceSelect, folderBrowser, kv, pager, poll, jobBadge, waitForJob } from '../components.js';

function page(main, titleKey, introKey, ...body) {
  mount(main, h('h1', {}, t(titleKey)), introKey ? h('p', { class: 'intro' }, t(introKey)) : null, ...body);
}

async function startJob(url, body, then) {
  try {
    const r = await post(url, body);
    toast(t('job.started', { id: r.job_id }), 'ok');
    if (then) then(r.job_id);
    return r.job_id;
  } catch (e) { toast(e.message, 'error'); return null; }
}

function openPlanWhenReady(id) {
  waitForJob(id).then((j) => { if (j.status === 'completed') location.hash = '#/plans/' + id; });
}

// --- home -------------------------------------------------------------------------------------
export async function home(main, state) {
  const steps = [['#/scan', 'nav.scan', 'home.step_scan'], ['#/duplicates', 'nav.duplicates', 'home.step_duplicates'],
    ['#/metadata', 'nav.metadata', 'home.step_metadata'], ['#/review', 'nav.review', 'home.step_review'],
    ['#/organize', 'nav.organize', 'home.step_organize'], ['#/report', 'nav.report', 'home.step_report']];
  const summary = h('div', { class: 'cards' });
  page(main, 'home.title', 'home.intro',
    h('ol', { class: 'flow' }, steps.map(([href, k, d]) => h('li', {}, h('a', { href }, t(k)), h('p', {}, t(d))))),
    h('h2', {}, t('home.summary')), summary, h('h2', {}, t('home.open_jobs')));
  try {
    const r = await get('/api/report');
    const total = r.files.reduce((a, x) => a + x.n, 0);
    const bytes = r.files.reduce((a, x) => a + x.bytes, 0);
    const pending = (r.review.find((x) => x.status === 'pending') || {}).n || 0;
    mount(summary, card('home.files', t('home.files_value', { count: total, size: fmt.bytes(bytes) })),
      card('home.duplicates', t('home.duplicates_value', { count: r.duplicates.groups, size: fmt.bytes(r.duplicates.recoverable) })),
      card('home.review_pending', fmt.number(pending)), card('home.missing', fmt.number(r.missing)));
  } catch (e) { mount(summary, h('p', { class: 'notice warn' }, e.message)); }
  const jl = jobList(main, 'status=open&limit=20');
  return { destroy: jl.stop };
}

function card(k, v) { return h('div', { class: 'card' }, h('div', { class: 'card-label' }, t(k)), h('div', { class: 'card-value' }, v)); }

// --- scan -------------------------------------------------------------------------------------
function noSources(state) {
  return state.perms.manage_users
    ? h('p', { class: 'notice warn' }, t('admin.no_sources'), ' ',
      h('a', { href: '#/admin/users' }, t('nav.admin'), ' → ', t('admin.users')),
      ': ', t('admin.edit'), ' → ', t('admin.sources'))
    : h('p', { class: 'notice warn' }, t('scan.no_sources'));
}

export async function scan(main, state) {
  const { select, sources, canBrowse, scanSelection } = await sourceSelect();
  const adminBrowse = Boolean(state.perms.see_all_sources);
  if (state.scanPath === null && scanSelection) state.scanPath = scanSelection.path;
  const selectedSource = () => sources.find((s) => s.path_rel === state.scanPath ||
    (!adminBrowse && state.scanPath?.startsWith(s.path_rel + '/')));
  select.value = selectedSource()?.id || '';
  const adhocLabel = h('code', {});
  const noSelection = h('p', { class: 'notice warn', role: 'status' }, t('scan.choose_folder'));
  const updateSelection = () => {
    const source = selectedSource();
    adhocLabel.textContent = state.scanPath === null ? '' : (adminBrowse ? '/data/' + state.scanPath :
      source ? source.path + state.scanPath.slice(source.path_rel.length) : '');
    noSelection.hidden = state.scanPath !== null;
    select.value = selectedSource()?.id || '';
    browser?.querySelector('.browser')?.dispatchEvent(new Event('po:folder-selected'));
  };
  const browser = canBrowse ? h('details', { class: 'panel' }, h('summary', {}, t(adminBrowse ? 'scan.browse_data' : 'scan.source')),
    folderBrowser((p) => { state.scanPath = p; updateSelection(); }, state.scanPath ?? (adminBrowse ? '' : null), () => state.scanPath)) : null;
  select.addEventListener('change', () => {
    state.scanPath = sources.find((s) => s.id === select.value)?.path_rel ?? null;
    updateSelection();
    browser?.querySelector('.browser')?.dispatchEvent(new CustomEvent('po:folder-browse', { detail: state.scanPath }));
  });
  updateSelection();
  const startSelected = () => {
    if (state.scanPath === null) { noSelection.hidden = false; return; }
    const source = selectedSource();
    startJob('/api/scan', source ? { source_id: source.id, ...(state.scanPath !== source.path_rel ? { path: state.scanPath } : {}) } : { path: state.scanPath });
  };
  page(main, 'scan.title', 'scan.intro',
    sources.length ? field('scan.source', select) : noSources(state),
    browser, h('p', {}, t('scan.adhoc_selected'), ' ', adhocLabel), noSelection,
    h('div', { class: 'actions' },
      sources.length ? button('scan.start', startSelected, 'primary') : null,
      adminBrowse ? button('scan.start_adhoc', startSelected) : null,
      (sources.length || canBrowse) ? button('scan.analyze_library', () => {
        if (state.scanPath === null) { noSelection.hidden = false; return; }
        const source = selectedSource();
        startJob('/api/library/analyze', source ? { source_id: source.id } : { path: state.scanPath });
      }, 'primary') : null),
    h('h2', {}, t('scan.jobs')));
  const jl = jobList(main, 'kind=analyze_library&limit=10');
  return { destroy: jl.stop };
}

// --- duplicates ------------------------------------------------------------------------------
export async function duplicates(main, state) {
  let offset = 0;
  const list = h('div', {});
  const summary = h('p', { class: 'summary' });
  const scopeSel = state.perms.global_dedupe ? h('select', {}, h('option', { value: 'user' }, t('dup.scope_user')), h('option', { value: 'global' }, t('dup.scope_global'))) : null;
  const scope = () => (scopeSel ? scopeSel.value : 'user');
  const load = async () => {
    const d = await get(`/api/duplicates?scope=${scope()}&offset=${offset}&limit=20`);
    summary.textContent = t('dup.summary', { count: d.total, size: fmt.bytes(d.recoverable) });
    const last = d.analyzed_at
      ? h('p', { class: 'notice' }, t('analysis.results_from', { date: fmt.date(d.analyzed_at) }), ' ', t('analysis.recheck_before_action'))
      : h('p', { class: 'notice warn' }, t('dup.no_analysis'), ' ', h('a', { href: '#/scan' }, t('analysis.go_scan')));
    mount(list, last, d.groups.length ? d.groups.map((g) => group(g)) : h('p', { class: 'muted' }, t('dup.none')),
      pager(d.total, offset, 20, (o) => { offset = o; load(); }));
  };
  const group = (g) => h('fieldset', { class: 'dup-group' },
    h('legend', {}, t('dup.group_title', { size: fmt.bytes(g.size), count: g.members.length, recoverable: fmt.bytes(g.recoverable) })),
    h('code', { class: 'hash' }, g.sha256),
    h('p', { class: 'keeper-decision' },
      g.capture_date ? t('dup.capture_date', { date: fmt.localDate(g.capture_date) }) : t('dup.date_unknown'),
      g.date_flags === 'utc_ambiguous' ? ' · ' + t('review.utc') : '',
      ' · ', t('dup.keeper_date', { date: fmt.localDate(g.keeper_date) }), ' · ', t('dup.keeper_rule.' + g.keeper_rule)),
    h('ul', {}, g.members.map((m) => {
      const r = h('input', { type: 'radio', name: 'keep' + g.id, checked: m.id === g.keeper_file_id, 'aria-label': t('dup.keep') });
      r.addEventListener('change', async () => { try { await post(`/api/duplicates/${g.id}/keeper`, { file_id: m.id }); load(); } catch (e) { toast(e.message, 'error'); } });
      const exif = h(g.keeper_rule.startsWith('capture_date_') ? 'strong' : 'span', { class: 'dup-date-exif' },
        t('dup.exif_date', { date: m.exif_date ? fmt.localDate(m.exif_date) : t('dup.unknown_date') }));
      const filesystem = h(g.keeper_rule.startsWith('mtime') ? 'strong' : 'span', { class: 'dup-date-filesystem' },
        t('dup.filesystem_date', { date: fmt.localDate(m.filesystem_date) }));
      return h('li', { class: m.id === g.keeper_file_id ? 'keeper' : '' }, h('label', {}, r, ' ', m.id === g.keeper_file_id ? badge(t('dup.kept'), 'ok') : badge(t('dup.duplicate'), 'warn'), ' ',
        h('code', {}, m.path || t('dup.other_user')), m.owner ? h('span', { class: 'muted' }, ' · ' + m.owner) : null,
        ' · ', exif, ' · ', filesystem));
    })));
  if (scopeSel) scopeSel.addEventListener('change', () => { offset = 0; load(); });
  page(main, 'dup.title', 'dup.intro',
    scopeSel ? field('dup.scope', scopeSel) : null,
    h('div', { class: 'actions' },
      button('dup.analyze', () => startJob(scope() === 'global' ? '/api/admin/global-dedupe/analyze' : '/api/duplicates/analyze', {}, (id) => waitForJob(id).then(load))),
      button('dup.plan', () => startJob(scope() === 'global' ? '/api/admin/global-dedupe/plan' : '/api/duplicates/plan', {}, openPlanWhenReady), 'primary'),
      h('a', { class: 'btn', href: '/api/duplicates.csv?scope=' + scope(), download: 'duplicates.csv' }, t('dup.export_csv'))),
    h('p', { class: 'hint' }, t('dup.rule')), summary, list);
  await load();
  const jl = jobList(main, 'kind=dedupe_analyze&limit=3');
  return { destroy: jl.stop };
}

// --- metadata --------------------------------------------------------------------------------
export async function metadata(main) {
  const { select } = await sourceSelect(true);
  const results = h('div', {});
  const loadResults = async () => {
    const data = await get('/api/metadata/results');
    if (!data.analyzed_at || !data.summary) {
      mount(results, h('p', { class: 'notice warn' }, t('meta.no_analysis'), ' ', h('a', { href: '#/scan' }, t('analysis.go_scan'))));
      return;
    }
    mount(results,
      h('p', { class: 'notice' }, t('analysis.results_from', { date: fmt.date(data.analyzed_at) }), ' ', t('analysis.recheck_before_action')),
      h('p', { class: 'summary' }, t('meta.summary', data.summary)),
      data.review_pending > 0 ? h('a', { class: 'btn', href: '#/review' }, t('meta.review_items', { count: data.review_pending })) : null);
  };
  page(main, 'meta.title', 'meta.intro',
    h('ul', { class: 'rules' }, ['meta.rule_copy', 'meta.rule_never_invent', 'meta.rule_never_overwrite', 'meta.rule_conflicts', 'meta.rule_video_utc', 'meta.rule_backup']
      .map((k) => h('li', {}, t(k)))),
    field('scan.source', select),
    h('div', { class: 'actions' },
      h('a', { class: 'btn primary', href: '#/review' }, t('meta.open_review')),
      button('meta.analyze', () => startJob('/api/metadata/analyze', { source_id: select.value || undefined }, openPlanWhenReady))),
    results,
    h('h2', {}, t('meta.jobs')));
  const jl = jobList(main, 'kind=metadata_analyze&limit=5');
  await loadResults();
  return { destroy: jl.stop };
}

// --- organise ---------------------------------------------------------------------------------
export async function organize(main, state) {
  const { select, sources } = await sourceSelect();
  page(main, 'org.title', 'org.intro',
    h('ul', { class: 'rules' }, ['org.rule_structure', 'org.rule_no_date', 'org.rule_no_rename', 'org.rule_companions', 'org.rule_mtime', 'org.rule_volumes']
      .map((k) => h('li', {}, t(k)))),
    sources.length ? field('scan.source', select) : noSources(state),
    sources.length ? h('div', { class: 'actions' }, button('org.plan', () => startJob('/api/organize/plan', { source_id: select.value }, openPlanWhenReady), 'primary')) : null,
    h('h2', {}, t('org.jobs')));
  const jl = jobList(main, 'kind=organize_plan&limit=5');
  return { destroy: jl.stop };
}

// --- plan (dry-run) and confirmation ------------------------------------------------------------
export async function plan(main, state, [id]) {
  let offset = 0;
  let section = '#/jobs';
  let status = '';
  const box = h('div', {});
  const applyBox = h('div', {});
  let stopApply = null;
  const load = async () => {
    const d = await get(`/api/plans/${id}?offset=${offset}&limit=100${status ? '&status=' + status : ''}`);
    section = { review_plan: '#/review', metadata_analyze: '#/metadata', dedupe_plan: '#/duplicates',
      organize_plan: '#/organize', undo_moves_plan: '#/report', undo_meta_plan: '#/report' }[d.job.kind] || '#/jobs';
    const counts = {};
    for (const s of d.summary) counts[s.status] = (counts[s.status] || 0) + s.n;
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const filter = h('select', {}, ['', 'planned', 'blocked', 'done', 'skipped', 'failed'].map((s) => h('option', { value: s, selected: s === status }, s ? t('plan.status.' + s) : t('plan.all'))));
    filter.addEventListener('change', () => { status = filter.value; offset = 0; load(); });
    const rows = d.items.map((it) => h('tr', {},
      h('td', {}, t('plan.kind.' + it.kind)),
      h('td', {}, h('code', {}, it.src), it.companions.map((c) => h('div', { class: 'muted small' }, '+ ', h('code', {}, c.src)))),
      h('td', {}, it.dst ? h('code', {}, it.dst) : null, it.renamed ? h('div', {}, badge(t('plan.renamed'), 'warn')) : null,
        Object.entries(it.fills).map(([f, v]) => h('div', { class: 'small' }, t('field.' + f), ': ', h('code', {}, String(v.value)), ' ', h('span', { class: 'muted' }, t('plan.from', { tag: v.from, source: v.source })))),
        it.restore.length ? h('div', { class: 'small' }, t('plan.restore_tags'), ' ', h('code', {}, it.restore.join(', '))) : null),
      h('td', {}, badge(t('plan.status.' + it.status), it.status === 'done' ? 'ok' : it.status === 'planned' ? 'info' : 'warn'), it.note ? h('div', { class: 'small muted' }, t('plan.note.' + it.note)) : null)));
    mount(box,
      h('div', { class: 'job-head' }, h('strong', {}, t('job.kind.' + d.job.kind)), ' #' + d.job.id, ' ', jobBadge(d.job.status)),
      d.job.result && d.job.kind === 'metadata_analyze'
        ? h('p', { class: 'notice' }, t('meta.summary', d.job.result))
        : null,
      h('p', { class: 'summary' }, t('plan.summary', { count: total, planned: counts.planned || 0, done: counts.done || 0, blocked: (counts.blocked || 0) + (counts.skipped || 0) })),
      field('plan.filter', filter),
      table(['plan.col_action', 'plan.col_from', 'plan.col_to', 'plan.col_status'], rows),
      pager(total, offset, 100, (o) => { offset = o; load(); }),
      d.can_confirm && (counts.planned || 0) > 0 ? h('div', { class: 'actions sticky' }, button('plan.confirm', () => confirmPlan(d, counts.planned), 'primary')) : null,
      d.apply_jobs.length ? h('p', {}, t('plan.apply_jobs'), ' ', d.apply_jobs.map((a) => h('span', {}, '#' + a.id + ' ', jobBadge(a.status), ' '))) : null);
  };
  const confirmPlan = async (d, n) => {
    const ok = await modal({ titleKey: 'plan.confirm_title', body: h('div', {}, h('p', {}, t('plan.confirm_text', { count: n })), h('p', { class: 'hint' }, t('plan.confirm_safety'))),
      confirmKey: 'plan.confirm_button', requireCheckKey: 'plan.confirm_check' });
    if (!ok) return;
    try {
      const r = await post(`/api/plans/${id}/confirm`, { confirm: true });
      toast(t('job.started', { id: r.job_id }), 'ok');
      if (stopApply) stopApply();
      stopApply = jobList(applyBox, 'kind=' + d.job.apply_kind + '&limit=3').stop;
    } catch (e) { toast(e.message, 'error'); }
  };
  page(main, 'plan.title', 'plan.intro', box, h('h2', {}, t('plan.execution')), applyBox);
  await load();
  const stop = poll(load, 4000);
  return { section, destroy: () => { stop(); if (stopApply) stopApply(); } };
}

// --- report, manifest, undo, verify --------------------------------------------------------------
export async function report(main) {
  const r = await get('/api/report');
  const rows = r.files.map((x) => h('tr', {}, h('td', {}, t('file.status.' + x.status)), h('td', {}, t('file.kind.' + x.media_kind)), h('td', {}, fmt.number(x.n)), h('td', {}, fmt.bytes(x.bytes))));
  const man = r.manifest.map((x) => h('tr', {}, h('td', {}, t('manifest.action.' + x.action)), h('td', {}, fmt.number(x.n)), h('td', {}, fmt.number(x.undone || 0))));
  page(main, 'report.title', 'report.intro',
    h('h2', {}, t('report.files')), table(['report.col_status', 'report.col_kind', 'report.col_count', 'report.col_size'], rows),
    h('p', {}, t('report.missing', { count: r.missing })),
    h('h2', {}, t('report.manifest')), table(['report.col_action', 'report.col_count', 'report.col_undone'], man),
    h('div', { class: 'actions' }, h('a', { class: 'btn', href: '/api/manifest.csv', download: 'manifest.csv' }, t('report.manifest_csv'))),
    h('h2', {}, t('report.undo')), h('p', { class: 'hint' }, t('report.undo_db_note')),
    h('div', { class: 'actions' },
      button('report.undo_moves', () => startJob('/api/undo/moves/plan', {}, openPlanWhenReady)),
      button('report.undo_metadata', () => startJob('/api/undo/metadata/plan', {}, openPlanWhenReady))),
    h('h2', {}, t('report.verify')), h('p', { class: 'hint' }, t('report.verify_help')),
    h('div', { class: 'actions' }, button('report.verify_start', () => startJob('/api/verify', {}), 'primary')));
  const jl = jobList(main, 'kind=verify&limit=3');
  return { destroy: jl.stop };
}

// --- jobs -----------------------------------------------------------------------------------------
export async function jobs(main) {
  const sel = h('select', {}, ['open', '', 'completed', 'failed', 'cancelled'].map((s) => h('option', { value: s }, s ? t('job.filter.' + s) : t('job.filter.all'))));
  const box = h('div', {});
  page(main, 'jobs.title', 'jobs.intro', field('jobs.filter', sel), box);
  let jl = jobList(box, 'status=open&limit=50');
  sel.addEventListener('change', () => { jl.stop(); mount(box); jl = jobList(box, (sel.value ? 'status=' + sel.value + '&' : '') + 'limit=50'); });
  return { destroy: () => jl.stop() };
}

// --- logs (entries stay in English; only the surrounding UI is translated) -------------------------
export async function logs(main) {
  let offset = 0;
  const ev = h('input', { type: 'text', placeholder: 'FILE_MOVED', size: '16' });
  const lv = h('select', {}, ['', 'INFO', 'WARNING', 'ERROR'].map((l) => h('option', { value: l }, l || t('logs.all_levels'))));
  const job = h('input', { type: 'number', min: '1', size: '6' });
  const box = h('div', {});
  const load = async () => {
    const q = new URLSearchParams({ offset, limit: 100 });
    if (ev.value) q.set('event', ev.value.trim().toUpperCase());
    if (lv.value) q.set('level', lv.value);
    if (job.value) q.set('job_id', job.value);
    const d = await get('/api/logs?' + q);
    mount(box, table(['logs.col_time', 'logs.col_level', 'logs.col_event', 'logs.col_message', 'logs.col_job'],
      d.items.map((r) => h('tr', {}, h('td', {}, fmt.date(r.ts)), h('td', {}, r.level), h('td', {}, h('code', {}, r.event)),
        h('td', { lang: 'en', dir: 'ltr' }, r.message), h('td', {}, r.job_id ? '#' + r.job_id : '')))),
    pager(d.total, offset, 100, (o) => { offset = o; load(); }));
  };
  page(main, 'logs.title', 'logs.intro', h('div', { class: 'filters' }, field('logs.event', ev), field('logs.level', lv), field('logs.job', job),
    button('logs.apply', () => { offset = 0; load(); })), box);
  await load();
}

// --- personal settings ------------------------------------------------------------------------------
export async function me(main, state) {
  const cur = h('input', { type: 'password', autocomplete: 'current-password' });
  const p1 = h('input', { type: 'password', autocomplete: 'new-password' });
  const p2 = h('input', { type: 'password', autocomplete: 'new-password' });
  page(main, 'me.title', null,
    state.user.must_change_password ? h('p', { class: 'notice warn' }, t('me.must_change')) : null,
    kv([['me.username', state.user.username], ['me.role', t('role.' + state.user.role)]]),
    h('form', { class: 'form', onsubmit: async (e) => {
      e.preventDefault();
      const submit = e.submitter;
      if (submit) submit.disabled = true;
      try {
        await post('/api/me/password', { current: cur.value, password: p1.value, password_confirm: p2.value });
        toast(t('me.password_changed'), 'ok');
        cur.value = p1.value = p2.value = '';
        window.dispatchEvent(new CustomEvent('po:password-changed'));
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        if (submit?.isConnected) submit.disabled = false;
      }
    } }, h('h2', {}, t('user.change_password')), field('me.current', cur), field('me.new', p1, 'setup.password_rules'), field('me.confirm', p2),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, t('common.save')))));
}
