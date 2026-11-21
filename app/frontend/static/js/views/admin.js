// Administration: users, groups, sources, settings, database, audit, security.
import { h, mount, button, field, table, modal, toast, badge } from '../dom.js';
import { t, fmt } from '../i18n.js';
import { get, post, patch, del } from '../api.js';
import { folderBrowser, kv, pager } from '../components.js';
import { testResult } from './setup.js';

export function adminNav(state, active) {
  const items = [['users', 'admin.users'], ['groups', 'admin.groups'], ['settings', 'admin.settings'], ['database', 'admin.database']];
  if (state.perms.transfer_database) items.push(['transfer', 'admin.transfer']);
  items.push(['audit', 'admin.audit']);
  if (state.perms.security_settings) items.push(['security', 'admin.security']);
  return [h('p', { class: 'notice warn http-warning', role: 'note' }, t('admin.http_warning')),
    h('nav', { class: 'subnav', 'aria-label': t('nav.admin') }, items.map(([k, label]) => h('a', { href: '#/admin/' + k, 'aria-current': k === active ? 'page' : undefined }, t(label))))];
}

function shell(main, state, active, titleKey, ...body) {
  mount(main, h('h1', {}, t('nav.admin')), adminNav(state, active), h('h2', {}, t(titleKey)), ...body);
}

async function showPassword(pw) {
  await modal({ titleKey: 'admin.generated_password', body: h('div', {}, h('p', {}, t('admin.generated_password_once')), h('pre', { class: 'secret' }, pw)), confirmKey: 'common.close' });
}

// --- sources editor -----------------------------------------------------------------------------
function sourcesEditor(initial) {
  const list = initial.map((s) => ({ ...s }));
  const box = h('div', { class: 'sources-editor' });
  const render = () => mount(box,
    list.length ? table(['admin.src_path', 'admin.src_dest', 'admin.src_label', ''], list.map((s, i) => {
      const dest = h('input', { type: 'text', value: s.dest === s.path ? '' : (s.dest || ''), placeholder: t('admin.src_dest_same') });
      dest.addEventListener('input', () => { s.dest = dest.value; });
      const label = h('input', { type: 'text', value: s.label || '' });
      label.addEventListener('input', () => { s.label = label.value; });
      return h('tr', {}, h('td', {}, h('code', {}, '/data/' + s.path)), h('td', {}, dest), h('td', {}, label),
        h('td', {}, button('common.remove', () => { list.splice(i, 1); render(); }, 'small danger-outline')));
    })) : h('p', { class: 'muted' }, t('admin.no_sources')),
    h('details', { class: 'panel' }, h('summary', {}, t('admin.add_source')), folderBrowser((p) => {
      if (!p) { toast(t('error.source_is_data_root'), 'error'); return; }
      list.push({ path: p, dest: '', label: '' }); render();
    })),
    h('p', { class: 'hint' }, t('admin.sources_rules')));
  render();
  return { el: box, value: () => list.map((s) => ({ id: s.id, path: s.path, dest: s.dest || '', label: s.label || '' })) };
}

