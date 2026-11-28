// Export / import / direct migration between databases (admin only).
import { h, mount, button, field, table, modal, toast, instant } from '../dom.js';
import { t, fmt } from '../i18n.js';
import { get, post, del, uploadFile } from '../api.js';
import { jobList, kv, poll } from '../components.js';
import { adminNav, dbForm } from './admin.js';

export async function transfer(main, state) {
  let mode = 'export';
  const body = h('div', {});
  const archivesBox = h('div', {});
  const jobsBox = h('div', {});
  const tabs = h('div', { class: 'tabs', role: 'tablist' });

  const loadArchives = async () => {
    const a = await get('/api/admin/archives');
    const rows = (where) => a[where].map((x) => h('tr', {}, h('td', {}, h('code', {}, x.name)), h('td', {}, fmt.bytes(x.bytes)), h('td', {}, instant(new Date(x.modified * 1000).toISOString())),
      h('td', { class: 'actions' },
        where === 'exports' ? h('a', { class: 'btn small', href: '/api/admin/archives/exports/' + encodeURIComponent(x.name), download: x.name }, t('transfer.download')) : null,
        button('common.delete', async () => {
          if (!await modal({ titleKey: 'transfer.delete_archive', body: h('p', {}, t('transfer.delete_archive_confirm', { name: x.name })), danger: true, confirmKey: 'common.delete' })) return;
          try { await del(`/api/admin/archives/${where}/${encodeURIComponent(x.name)}?confirm=1`); loadArchives(); } catch (e) { toast(e.message, 'error'); }
        }, 'small danger-outline'))));
    mount(archivesBox, h('h3', {}, t('transfer.exports_dir')), a.exports.length ? table(['transfer.col_name', 'transfer.col_size', 'transfer.col_date', ''], rows('exports')) : h('p', { class: 'muted' }, t('transfer.none')),
      h('h3', {}, t('transfer.imports_dir')), a.imports.length ? table(['transfer.col_name', 'transfer.col_size', 'transfer.col_date', ''], rows('imports')) : h('p', { class: 'muted' }, t('transfer.none')));
    return a;
  };

  const passField = () => h('input', { type: 'password', autocomplete: 'new-password' });

  const preview = async (payload) => {
    try {
      const p = await post('/api/admin/transfer/preview', payload);
      const src = p.source;
      const el = h('div', { class: 'preview', dataset: (p.errors || []).length ? { blocked: '1' } : {} }, h('h3', {}, t('transfer.preview')),
        src ? [kv([['db.db_id', src.db_id], ['db.schema_version', String(src.schema_version)], ['transfer.rows', fmt.number(src.rows)],
          ['transfer.est_size', fmt.bytes(src.est_archive_bytes)], ['transfer.est_time', fmt.duration(src.est_seconds)]]),
        table(['transfer.col_table', 'transfer.rows'], src.tables.map((x) => h('tr', {}, h('td', {}, h('code', {}, x.name)), h('td', {}, fmt.number(x.rows)))))] : null,
        p.archive ? kv([['transfer.archive', p.archive.name], ['transfer.app_version', p.archive.app_version], ['db.schema_version', String(p.archive.schema_version)],
          ['transfer.source_db', p.archive.source_db_id + ' (' + t('db.type.' + p.archive.source_type) + ')'], ['transfer.created', instant(p.archive.created_at)],
          ['transfer.est_time', fmt.duration(p.est_seconds)]]) : null,
        p.destination ? h('div', {}, h('h4', {}, t('transfer.destination')), kv([['db.structure', t('db.structure.' + (p.destination.database_missing ? 'absent' : p.destination.structure || 'absent'))],
          ['db.files', String(p.destination.files ?? 0)]])) : null,
        (p.warnings || []).map((w) => h('p', { class: 'notice warn' }, t('transfer.warn.' + w))),
        (p.errors || []).map((w) => h('p', { class: 'notice error' }, t('error.' + w))));
      return el;
    } catch (e) { toast(e.message, 'error'); return null; }
  };

  const confirmStart = async (payload, previewEl) => {
    if (!previewEl || (previewEl.dataset && previewEl.dataset.blocked)) return;
    if (!await modal({ titleKey: 'transfer.confirm_title', body: h('div', {}, h('p', {}, t('transfer.confirm_' + payload.mode)), h('p', { class: 'hint' }, t('transfer.source_untouched'))),
      requireCheckKey: 'transfer.confirm_check', confirmKey: 'transfer.start' })) return;
    try { const r = await post('/api/admin/transfer/start', { ...payload, confirm: true }); toast(t('job.started', { id: r.job_id }), 'ok'); } catch (e) { toast(e.message, 'error'); }
  };

  const renderMode = async () => {
    mount(tabs, ['export', 'import', 'migrate'].map((m) => h('button', { type: 'button', role: 'tab', class: 'tab' + (m === mode ? ' active' : ''), 'aria-selected': String(m === mode),
      onclick: () => { mode = m; renderMode(); } }, t('transfer.mode.' + m))));
    const out = h('div', {});
    if (mode === 'export') {
      const pass = passField(); const pass2 = passField();
      mount(body, h('p', { class: 'hint' }, t('transfer.export_help')), field('transfer.passphrase', pass, 'transfer.passphrase_hint'), field('transfer.passphrase_confirm', pass2),
        h('div', { class: 'actions' }, button('transfer.preview_btn', async () => {
          if (pass.value !== pass2.value) { toast(t('error.password_mismatch'), 'error'); return; }
          const pv = await preview({ mode: 'export' }); if (!pv) return;
          mount(out, pv, h('div', { class: 'actions' }, button('transfer.start', () => confirmStart({ mode: 'export', passphrase: pass.value }, pv), 'primary')));
        })), out);
    } else if (mode === 'import') {
      const a = await loadArchives();
      const sel = h('select', {}, a.imports.map((x) => h('option', { value: 'imports:' + x.name }, '/state/imports/' + x.name)), a.exports.map((x) => h('option', { value: 'exports:' + x.name }, '/state/exports/' + x.name)));
      const file = h('input', { type: 'file', accept: '.tcarc' });
      const prog = h('progress', { max: '100', value: '0', hidden: true });
      const pass = passField();
      const dest = dbForm();
      mount(body, h('p', { class: 'hint' }, t('transfer.import_help')),
        h('div', { class: 'form' }, field('transfer.upload', file, 'transfer.upload_hint'), prog, button('transfer.upload_btn', async () => {
          if (!file.files[0]) return;
          prog.hidden = false;
          try { await uploadFile('/api/admin/archives/imports/' + encodeURIComponent(file.files[0].name), file.files[0], (l, tot) => { prog.value = String(Math.round((100 * l) / tot)); }); toast(t('common.saved'), 'ok'); renderMode(); } catch (e) { toast(e.message, 'error'); }
        })),
        field('transfer.archive', sel), field('transfer.passphrase', pass), h('h3', {}, t('transfer.destination')), h('p', { class: 'hint' }, t('transfer.destination_empty')), dest.el,
        h('div', { class: 'actions' }, button('transfer.preview_btn', async () => {
          const [location, archive] = sel.value.split(/:(.*)/s);
          const payload = { mode: 'import', archive, location, passphrase: pass.value, database: dest.value() };
          const pv = await preview(payload); if (!pv) return;
          mount(out, pv, h('div', { class: 'actions' }, button('transfer.start', () => confirmStart(payload, pv), 'primary')));
        })), out);
    } else {
      const dest = dbForm();
      mount(body, h('p', { class: 'hint' }, t('transfer.migrate_help')), h('h3', {}, t('transfer.destination')), h('p', { class: 'hint' }, t('transfer.destination_empty')), dest.el,
        h('div', { class: 'actions' }, button('transfer.preview_btn', async () => {
          const payload = { mode: 'migrate', database: dest.value() };
          const pv = await preview(payload); if (!pv) return;
          mount(out, pv, h('div', { class: 'actions' }, button('transfer.start', () => confirmStart(payload, pv), 'primary')));
        })), out);
    }
  };

  // transfer jobs with resume / purge / switch actions
  const tj = async () => {
    const d = await get('/api/jobs?limit=20');
    const items = d.items.filter((j) => ['export', 'import', 'migrate'].includes(j.kind));
    mount(jobsBox, items.length ? table(['transfer.col_job', 'transfer.col_status', 'transfer.col_progress', ''], items.map((j) => h('tr', {},
      h('td', {}, t('transfer.mode.' + j.kind), ' #' + j.id), h('td', {}, t('job.status.' + j.status)),
      h('td', {}, j.progress_total ? fmt.percent((100 * j.progress_done) / j.progress_total) : fmt.number(j.progress_done), j.phase ? h('span', { class: 'muted small' }, ' ' + j.phase) : null),
      h('td', { class: 'actions' },
        ['paused', 'interrupted', 'failed'].includes(j.status) ? button('job.resume', async () => {
          const pass = passField();
          if (!await modal({ titleKey: 'transfer.resume', body: j.kind === 'migrate' ? h('p', {}, t('transfer.resume_text')) : field('transfer.passphrase', pass), confirmKey: 'job.resume' })) return;
          try { await post(`/api/admin/transfer/${j.id}/resume`, { passphrase: pass.value }); } catch (e) { toast(e.message, 'error'); }
        }) : null,
        ['failed', 'cancelled', 'paused', 'interrupted'].includes(j.status) && j.kind !== 'export' ? button('transfer.purge', async () => {
          if (!await modal({ titleKey: 'transfer.purge', body: h('p', {}, t('transfer.purge_text')), danger: true, requireCheckKey: 'transfer.purge_check', confirmKey: 'transfer.purge' })) return;
          try { await post(`/api/admin/transfer/${j.id}/purge`, { confirm: true }); toast(t('common.saved'), 'ok'); } catch (e) { toast(e.message, 'error'); }
        }, 'danger-outline') : null,
        j.status === 'completed' && j.kind !== 'export' ? button('transfer.switch_to', async () => {
          if (!await modal({ titleKey: 'db.switch_title', body: h('ul', {}, ['db.switch_point_pause', 'db.switch_point_old_kept', 'db.switch_point_reconcile', 'db.switch_point_rollback'].map((k) => h('li', {}, t(k)))), requireCheckKey: 'db.switch_check', confirmKey: 'db.switch' })) return;
          try { const r = await post('/api/admin/db/switch', { from_transfer: j.id, confirm: true, hash_mode: 'sample' }); toast(t('db.switched', { job: r.reconcile_job_id }), 'ok'); } catch (e) { toast(e.message, 'error'); }
        }, 'primary') : null,
        j.result && j.result.report ? h('details', {}, h('summary', {}, t('transfer.report')), table(['transfer.col_table', 'transfer.rows', 'transfer.checksum'],
          j.result.report.tables.map((r) => h('tr', {}, h('td', {}, r.name), h('td', {}, fmt.number(r.rows_dest) + ' / ' + fmt.number(r.rows_source)), h('td', {}, r.ok ? '✓' : '✗'))))) : null)))) : h('p', { class: 'muted' }, t('transfer.none')));
  };

  mount(main, h('h1', {}, t('nav.admin')), adminNav(state, 'transfer'), h('h2', {}, t('admin.transfer')), h('p', { class: 'intro' }, t('transfer.intro')),
    tabs, body, h('h2', {}, t('transfer.jobs')), jobsBox, h('h2', {}, t('transfer.archives')), archivesBox);
  await renderMode();
  await loadArchives();
  return { destroy: poll(tj, 2000) };
}

export { jobList };
