// Review queue and review screen (media on one side, all target fields on the other).
import { h, mount, button, field, table, toast, badge } from '../dom.js';
import { t, fmt } from '../i18n.js';
import { get, post } from '../api.js';
import { pager, waitForJob } from '../components.js';

const KINDS = ['', 'meta_conflict', 'no_date', 'date_utc', 'move_conflict', 'verify_mismatch', 'reconcile_missing', 'reconcile_modified', 'reconcile_new', 'reconcile_moved'];

export async function list(main) {
  let offset = 0;
  const status = h('select', {}, ['pending', 'saved', 'skipped', 'kept'].map((s) => h('option', { value: s }, t('review.status.' + s))));
  const kind = h('select', {}, KINDS.map((k) => h('option', { value: k }, k ? t('review.kind.' + k) : t('review.all_kinds'))));
  const box = h('div', {});
  const counts = h('p', { class: 'summary' });
  const load = async () => {
    const q = new URLSearchParams({ status: status.value, offset, limit: 50 });
    if (kind.value) q.set('kind', kind.value);
    const d = await get('/api/review?' + q);
    counts.textContent = t('review.counts', { pending: d.counts.pending || 0, saved: d.counts.saved || 0, skipped: d.counts.skipped || 0, kept: d.counts.kept || 0 });
    mount(box, d.items.length ? table(['review.col_kind', 'review.col_file', 'review.col_folder', 'review.col_date', ''],
      d.items.map((r) => h('tr', {}, h('td', {}, badge(t('review.kind.' + r.kind), 'warn')),
        h('td', {}, r.file ? h('code', {}, r.file.name) : h('code', {}, r.details.path || '')),
        h('td', {}, r.file ? h('code', {}, r.file.folder) : ''),
        h('td', {}, r.file ? fmt.localDate(r.file.chosen_date) : ''),
        h('td', {}, h('a', { class: 'btn small', href: '#/review/' + r.id }, t('review.open')))))) : h('p', { class: 'muted' }, t('review.empty')),
    pager(d.total, offset, 50, (o) => { offset = o; load(); }));
  };
  status.addEventListener('change', () => { offset = 0; load(); });
  kind.addEventListener('change', () => { offset = 0; load(); });
  mount(main, h('h1', {}, t('review.title')), h('p', { class: 'intro' }, t('review.intro')), counts,
    h('div', { class: 'filters' }, field('review.filter_status', status), field('review.filter_kind', kind)), box,
    h('h2', {}, t('review.write_title')), h('p', { class: 'hint' }, t('review.write_help')),
    h('div', { class: 'actions' }, button('review.write_plan', async () => {
      try { const r = await post('/api/review/plan'); toast(t('job.started', { id: r.job_id }), 'ok'); const j = await waitForJob(r.job_id); if (j.status === 'completed') location.hash = '#/plans/' + r.job_id; } catch (e) { toast(e.message, 'error'); }
    }, 'primary')));
  await load();
}

function mediaView(f) {
  const box = h('div', { class: 'media' }, h('p', { class: 'muted' }, t('review.loading_preview')));
  const show = async () => {
    let probe;
    try { probe = await get(`/api/files/${f.id}/preview?probe=1`); } catch (e) { mount(box, h('p', { class: 'notice error' }, e.message)); return; }
    if (probe.status === 'pending') {
      mount(box, h('p', { class: 'muted' }, t('review.generating_preview')));
      setTimeout(show, 2500);
      return;
    }
    const src = `/api/files/${f.id}/preview`;
    if (f.media_kind === 'video') {
      const v = h('video', { src, controls: true, preload: 'metadata', playsinline: true });
      v.addEventListener('error', () => box.appendChild(h('p', { class: 'notice warn' }, t('review.cannot_play'))));
      mount(box, v);
    } else {
      mount(box, h('img', { src, alt: f.name, loading: 'lazy' }));
    }
  };
  show();
  return box;
}

function candidateList(cands) {
  if (!cands || !cands.length) return h('span', { class: 'missing' }, t('review.missing'));
  return h('ul', { class: 'cands' }, cands.map((c) => h('li', {}, h('code', {}, c.value + (c.offset ? ' ' + c.offset : '')), ' ',
    h('span', { class: 'muted small' }, c.tag + ' · ' + (c.source === 'file' ? t('review.source_file') : c.source) + (c.semantic === 'utc' ? ' · ' + t('review.utc') : '')))));
}