// --- users ------------------------------------------------------------------------------------------
export async function users(main, state) {
  const d = await get('/api/admin/users');
  const groupName = (id) => (d.groups.find((g) => g.id === id) || {}).name || id;
  const editable = (u) => state.user.role === 'admin' || u.role === 'user';
  const act = async (fn) => { try { await fn(); toast(t('common.saved'), 'ok'); users(main, state); } catch (e) { toast(e.message, 'error'); } };
  const editUser = async (u) => {
    const role = h('select', { disabled: u.fixed || state.user.role !== 'admin' }, ['user', 'administrator'].map((r) => h('option', { value: r, selected: u.role === r }, t('role.' + r))));
    const groups = h('div', { class: 'checks' }, d.groups.map((g) => h('label', {}, h('input', { type: 'checkbox', value: g.id, checked: u.groups.includes(g.id) }), ' ', g.name)));
    const src = sourcesEditor(u.sources);
    const body = h('div', { class: 'form' }, u.fixed ? null : field('admin.role', role),
      u.role === 'user' ? field('admin.groups', groups) : null, h('h3', {}, t('admin.sources')), src.el);
    if (!await modal({ titleKey: 'admin.edit_user', body, confirmKey: 'common.save' })) return;
    const payload = { groups: [...groups.querySelectorAll('input:checked')].map((i) => i.value) };
    if (!u.fixed && state.user.role === 'admin') payload.role = role.value;
    payload.sources = src.value();
    await act(() => patch('/api/admin/users/' + u.id, payload));
  };

  const rows = d.users.map((u) => h('tr', {},
    h('td', {}, h('strong', {}, u.username), u.fixed ? ' ' : null, u.fixed ? badge(t('admin.fixed'), 'muted') : null),
    h('td', {}, t('role.' + u.role)), h('td', {}, u.active ? badge(t('admin.active'), 'ok') : badge(t('admin.inactive'), 'warn')),
    h('td', {}, u.groups.map(groupName).join(', ')),
    h('td', {}, u.sources.map((s) => h('div', {}, h('code', {}, '/data/' + s.path), s.dest !== s.path ? h('span', { class: 'muted' }, ' → /data/' + s.dest) : null))),
    h('td', { class: 'actions' }, editable(u) ? [
      button('admin.edit', () => editUser(u)),
      !u.fixed ? button(u.active ? 'admin.deactivate' : 'admin.activate', async () => { await act(() => patch('/api/admin/users/' + u.id, { active: !u.active })); }) : null,
      button('admin.reset_password', async () => {
        if (!await modal({ titleKey: 'admin.reset_password', body: h('p', {}, t('admin.reset_password_confirm', { username: u.username })) })) return;
        try { const r = await post(`/api/admin/users/${u.id}/password`); await showPassword(r.generated_password); } catch (e) { toast(e.message, 'error'); }
      }),
      !u.fixed && u.id !== state.user.id ? button('common.delete', async () => {
        if (!await modal({ titleKey: 'admin.delete_user', body: h('p', {}, t('admin.delete_user_confirm', { username: u.username })), danger: true, confirmKey: 'common.delete' })) return;
        await act(() => del('/api/admin/users/' + u.id));
      }, 'danger-outline') : null] : null)));

  // creation form
  const uname = h('input', { type: 'text', autocomplete: 'off' });
  const role = h('select', {}, h('option', { value: 'user' }, t('role.user')), state.user.role === 'admin' ? h('option', { value: 'administrator' }, t('role.administrator')) : null);
  const pw = h('input', { type: 'password', autocomplete: 'new-password', placeholder: t('admin.password_generate_hint') });
  const groupsBox = h('div', { class: 'checks' }, d.groups.map((g) => h('label', {}, h('input', { type: 'checkbox', value: g.id }), ' ', g.name)));
  const src = sourcesEditor([]);
  const create = async () => {
    try {
      const r = await post('/api/admin/users', { username: uname.value.trim(), role: role.value, password: pw.value || undefined,
        groups: [...groupsBox.querySelectorAll('input:checked')].map((i) => i.value), sources: role.value === 'user' ? src.value() : [] });
      if (r.generated_password) await showPassword(r.generated_password);
      toast(t('common.saved'), 'ok');
      users(main, state);
    } catch (e) { toast(e.message, 'error'); }
  };
  shell(main, state, 'users', 'admin.users',
    table(['admin.col_user', 'admin.col_role', 'admin.col_state', 'admin.col_groups', 'admin.col_sources', ''], rows),
    h('details', { class: 'panel' }, h('summary', {}, t('admin.new_user')),
      h('div', { class: 'form' }, field('admin.username', uname, 'admin.username_rules'), field('admin.role', role), field('admin.password', pw, 'setup.password_rules'),
        d.groups.length ? field('admin.groups', groupsBox) : null, h('h3', {}, t('admin.sources')), src.el,
        h('div', { class: 'actions' }, button('admin.create', create, 'primary')))),
    state.perms.reassign_owner ? await ownersPanel() : null);
}

async function ownersPanel() {
  let data;
  try { data = await get('/api/admin/owners'); } catch (e) { return null; }
  const orphans = data.owners.filter((o) => !o.known);
  if (!orphans.length) return null;
  const users = (await get('/api/admin/users')).users;
  return h('details', { class: 'panel' }, h('summary', {}, t('admin.orphans', { count: orphans.length })), h('p', { class: 'hint' }, t('admin.orphans_help')),
    orphans.map((o) => {
      const sel = h('select', {}, users.map((u) => h('option', { value: u.id }, u.username)));
      return h('div', { class: 'row' }, h('code', {}, o.owner_id), ' ', t('admin.orphan_files', { count: o.files }), ' ', sel,
        button('admin.reassign', async () => { try { await post('/api/admin/owners/reassign', { from_owner: o.owner_id, to_user: sel.value }); toast(t('common.saved'), 'ok'); } catch (e) { toast(e.message, 'error'); } }));
    }));
}

