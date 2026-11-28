// Review queue and review screen (media on one side, all target fields on the other).
import { h, mount, button, field, table, toast, badge } from '../dom.js';
import { t, fmt } from '../i18n.js';
import { get, post } from '../api.js';
import { tablePaging, waitForJob, poll, stepGuide, analysisSummary, pageFilters } from '../components.js';

const KINDS = ['', 'meta_conflict', 'no_date', 'date_utc', 'move_conflict', 'verify_mismatch', 'reconcile_missing', 'reconcile_modified', 'reconcile_new', 'reconcile_moved'];
const previewRequests = new Map();

function requestPreview(id) {
  if (!previewRequests.has(id)) {
    previewRequests.set(id, get(`/api/review/${id}/preview`, { raw: true }).then(async (response) => {
      if (response.status === 202) return { pending: true };
      if (!response.ok) throw new Error(t('review.preview_error', { status: response.status }));
      const blob = await response.blob();
      return { url: URL.createObjectURL(blob), type: blob.type };
    }));
  }
  return previewRequests.get(id);
}

export async function list(main, state, options = {}) {
  let offset = 0;
  const filters = pageFilters({ status: 'pending' });
  const status = h('select', {}, ['all', 'pending', 'decided'].map((s) => h('option', { value: s }, t('review.status.' + s))));
  const kind = h('select', {}, KINDS.map((k) => h('option', { value: k }, k ? t('review.kind.' + k) : t('review.all_kinds'))));
  status.value = filters.get('status');
  kind.value = filters.get('kind');
  const box = h('div', {});
  const flowStatus = await get('/api/library/status');
  const notice = flowStatus.exists
    ? (flowStatus.stale ? h('p', { class: 'notice warn' }, t('analysis.stale'), ' ',
      h('a', { href: '#/analyze' }, t('analysis.update'))) : null)
    : h('p', { class: 'notice warn' }, t('analysis.empty'), ' ',
      h('a', { class: 'btn', href: '#/analyze' }, t('scan.analyze_library')));
  const counts = h('p', { class: 'summary' });
  const population = h('div', {});
  const start = h('a', { class: 'btn primary start-review', hidden: true }, t('meta.start_review'));
  const pagination = tablePaging(state.user.id, 'review', (o) => { offset = o; load(); });
  const load = async () => {
    if (!flowStatus.exists || flowStatus.empty) {
      counts.hidden = true;
      mount(box, h('p', { class: 'notice' }, t('analysis.no_results')));
      return;
    }
    const q = new URLSearchParams({ status: status.value, offset, limit: pagination.query() });
    if (kind.value) q.set('kind', kind.value);
    const d = await get('/api/review?' + q);
    if (!await pagination.check(d.total, d.items.length)) return;
    counts.textContent = reviewCounts(d.review_counts);
    const first = await get('/api/review?status=pending&limit=10' + (kind.value ? '&kind=' + encodeURIComponent(kind.value) : ''));
    start.hidden = !first.items.length;
    if (first.items.length) start.href = '#/metadata/review/' + first.items[0].id;
    if (!options.embedded) mount(population, analysisSummary(d.population));
    mount(box, d.items.length ? table(['review.col_kind', 'review.col_file', 'review.col_folder', 'review.col_date',
      'review.col_decision', ''],
      d.items.map((r) => h('tr', {}, h('td', {}, badge(t('review.kind.' + r.kind), 'warn')),
        h('td', {}, r.file ? h('code', {}, r.file.name) : h('code', {}, r.details.path || '')),
        h('td', {}, r.file ? h('code', {}, r.file.folder) : ''),
        h('td', {}, r.file ? fmt.localDate(r.file.chosen_date) : ''),
        h('td', { class: 'review-decision' }, r.status === 'skipped' && r.skipped_at ?
          h('span', { class: 'muted' }, t('review.postponed_on', { date: fmt.date(r.skipped_at) })) : r.decision_kind ? [
          t('review.decision.' + r.decision_kind), r.decision?.date ? ' · ' + fmt.localDate(r.decision.date) : '',
        ] : '\u2014'),
        h('td', {}, h('a', { class: 'btn small', href: '#/metadata/review/' + r.id }, t('review.open')))))) : h('p', { class: 'muted' }, t('review.empty')),
    pagination.render(d.total, offset));
  };
  const changeFilters = () => { filters.save({ status: status.value, kind: kind.value }); offset = 0; load(); };
  status.addEventListener('change', changeFilters);
  kind.addEventListener('change', changeFilters);
  mount(main, options.embedded ? null : [
    h('h1', { class: 'sr-only' }, t('review.title')), stepGuide(2, flowStatus), h('p', { class: 'intro' }, t('review.intro')), notice, counts],
    population, h('div', { class: 'filters' }, field('review.filter_status', status), field('review.filter_kind', kind), start,
      button('logs.clear', () => { status.value = 'pending'; kind.value = ''; changeFilters(); })), box,
    options.embedded ? null : [h('h2', {}, t('review.write_title')), h('p', { class: 'hint' }, t('review.write_help'))],
    !options.embedded && flowStatus.exists && h('div', { class: 'actions' }, button('review.write_plan', async (event) => {
      event.currentTarget.disabled = true;
      try { const r = await post('/api/review/plan'); toast(t('job.started', { id: r.job_id }), 'ok'); const j = await waitForJob(r.job_id); if (j.status === 'completed') location.hash = '#/plans/' + r.job_id; } catch (e) { toast(e.message, 'error'); }
      finally { if (event.currentTarget?.isConnected) event.currentTarget.disabled = false; }
    }, 'primary')));
  await load();
}