export async function item(main, state, [id]) {
  const r = await get('/api/review/' + id);
  const f = r.file;
  const inputs = {};
  const dateChoice = h('select', {}, h('option', { value: '' }, t('review.date_keep')));
  const allDates = [];
  for (const row of r.fields || []) {
    if (row.field === 'datetime_original' || row.field === 'create_date') for (const c of row.candidates) allDates.push({ value: c.value, label: c.tag });
  }
  for (const p of r.proposals) allDates.push({ value: p.value, label: t('review.origin.' + p.origin) });
  for (const d of allDates) dateChoice.appendChild(h('option', { value: d.value }, fmt.localDate(d.value) + ' — ' + d.label));
  const rows = (r.fields || []).map((row) => {
    let editor = null;
    if (row.writable && !row.present) {
      const typ = row.field === 'datetime_original' || row.field === 'create_date' ? 'datetime-local' : 'text';
      const inp = h('input', { type: typ, step: typ === 'datetime-local' ? '1' : undefined, 'aria-label': t('field.' + row.field),
        placeholder: row.field === 'gps' ? '45.464200, 9.190000' : row.field === 'offset' ? '+01:00' : undefined });
      const prop = r.proposals.find((p) => p.field === row.field);
      if (prop) inp.dataset.proposal = prop.value;
      inputs[row.field] = inp;
      editor = inp;
    }
    const proposals = r.proposals.filter((p) => p.field === row.field).map((p) => h('div', { class: 'proposal small' },
      badge(t('review.proposal'), 'info'), ' ', h('code', {}, p.value), ' ', t('review.origin.' + p.origin), ' ',
      inputs[row.field] ? h('button', { type: 'button', class: 'link', onclick: () => { inputs[row.field].value = p.value.slice(0, 19); } }, t('review.use_proposal')) : null));
    return h('tr', { class: row.present ? '' : 'is-missing' }, h('th', { scope: 'row' }, t('field.' + row.field)),
      h('td', {}, candidateList(row.candidates), row.fill ? h('div', { class: 'small' }, badge(t('review.auto_fill'), 'ok'), ' ', h('code', {}, String(row.fill.value)), ' ← ', row.fill.from) : null, proposals),
      h('td', {}, editor || (row.present ? h('span', { class: 'muted small' }, t('review.present_readonly')) : h('span', { class: 'muted small' }, t('review.not_writable')))));
  });
  const scope = h('select', {}, ['item', 'folder', 'group'].map((s) => h('option', { value: s }, t('review.scope.' + s))));
  const decide = async (action) => {
    const fields = {};
    for (const [k, el] of Object.entries(inputs)) if (el.value) fields[k] = el.value;
    try {
      const res = await post('/api/review/' + id, { action, fields, date: dateChoice.value || undefined, scope: scope.value });
      toast(t('review.saved', { count: res.updated }), 'ok');
      location.hash = res.next_id ? '#/review/' + res.next_id : '#/review';
    } catch (e) { toast(e.message, 'error'); }
  };
  mount(main,
    h('div', { class: 'review-head' }, h('a', { href: '#/review' }, '← ', t('review.back')), ' ', badge(t('review.kind.' + r.kind), 'warn'), ' ', badge(t('review.status.' + r.status), '')),
    h('div', { class: 'review' },
      f ? mediaView(f) : h('div', { class: 'media' }, h('p', { class: 'muted' }, t('review.no_file'))),
      h('div', { class: 'review-side' },
        f ? h('dl', { class: 'kv' }, h('dt', {}, t('review.file_name')), h('dd', {}, h('code', {}, f.name)),
          h('dt', {}, t('review.file_path')), h('dd', {}, h('code', {}, f.path)),
          h('dt', {}, t('review.file_origin')), h('dd', {}, h('code', {}, f.origin)),
          h('dt', {}, t('review.chosen_date')), h('dd', {}, fmt.localDate(f.chosen_date), f.chosen_date_source ? h('span', { class: 'muted small' }, ' · ' + f.chosen_date_source) : null,
            f.date_flags === 'utc_ambiguous' ? h('div', { class: 'notice warn small' }, t('review.utc_ambiguous')) : null),
          f.duration ? [h('dt', {}, t('field.duration')), h('dd', {}, fmt.number(f.duration, { maximumFractionDigits: 1 }))] : null) : null,
        Object.keys(r.details).length ? h('pre', { class: 'details', dir: 'ltr' }, JSON.stringify(r.details, null, 1)) : null,
        rows.length ? table(['review.col_field', 'review.col_values', 'review.col_edit'], rows) : null,
        field('review.date_choice', dateChoice, 'review.date_choice_hint'),
        field('review.apply_scope', scope),
        h('div', { class: 'actions sticky' },
          button('review.save_next', () => decide('save'), 'primary'), button('review.skip', () => decide('skip')),
          button('review.leave', () => decide('keep')),
          r.status !== 'pending' ? button('review.reopen', async () => { try { await post(`/api/review/${id}/reopen`); location.reload(); } catch (e) { toast(e.message, 'error'); } }) : null),
        h('p', { class: 'hint' }, t('review.empty_stays_empty')))));
}
