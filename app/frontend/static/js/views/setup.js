// First-run wizard: token -> database -> admin password -> summary.
import { h, mount, field, button, toast } from '../dom.js';
import { t } from '../i18n.js';
import { post } from '../api.js';
import { langSelect } from '../app.js';
import { kv } from '../components.js';

export function renderSetup(root, state) {
  const s = { step: 0, token: '', db: { type: 'sqlite', filename: 'photoorg.sqlite3', host: '', port: 3306, database: 'photoorg', user: '', password: '' },
    test: null, password: '', password2: '', confirmMigration: false };

  const render = () => {
    const steps = ['setup.step_token', 'setup.step_database', 'setup.step_admin', 'setup.step_summary'];
    mount(root, h('div', { class: 'auth-page' },
      h('div', { class: 'auth-card wide' },
        h('div', { class: 'auth-head' }, h('img', { src: '/static/img/logo.svg', alt: '', class: 'logo' }), h('h1', {}, t('setup.title')),
          langSelect(() => render())),
        h('ol', { class: 'steps' }, steps.map((k, i) => h('li', { class: i === s.step ? 'current' : i < s.step ? 'done' : '' }, t(k)))),
        [stepToken, stepDb, stepAdmin, stepSummary][s.step]())));
  };

  const stepToken = () => {
    const inp = h('input', { type: 'password', autocomplete: 'off', value: s.token });
    return h('form', { class: 'form', onsubmit: async (e) => {
      e.preventDefault();
      s.token = inp.value.trim();
      try { await post('/api/setup/check-token', { token: s.token }); s.step = 1; render(); } catch (err) { toast(err.message, 'error'); }
    } }, h('p', {}, t('setup.token_help')), h('pre', { class: 'cmd' }, 'docker compose logs app | grep SETUP_TOKEN'),
    field('setup.token', inp), h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, t('common.next'))));
  };

  const stepDb = () => {
    const type = h('select', {}, h('option', { value: 'sqlite', selected: s.db.type === 'sqlite' }, t('db.type.sqlite')),
      h('option', { value: 'mariadb', selected: s.db.type === 'mariadb' }, t('db.type.mariadb')));
    type.addEventListener('change', () => { s.db.type = type.value; s.test = null; render(); });
    const inputs = {};
    const mk = (k, typeAttr = 'text') => { inputs[k] = h('input', { type: typeAttr, value: s.db[k] ?? '', autocomplete: 'off' }); return inputs[k]; };
    const fields = s.db.type === 'sqlite'
      ? [field('db.filename', mk('filename'), 'db.filename_hint')]
      : [field('db.host', mk('host')), field('db.port', mk('port', 'number')), field('db.database', mk('database')),
        field('db.user', mk('user')), field('db.password', mk('password', 'password')), h('p', { class: 'hint' }, t('db.mariadb_privileges'))];
    const read = () => { for (const [k, el] of Object.entries(inputs)) s.db[k] = el.value; };
    const result = s.test ? testResult(s.test) : null;
    return h('div', { class: 'form' }, field('db.type', type), fields, result,
      h('div', { class: 'actions' },
        button('common.back', () => { read(); s.step = 0; render(); }),
        button('db.test', async () => { read(); try { s.test = await post('/api/setup/test-db', { token: s.token, database: s.db }); } catch (e) { s.test = null; toast(e.message, 'error'); } render(); }),
        h('button', { type: 'button', class: 'btn primary', disabled: !(s.test && s.test.reachable && s.test.compatible !== false),
          onclick: () => { read(); s.step = 2; render(); } }, t('common.next'))));
  };

  const stepAdmin = () => {
    const p1 = h('input', { type: 'password', autocomplete: 'new-password', value: s.password });
    const p2 = h('input', { type: 'password', autocomplete: 'new-password', value: s.password2 });
    return h('form', { class: 'form', onsubmit: (e) => {
      e.preventDefault(); s.password = p1.value; s.password2 = p2.value;
      if (s.password !== s.password2) { toast(t('error.password_mismatch'), 'error'); return; }
      s.step = 3; render();
    } },
    kv([['setup.admin_username', 'admin']]), h('p', { class: 'hint' }, t('setup.admin_fixed')),
    field('setup.password', p1, 'setup.password_rules'), field('setup.password_confirm', p2),
    h('div', { class: 'actions' }, button('common.back', () => { s.step = 1; render(); }), h('button', { type: 'submit', class: 'btn primary' }, t('common.next'))));
  };

  const stepSummary = () => {
    const needsMig = s.test && s.test.structure === 'present' && s.test.schema_version < s.test.app_schema_version;
    const mig = h('input', { type: 'checkbox', checked: s.confirmMigration });
    mig.addEventListener('change', () => { s.confirmMigration = mig.checked; });
    return h('div', { class: 'form' },
      kv([['db.type', t('db.type.' + s.db.type)], ['db.target', s.db.type === 'sqlite' ? '/state/' + s.db.filename : `${s.db.user}@${s.db.host}:${s.db.port}/${s.db.database}`],
        ['db.structure', s.test ? t('db.structure.' + s.test.structure) : null], ['setup.admin_username', 'admin'], ['common.language', state.locales.find((l) => l.code === document.documentElement.lang)?.name]]),
      needsMig ? h('label', { class: 'check notice warn' }, mig, ' ', t('db.confirm_migration')) : null,
      h('p', { class: 'notice' }, t('setup.final_warning')),
      h('div', { class: 'actions' }, button('common.back', () => { s.step = 2; render(); }),
        button('setup.finish', async () => {
          try {
            await post('/api/setup/complete', { token: s.token, database: s.db, password: s.password, password_confirm: s.password2,
              lang: document.documentElement.lang, confirm_migration: s.confirmMigration });
            toast(t('setup.done'), 'ok');
            setTimeout(() => location.reload(), 600);
          } catch (e) { toast(e.message, 'error'); if (e.code === 'weak_password' || e.code === 'password_mismatch') { s.step = 2; render(); } }
        }, 'primary')));
  };

  render();
}

export function testResult(r) {
  if (!r.reachable) return h('p', { class: 'notice error' }, t('db.unreachable'), r.error ? ' (' + t('error.' + r.error) + ')' : '');
  const rows = [['db.structure', t('db.structure.' + (r.database_missing ? 'absent' : r.structure))]];
  if (r.structure === 'present') {
    rows.push(['db.db_id', r.db_id], ['db.schema_version', String(r.schema_version)], ['db.files', String(r.files)],
      ['db.open_jobs', String(r.open_jobs)], ['db.last_update', r.last_update]);
  }
  return h('div', { class: 'notice ' + (r.compatible === false ? 'error' : 'ok') },
    r.structure === 'present' ? t('db.structure_present') : t('db.structure_will_be_created'), kv(rows),
    r.compatible === false ? h('p', {}, t('error.schema_too_new', { version: r.schema_version, supported: r.app_schema_version })) : null,
    r.migration_in_progress ? h('p', {}, t('error.migration_in_progress')) : null,
    r.in_use_by ? h('p', {}, t('db.in_use_by', { host: r.in_use_by.hostname })) : null,
    r.structure === 'present' && r.schema_version < r.app_schema_version ? h('p', {}, t('db.needs_migration', { from: r.schema_version, to: r.app_schema_version })) : null);
}