export function reviewCounts(counts = {}) {
  return t('review.live_counts', { total: counts.total || 0, decided: counts.decided || 0, pending: counts.pending || 0 }) +
    (counts.needs_recheck ? ' · ' + t('review.recheck_count', { count: counts.needs_recheck }) : '');
}

function mediaView(reviewId, f) {
  const box = h('div', { class: 'media' }, h('p', { class: 'muted' }, t('review.loading_preview')));
  const show = async () => {
    let preview;
    try { preview = await requestPreview(reviewId); }
    catch (e) { stop(); mount(box, h('p', { class: 'notice error' }, e.message)); return; }
    if (preview.pending) {
      previewRequests.delete(reviewId);
      mount(box, h('p', { class: 'muted' }, t('review.generating_preview')));
      return;
    }
    stop();
    if (f.media_kind === 'video') {
      const v = h('video', { src: preview.url, controls: true, preload: 'metadata', playsinline: true });
      v.addEventListener('error', () => box.appendChild(h('p', { class: 'notice warn' }, t('review.cannot_play'))));
      mount(box, v);
    } else {
      mount(box, h('img', { src: preview.url, alt: f.name, loading: 'lazy' }));
    }
    const enlarge = button('review.enlarge', () => {
      const previous = document.activeElement;
      const previewMedia = f.media_kind === 'video'
        ? h('video', { src: preview.url, controls: true, playsinline: true })
        : h('img', { src: preview.url, alt: f.name });
      const dialog = h('dialog', { class: 'preview-dialog', 'aria-label': t('review.enlarge') }, previewMedia,
        button('common.close', () => dialog.close()));
      dialog.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dialog.close(); }
      });
      document.body.appendChild(dialog);
      dialog.addEventListener('close', () => { dialog.remove(); if (previous?.isConnected) previous.focus(); });
      dialog.showModal();
    }, 'small');
    box.appendChild(enlarge);
    box.querySelector('img')?.addEventListener('click', () => enlarge.click());
  };
  const stop = poll(show, 2500);
  return { el: box, stop, destroy: () => {
    const current = previewRequests.get(reviewId);
    if (current) current.then((value) => { if (value?.url) URL.revokeObjectURL(value.url); });
    previewRequests.delete(reviewId);
  } };
}

function candidateList(cands, fieldName) {
  if (!cands || !cands.length) return h('span', { class: 'missing' }, t('review.missing'));
  return h('ul', { class: 'cands' }, cands.map((c) => h('li', {}, h('code', {}, (fieldName === 'dimensions' ? fmt.dimensions(c.value) : wallDate(c.value)) + (c.offset ? ' ' + c.offset : '')), ' ',
    h('span', { class: 'muted small' }, c.tag + ' · ' + (c.source === 'file' ? t('review.source_file') : c.source) + (c.semantic === 'utc' ? ' · ' + t('review.utc') : '')))));
}

function wallDate(value) {
  if (!value) return t('common.na');
  return /^\d{4}-\d{2}-\d{2}[T ]/.test(String(value)) ? String(value).replace('T', ' ').slice(0, 19) : String(value);
}