// --- groups ------------------------------------------------------------------------------------------
export async function groups(main, state) {
  const d = await get('/api/admin/users');
  const baseUsers = d.users.filter((u) => u.role === 'user');
  const reload = () => groups(main, state);
  const rows = d.groups.map((g) => {
    const members = h('div', { class: 'checks' }, baseUsers.map((u) => h('label', {}, h('input', { type: 'checkbox', value: u.id, checked: u.groups.includes(g.id) }), ' ', u.username)));
    const name = h('input', { type: 'text', value: g.name });
    return h('tr', {}, h('td', {}, name), h('td', {}, g.active ? badge(t('admin.active'), 'ok') : badge(t('admin.inactive'), 'warn')), h('td', {}, members),
      h('td', { class: 'actions' },
        button('common.save', async () => { try { await patch('/api/admin/groups/' + g.id, { name: name.value, members: [...members.querySelectorAll('input:checked')].map((i) => i.value) }); toast(t('common.saved'), 'ok'); reload(); } catch (e) { toast(e.message, 'error'); } }),
        button(g.active ? 'admin.deactivate' : 'admin.activate', async () => { try { await patch('/api/admin/groups/' + g.id, { active: !g.active }); reload(); } catch (e) { toast(e.message, 'error'); } }),
        button('common.delete', async () => { if (await modal({ titleKey: 'admin.delete_group', body: h('p', {}, t('admin.delete_group_confirm', { name: g.name })), danger: true, confirmKey: 'common.delete' })) { try { await del('/api/admin/groups/' + g.id); reload(); } catch (e) { toast(e.message, 'error'); } } }, 'danger-outline')));
  });
  const name = h('input', { type: 'text' });
  shell(main, state, 'groups', 'admin.groups', h('p', { class: 'hint' }, t('admin.groups_help')),
    d.groups.length ? table(['admin.col_group', 'admin.col_state', 'admin.col_members', ''], rows) : h('p', { class: 'muted' }, t('admin.no_groups')),
    h('div', { class: 'form inline' }, field('admin.group_name', name), button('admin.create', async () => { try { await post('/api/admin/groups', { name: name.value }); reload(); } catch (e) { toast(e.message, 'error'); } }, 'primary')));
}

// --- settings ----------------------------------------------------------------------------------------
export async function settings(main, state) {
  const d = await get('/api/admin/settings');
  const s = d.settings;
  const noDate = h('input', { type: 'text', value: s.organize.no_date_folder });
  const dupDir = h('input', { type: 'text', value: s.organize.duplicates_folder });
  const exclude = h('textarea', { rows: '5' }); exclude.value = s.scan.exclude.join('\n');
  const fields = h('div', { class: 'checks' }, ['datetime_original', 'create_date', 'offset', 'gps', 'make', 'model', 'orientation', 'title', 'description']
    .map((f) => h('label', {}, h('input', { type: 'checkbox', value: f, checked: s.metadata.fields.includes(f) }), ' ', t('field.' + f))));
  const height = h('input', { type: 'number', min: '240', max: '2160', value: String(s.previews.transcode_height) });
  const crf = h('input', { type: 'number', min: '18', max: '40', value: String(s.previews.crf) });
  const lang = h('select', { disabled: !state.perms.security_settings }, d.locales.map((l) => h('option', { value: l.code, selected: l.code === s.default_lang }, l.name)));
  const save = async () => {
    const body = { organize: { no_date_folder: noDate.value, duplicates_folder: dupDir.value }, scan: { exclude: exclude.value.split('\n').map((x) => x.trim()).filter(Boolean) },
      metadata: { fields: [...fields.querySelectorAll('input:checked')].map((i) => i.value) }, previews: { transcode_height: Number(height.value), crf: Number(crf.value) } };
    if (state.perms.security_settings) body.default_lang = lang.value;
    try { await patch('/api/admin/settings', body); toast(t('common.saved'), 'ok'); } catch (e) { toast(e.message, 'error'); }
  };
  const sec = d.security;
  const secInputs = sec ? Object.fromEntries(Object.entries(sec).map(([k, v]) => [k, h('input', { type: 'number', value: String(v) })])) : null;
  shell(main, state, 'settings', 'admin.settings',
    h('div', { class: 'form' },
      h('h3', {}, t('settings.organize')), field('settings.no_date_folder', noDate), field('settings.duplicates_folder', dupDir),
      h('h3', {}, t('settings.scan')), field('settings.exclude', exclude, 'settings.exclude_hint'),
      h('h3', {}, t('settings.metadata')), field('settings.fields', fields),
      h('h3', {}, t('settings.previews')), field('settings.preview_height', height), field('settings.preview_crf', crf),
      h('h3', {}, t('settings.instance')), field('settings.default_lang', lang, 'settings.default_lang_hint'),
      h('div', { class: 'actions' }, button('common.save', save, 'primary'))),
    secInputs ? h('div', { class: 'form' }, h('h3', {}, t('settings.security')),
      Object.entries(secInputs).map(([k, el]) => field('settings.sec.' + k, el)),
      h('div', { class: 'actions' }, button('common.save', async () => {
        try { await patch('/api/admin/security', Object.fromEntries(Object.entries(secInputs).map(([k, el]) => [k, Number(el.value)]))); toast(t('common.saved'), 'ok'); } catch (e) { toast(e.message, 'error'); }
      }, 'primary'))) : null);
}