export async function item(main, state, [id]) {
  requestPreview(id).catch(() => {});
  const r = await get('/api/review/' + id);
  if (r.next_id) requestPreview(r.next_id).catch(() => {});
  const f = r.file;
  const inputs = {};
  const dateChoice = h('select', {}, h('option', { value: '' }, t('review.date_keep')));
  const organizingDate = h('input', { type: 'text', class: 'organizing-date', inputmode: 'text',
    placeholder: t('review.example_date'), 'aria-label': t('review.date_choice') });
  organizingDate.value = r.decision?.date ? wallDate(r.decision.date) : '';
  organizingDate.addEventListener('input', () => {
    dateChoice.value = '';
    selectedProposal = null;
    selectedProposalField = null;
    for (const key of ['datetime_original', 'create_date']) if (inputs[key]) inputs[key].value = '';
  });
  let selectedProposal = null;
  let selectedProposalField = null;
  const allDates = [];
  if (r.date_candidates) {
    for (const c of r.date_candidates) allDates.push({ ...c, label: c.tag });
  } else for (const row of r.fields || []) {
    if (row.field === 'datetime_original' || row.field === 'create_date') for (const c of row.candidates) allDates.push({ ...c, source: 'exif', label: c.tag });
  }
  for (const p of r.proposals) allDates.push({ ...p, source: p.source || p.origin, label: t('review.origin.' + p.origin) });
  for (const d of allDates) dateChoice.appendChild(h('option', { value: d.value }, wallDate(d.value) + ' — ' + d.label +
    (d.timezone ? ' · ' + d.timezone : '')));
  const rows = (r.fields || []).map((row) => {
    let editor = null;
    if (row.writable && !row.present) {
      const isDate = row.field === 'datetime_original' || row.field === 'create_date';
      const inp = h('input', { type: 'text', class: isDate ? 'date-input' : '', 'aria-label': t('field.' + row.field),
        placeholder: t('review.example_' + (isDate ? 'date' : row.field)) });
      const prop = r.proposals.find((p) => p.field === row.field);
      if (prop) inp.dataset.proposal = prop.value;
      inputs[row.field] = inp;
      inp.addEventListener('input', () => {
        if (selectedProposalField === row.field) {
          selectedProposal = null;
          selectedProposalField = null;
        }
      });
      editor = inp;
    }
    const proposals = r.proposals.filter((p) => p.field === row.field).map((p) => h('div', { class: 'proposal small' },
      badge(t('review.proposal'), 'info'), ' ', h('code', {}, wallDate(p.value)), ' ', t('review.origin.' + p.origin),
      p.timezone ? ' · ' + p.timezone : '', ' ',
      inputs[row.field] ? h('button', { type: 'button', class: 'link', onclick: () => {
        inputs[row.field].value = wallDate(p.value);
        dateChoice.value = '';
        organizingDate.value = '';
        selectedProposal = { source: p.source || p.origin, tag: p.tag };
        selectedProposalField = row.field;
      } }, t('review.use_proposal')) : null));
    return h('tr', { class: row.present ? '' : 'is-missing' }, h('th', { scope: 'row' }, t('field.' + row.field)),
      h('td', {}, candidateList(row.candidates, row.field), row.fill ? h('div', { class: 'small' }, badge(t('review.auto_fill'), 'ok'), ' ', h('code', {}, String(row.fill.value)), ' ← ', row.fill.from) : null, proposals),
      h('td', {}, editor || (row.present ? h('span', { class: 'muted small' }, t('review.present_readonly')) : h('span', { class: 'muted small' }, t('review.not_writable')))));
  });
  dateChoice.addEventListener('change', () => {
    const choice = allDates[dateChoice.selectedIndex - 1];
    selectedProposal = choice ? { source: choice.source, tag: choice.tag } : null;
    selectedProposalField = null;
    organizingDate.value = choice ? wallDate(choice.value) : '';
    if (!dateChoice.value) return;
    for (const key of ['datetime_original', 'create_date']) {
      if (inputs[key]) inputs[key].value = '';
    }
  });
  const scope = h('select', {}, ['item', 'folder', 'group'].map((s) => h('option', { value: s }, t('review.scope.' + s))));
  let saving = false;
  const decide = async (action, next = false, dateOverride = null, proposalOverride = null) => {
    if (saving) return;
    saving = true;
    const fields = {};
    if (!dateOverride) for (const [k, el] of Object.entries(inputs)) if (el.value) fields[k] = el.value;
    try {
      const date = dateOverride || organizingDate.value || dateChoice.value;
      const res = await post('/api/review/' + id, { action, fields, date: date ? date.replace(' ', 'T') : undefined,
        proposal: dateOverride ? proposalOverride : selectedProposal, scope: scope.value });
      toast(t(action === 'skip' ? 'review.postponed' : 'review.saved', { count: res.updated }), 'ok');
      if (next) location.hash = res.next_id ? '#/metadata/review/' + res.next_id : '#/metadata/review';
      else {
        if (media) { media.stop(); media.destroy(); }
        state.view = await item(main, state, [id]);
      }
    } catch (e) { toast(e.message, 'error'); }
    finally { saving = false; }
  };
  const media = f ? mediaView(Number(id), f) : null;
  mount(main,
    h('div', { class: 'review-head' }, h('a', { class: 'back-link', href: '#/metadata/review' }, '← ',
      t('common.back_to', { page: t('nav.metadata') })), ' ', badge(t('review.kind.' + r.kind), 'warn'), ' ',
      r.status === 'skipped' ? null : badge(t('review.status.' + r.status), ''),
      r.status === 'skipped' && r.skipped_at ? h('span', { class: 'muted' }, t('review.postponed_on', { date: fmt.date(r.skipped_at) })) : null),
    h('div', { class: 'review' },
      h('div', { class: 'review-side' },
        r.recheck_reason ? h('div', { class: 'notice warn recheck-notice' },
          t('review.recheck_notice', { stored: wallDate(r.decision?.date), timezone: r.library_timezone,
            suggested: wallDate(r.recheck_proposal?.value) }),
          h('div', { class: 'actions' },
            button('review.accept', () => decide('save', false, r.recheck_proposal.value,
              { source: 'filesystem_mtime' }), 'primary'),
            button('review.accept_next', () => decide('save', true, r.recheck_proposal.value,
              { source: 'filesystem_mtime' })),
            button('review.keep_stored', () => decide('save', false, r.decision.date)),
            button('review.keep_next', () => decide('save', true, r.decision.date)))) : null,
        h('div', { class: 'review-controls' },
          field('review.date_choice', organizingDate, 'review.date_format'),
          field('review.proposal', dateChoice),
          field('review.apply_scope', scope),
          h('div', { class: 'actions review-actions' },
            button('common.save', () => decide('save'), 'primary'),
            button('review.save_next', () => decide('save', true)), button('review.skip', () => decide('skip', true)),
            button('review.leave', () => decide('keep')),
            button('review.keep_next', () => decide('keep', true)),
            r.status === 'decided' && !r.applied ? button('review.reopen', async () => { try { await post(`/api/review/${id}/reopen`); location.reload(); } catch (e) { toast(e.message, 'error'); } }) : null),
          h('p', { class: 'hint' }, t('review.empty_stays_empty')),
          h('p', { class: 'hint' }, t('review.date_choice_hint'))),
        f ? h('dl', { class: 'kv' }, h('dt', {}, t('review.file_name')), h('dd', {}, h('code', {}, f.name)),
          h('dt', {}, t('review.file_path')), h('dd', {}, h('code', {}, f.path)),
          h('dt', {}, t('review.file_origin')), h('dd', {}, h('code', {}, f.origin)),
          h('dt', {}, t('review.chosen_date')), h('dd', {}, fmt.localDate(f.chosen_date), f.chosen_date_source ? h('span', { class: 'muted small' }, ' · ' + f.chosen_date_source) : null,
            f.date_flags === 'utc_ambiguous' ? h('div', { class: 'notice warn small' }, t('review.utc_ambiguous')) : null),
          f.duration ? [h('dt', {}, t('field.duration')), h('dd', {}, fmt.number(f.duration, { maximumFractionDigits: 1 }))] : null) : null,
        Object.keys(r.details).length ? h('dl', { class: 'details' },
          Object.entries(r.details).map(([key, value]) => [
            h('dt', {}, t('review.detail_label', { name: key })),
            h('dd', {}, typeof value === 'object' ? t('review.detail_values', { count: Object.keys(value || {}).length }) : String(value)),
          ])) : null,
        rows.length ? table(['review.col_field', 'review.col_values', 'review.col_edit'], rows) : null),
      h('aside', { class: 'review-preview' },
        media ? media.el : h('div', { class: 'media' }, h('p', { class: 'muted' }, t('review.no_file'))),
        f ? h('p', { class: 'preview-caption' }, h('code', {}, f.name), ' ',
          r.fields?.find((row) => row.field === 'dimensions')?.candidates?.map((candidate) => fmt.dimensions(candidate.value)).join(' · ')) : null)));
  return { destroy: () => { document.querySelector('.preview-dialog')?.close(); if (media) { media.stop(); media.destroy(); } } };
}