// --- database -----------------------------------------------------------------------------------------
export function dbForm(prefill = {}) {
  const s = { type: prefill.type || 'sqlite', filename: 'twincheck-2.sqlite3', host: '', port: 3306, database: 'twincheck', user: '', password: '' };
  const box = h('div', { class: 'form' });
  const inputs = {};
  const render = () => {
    const type = h('select', {}, ['sqlite', 'mariadb'].map((x) => h('option', { value: x, selected: s.type === x }, t('db.type.' + x))));
    type.addEventListener('change', () => { read(); s.type = type.value; render(); });
    const mk = (k, ty = 'text') => { inputs[k] = h('input', { type: ty, value: s[k] ?? '', autocomplete: 'off' }); return inputs[k]; };
    for (const k of Object.keys(inputs)) delete inputs[k];
    mount(box, field('db.type', type), s.type === 'sqlite' ? field('db.filename', mk('filename'), 'db.filename_hint')
      : [field('db.host', mk('host')), field('db.port', mk('port', 'number')), field('db.database', mk('database')), field('db.user', mk('user')),
        field('db.password', mk('password', 'password')), h('p', { class: 'hint' }, t('db.mariadb_privileges'))]);
  };
  const read = () => { for (const [k, el] of Object.entries(inputs)) s[k] = el.value; };
  render();
  return { el: box, value: () => { read(); return { ...s }; } };
}

export async function database(main, state) {
  const d = await get('/api/admin/db');
  const reload = () => database(main, state);
  const form = dbForm();
  const result = h('div', {});
  const hashMode = h('select', {}, ['none', 'sample', 'full'].map((m) => h('option', { value: m }, t('db.hash_mode.' + m))));
  let lastTest = null; let lastBody = null;
  const doTest = async (body) => {
    try { lastTest = await post('/api/admin/db/test', body); lastBody = body; mount(result, testResult(lastTest), switchButton()); } catch (e) { toast(e.message, 'error'); }
  };
  const switchButton = () => (lastTest && lastTest.reachable && lastTest.compatible !== false && !lastTest.is_current
    ? h('div', { class: 'actions' }, field('db.hash_mode', hashMode), button('db.switch', () => doSwitch(lastBody, lastTest), 'primary')) : null);
  const doSwitch = async (body, info, confirmMigration = false) => {
    const ok = await modal({ titleKey: 'db.switch_title', danger: false, confirmKey: 'db.switch',
      body: h('div', {}, h('ul', {}, ['db.switch_point_pause', 'db.switch_point_old_kept', 'db.switch_point_reconcile', 'db.switch_point_rollback'].map((k) => h('li', {}, t(k)))),
        kv([['db.target', info.label], ['db.structure', t('db.structure.' + (info.database_missing ? 'absent' : info.structure))], ['db.files', String(info.files ?? 0)]])),
      requireCheckKey: 'db.switch_check' });
    if (!ok) return;
    try {
      const r = await post('/api/admin/db/switch', { ...body, confirm: true, hash_mode: hashMode.value, confirm_migration: confirmMigration });
      toast(t('db.switched', { job: r.reconcile_job_id }), 'ok');
      reload();
    } catch (e) {
      if (e.code === 'needs_migration') {
        if (await modal({ titleKey: 'db.migration_title', body: h('p', {}, t('db.migration_text', { from: e.params.version, to: e.params.supported })), requireCheckKey: 'db.backup_done', confirmKey: 'db.migrate' })) {
          return doSwitch(body, info, true);
        }
      } else toast(e.message, 'error');
    }
  };
  const cur = d.current || {};
  shell(main, state, 'database', 'admin.database',
    d.error && d.error.code === 'needs_migration' ? h('div', { class: 'notice warn' }, t('db.current_needs_migration', { from: d.error.version, to: d.error.supported }),
      h('div', { class: 'actions' }, button('db.migrate', async () => {
        if (!await modal({ titleKey: 'db.migration_title', body: h('p', {}, t('db.migration_backup')), requireCheckKey: 'db.backup_done', confirmKey: 'db.migrate' })) return;
        try { await post('/api/admin/db/migrate-schema', { confirm_backup: true }); toast(t('common.saved'), 'ok'); reload(); } catch (e) { toast(e.message, 'error'); }
      }, 'primary'))) : null,
    d.error && d.error.code !== 'needs_migration' ? h('p', { class: 'notice error' }, t('error.' + d.error.code)) : null,
    h('h3', {}, t('db.current')),
    kv([['db.type', cur.type ? t('db.type.' + cur.type) : null], ['db.target', cur.type === 'sqlite' ? cur.path : cur.type ? `${cur.user}@${cur.host}:${cur.port}/${cur.database}` : null],
      ['db.password', cur.password || null], ['db.connected', d.status.connected ? t('common.yes') : t('common.no')], ['db.schema_version', d.status.schema_version ? String(d.status.schema_version) : null],
      ['db.db_id', d.info && d.info.db_id], ['db.files', d.info ? String(d.info.files) : null], ['db.open_jobs', d.info ? String(d.info.open_jobs) : null],
      ['db.last_update', d.info && d.info.last_update]]),
    d.info && d.info.provenance ? h('p', { class: 'hint' }, t('db.provenance', { source: d.info.provenance.source_db_id, date: fmt.date(d.info.provenance.date) })) : null,
    h('h3', {}, t('db.change')), h('p', { class: 'hint' }, t('db.change_help')), form.el,
    h('div', { class: 'actions' }, button('db.test', () => doTest({ database: form.value() }))), result,
    d.previous.length ? [h('h3', {}, t('db.previous')), table(['db.type', 'db.target', ''], d.previous.map((p, i) => h('tr', {},
      h('td', {}, t('db.type.' + p.type)), h('td', {}, h('code', {}, p.type === 'sqlite' ? p.path : `${p.user}@${p.host}:${p.port}/${p.database}`)),
      h('td', {}, button('db.test', () => doTest({ use_previous: i }))))))] : null);
}

// --- audit ------------------------------------------------------------------------------------------------
export async function audit(main, state) {
  let offset = 0;
  const box = h('div', {});
  const load = async () => {
    const d = await get(`/api/admin/audit?offset=${offset}&limit=100`);
    mount(box, table(['logs.col_time', 'audit.col_actor', 'logs.col_event', 'logs.col_message'],
      d.items.map((a) => h('tr', {}, h('td', {}, fmt.date(a.ts)), h('td', {}, a.actor), h('td', {}, h('code', {}, a.event)), h('td', { lang: 'en', dir: 'ltr' }, a.message)))),
    pager(d.total, offset, 100, (o) => { offset = o; load(); }));
  };
  shell(main, state, 'audit', 'admin.audit', h('p', { class: 'hint' }, t('audit.english_note')), box);
  await load();
}

// --- security: key rotation -------------------------------------------------------------------------------
export async function security(main, state) {
  const area = h('div', {});
  const start = async () => {
    if (!await modal({ titleKey: 'security.rotate', body: h('p', {}, t('security.rotate_intro')), confirmKey: 'common.continue' })) return;
    let r;
    try { r = await post('/api/admin/key/prepare'); } catch (e) { toast(e.message, 'error'); return; }
    const confirmInput = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false' });
    mount(area, h('div', { class: 'notice warn' }, h('p', {}, t('security.new_key_once')), h('pre', { class: 'secret' }, 'APP_MASTER_KEY=' + r.new_key),
      h('ol', {}, ['security.step_save', 'security.step_paste', 'security.step_env', 'security.step_restart'].map((k) => h('li', {}, t(k)))),
      field('security.paste_key', confirmInput),
      h('div', { class: 'actions' }, button('security.commit', async () => {
        try { await post('/api/admin/key/commit', { new_key: confirmInput.value.replace(/^APP_MASTER_KEY=/, '').trim() }); mount(area, h('p', { class: 'notice ok' }, t('security.rotated'))); } catch (e) { toast(e.message, 'error'); }
      }, 'danger'))));
  };
  shell(main, state, 'security', 'admin.security',
    h('h3', {}, t('security.network')), h('p', {}, t('security.network_text')),
    h('h3', {}, t('security.key')), h('p', {}, t('security.key_text')), h('p', { class: 'notice warn' }, t('security.key_loss')),
    h('div', { class: 'actions' }, button('security.rotate', start, 'primary')), area);
}
