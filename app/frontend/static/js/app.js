// Application shell: boot, fixed header with live system metrics, navigation, router.
import { h, mount, clear, toast } from './dom.js';
import { t, fmt, setLanguage, listLocales, browserLanguage, onLanguage, currentLang } from './i18n.js';
import { get, post, setCsrf } from './api.js';
import { poll, stopPolling, resumePolling } from './components.js';
import { setThemeUser, themeSelect, themeToggle } from './theme.js';
import { icon } from './icons.js';
import { renderSetup } from './views/setup.js';
import { renderLogin } from './views/login.js';
import * as tool from './views/tool.js';
import * as review from './views/review.js';
import * as admin from './views/admin.js';
import * as transferView from './views/transfer.js';

export const state = { boot: null, user: null, locales: [], perms: {}, stopMetrics: null, view: null, scanPath: null };
const root = document.getElementById('app');

const ROUTES = [
  [/^#\/?$/, () => tool.home], [/^#\/home$/, () => tool.home], [/^#\/scan$/, () => tool.scan],
  [/^#\/duplicates$/, () => tool.duplicates], [/^#\/metadata$/, () => tool.metadata], [/^#\/review$/, () => review.list],
  [/^#\/review\/(\d+)$/, () => review.item], [/^#\/organize$/, () => tool.organize], [/^#\/plans\/(\d+)$/, () => tool.plan],
  [/^#\/report$/, () => tool.report], [/^#\/jobs$/, () => tool.jobs], [/^#\/logs$/, () => tool.logs], [/^#\/me$/, () => tool.me],
  [/^#\/admin\/users$/, () => admin.users], [/^#\/admin\/groups$/, () => admin.groups], [/^#\/admin\/settings$/, () => admin.settings],
  [/^#\/admin\/database$/, () => admin.database], [/^#\/admin\/transfer$/, () => transferView.transfer],
  [/^#\/admin\/audit$/, () => admin.audit], [/^#\/admin\/security$/, () => admin.security], [/^#\/admin$/, () => admin.users],
];

const NAV = [
  ['#/home', 'nav.home', 'house'], ['#/scan', 'nav.scan', 'folder-search'], ['#/duplicates', 'nav.duplicates', 'copy'], ['#/metadata', 'nav.metadata', 'file-text'],
  ['#/review', 'nav.review', 'list-checks'], ['#/organize', 'nav.organize', 'folder-tree'], ['#/report', 'nav.report', 'undo-2'], ['#/jobs', 'nav.jobs', 'activity'], ['#/logs', 'nav.logs', 'scroll-text'],
];

export function langSelect(onChange) {
  const sel = h('select', { 'aria-label': t('common.language'), class: 'lang-select' },
    state.locales.map((l) => h('option', { value: l.code, selected: l.code === currentLang() }, l.name)));
  sel.addEventListener('change', async () => { await setLanguage(sel.value); if (onChange) onChange(sel.value); });
  return sel;
}

async function boot() {
  state.locales = await listLocales();
  const b = await get('/api/bootstrap', { allow401: true });
  state.boot = b;
  setThemeUser(b.authenticated ? b.user : null);
  const cookieLang = (document.cookie.match(/(?:^|; )tc_lang=([^;]+)/) || [])[1];
  const preferred = (b.user && b.user.lang) || (cookieLang && decodeURIComponent(cookieLang)) || browserLanguage(state.locales) || b.default_lang || 'en';
  await setLanguage(state.locales.some((l) => l.code === preferred) ? preferred : (b.default_lang || 'en'));
  document.title = t('app.name');
  if (b.setup_required) { clearSession(); renderSetup(root, state); return; }
  if (!b.authenticated) { clearSession(); renderLogin(root, state, boot); return; }
  setCsrf(b.csrf);
  if (!state.user || state.user.id !== b.user.id) state.scanPath = null;
  state.user = b.user;
  state.perms = b.permissions;
  renderShell();
}

function stopMetrics() { if (state.stopMetrics) { state.stopMetrics(); state.stopMetrics = null; } }

let shell = null;
let routeGeneration = 0;

function clearSession() {
  stopPolling();
  stopMetrics();
  if (state.view?.destroy) state.view.destroy();
  state.view = null;
  shell = null;
  routeGeneration++;
  setCsrf('');
  state.user = null;
  setThemeUser(null);
  state.perms = {};
  state.scanPath = null;
  lastMetrics = null;
}

function renderShell() {
  const metrics = h('div', { class: 'metrics', id: 'metrics' });
  const toggle = h('button', { type: 'button', class: 'btn small metrics-toggle', 'aria-expanded': 'false', 'aria-controls': 'metrics' }, t('header.system'));
  toggle.addEventListener('click', () => {
    const open = metrics.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(open));
  });
  const main = h('main', { class: 'content', id: 'content', tabindex: '-1' });
  const header = h('header', { class: 'topbar' },
    h('a', { class: 'brand', href: '#/home' }, h('img', { src: '/static/img/logo.svg', alt: '', class: 'logo' }), h('span', { class: 'brand-name' }, t('app.name'))),
    toggle, metrics, h('div', { class: 'header-actions' }, themeToggle(), userMenu()));
  const nav = h('nav', { class: 'mainnav', 'aria-label': t('nav.label') },
    NAV.map(([href, key, name]) => h('a', { href, title: t(key), 'aria-label': t(key) }, icon(name), h('span', { class: 'nav-label' }, t(key)))));
  shell = { header, nav, main, metrics };
  mount(root, header, nav, main);
  stopMetrics();
  resumePolling();
  state.stopMetrics = poll(refreshMetrics, 5000);
  route();
}

function userMenu() {
  const u = state.user;
  const panel = h('div', { class: 'menu-panel', hidden: true },
    h('div', { class: 'menu-who' }, h('strong', {}, u.username), h('span', { class: 'muted' }, t('role.' + u.role))),
    h('label', { class: 'menu-row' }, icon('languages'), t('common.language'), ' ', langSelect(async (code) => {
      try { await post('/api/me/lang', { lang: code }); } catch (e) { toast(e.message, 'error'); }
    })),
    h('label', { class: 'menu-row' }, icon('sun-moon'), t('theme.label'), ' ', themeSelect()),
    state.perms.manage_users ? h('a', { class: 'menu-row', href: '#/admin/users' }, icon('shield'), h('span', { class: 'nav-label' }, t('nav.admin'))) : null,
    h('a', { class: 'menu-row', href: '#/me' }, icon('key-round'), h('span', { class: 'nav-label' }, t('user.change_password'))),
    h('button', { type: 'button', class: 'menu-row link', onclick: logout }, icon('log-out'), h('span', { class: 'nav-label' }, t('user.logout'))));
  const btn = h('button', { type: 'button', class: 'btn user-btn', 'aria-haspopup': 'true', 'aria-expanded': 'false' }, u.username, ' ▾');
  btn.addEventListener('click', () => { panel.hidden = !panel.hidden; btn.setAttribute('aria-expanded', String(!panel.hidden)); });
  return h('div', { class: 'usermenu' }, btn, panel);
}

function closeUserMenu(restoreFocus = false) {
  const menu = shell && shell.header.querySelector('.usermenu');
  if (!menu) return;
  const panel = menu.querySelector('.menu-panel');
  const btn = menu.querySelector('.user-btn');
  const wasOpen = !panel.hidden;
  panel.hidden = true;
  btn.setAttribute('aria-expanded', 'false');
  if (restoreFocus && wasOpen) btn.focus();
}

document.addEventListener('click', (e) => {
  const menu = shell && shell.header.querySelector('.usermenu');
  if (menu && !menu.contains(e.target)) closeUserMenu();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeUserMenu(true); });

async function logout() {
  try { await post('/api/logout'); } catch (e) { /* ignore */ }
  location.hash = '#/home';
  location.reload();
}

function level(l) { return l === 'critical' ? 'crit' : l === 'warning' ? 'warn' : ''; }

function chip(labelKey, value, cls = '', title) {
  return h('div', { class: 'chip ' + cls, title: title || t(labelKey) + ': ' + value }, h('span', { class: 'chip-label' }, t(labelKey)), h('span', { class: 'chip-value' }, value));
}

function vol(v) {
  if (!v || v.total === null || v.total === undefined) return t('common.na');
  return t('header.disk_value', { used: fmt.bytes(v.used), total: fmt.bytes(v.total), pct: fmt.percent(v.used_pct) }) + (v.read_only ? ' · ' + t('header.read_only') : '');
}

let lastMetrics = null;
async function refreshMetrics() {
  const m = await get('/api/sysinfo');
  lastMetrics = m;
  drawMetrics();
}

function drawMetrics() {
  const m = lastMetrics;
  if (!m || !shell) return;
  const mem = m.memory || {};
  const disks = (m.disks || []).map((d) => h('div', { class: 'disk-row' },
    h('strong', {}, d.label), ' ',
    h('span', { class: 'chip-label' }, t('header.source')), ' ', h('code', {}, d.source_path), ' ',
    h('span', { class: level(d.source.level) }, vol(d.source)), ' · ', h('span', { class: 'muted' }, (d.source.mount_point || t('common.na')) + ' (' + (d.source.fstype || t('common.na')) + ')'),
    h('br'),
    h('span', { class: 'chip-label' }, t('header.destination')), ' ', h('code', {}, d.dest_path), ' ',
    h('span', { class: level(d.dest.level) }, vol(d.dest)), ' · ',
    d.same_volume === null ? t('common.na') : (d.same_volume ? h('span', { class: 'ok' }, t('header.same_volume')) : h('span', { class: 'warn' }, t('header.different_volume'))),
    ' · ', t('header.indexed_size', { size: d.indexed_bytes === null || d.indexed_bytes === undefined ? t('common.na') : fmt.bytes(d.indexed_bytes) })));
  const db = m.database || {};
  mount(shell.metrics,
    chip('header.host', m.host || t('common.na')),
    chip('header.os', [m.os, m.kernel, m.arch].map((x) => x || t('common.na')).join(' · ')),
    chip('header.cpu', t('header.cpu_value', { cores: m.cpu.cores ?? t('common.na'), pct: fmt.percent(m.cpu.percent) }) +
      ' · ' + t('header.load', { load: m.cpu.load ? m.cpu.load.map((x) => fmt.number(x)).join(' ') : t('common.na') })),
    chip('header.memory', mem.total ? t('header.disk_value', { used: fmt.bytes(mem.used), total: fmt.bytes(mem.total), pct: fmt.percent(mem.percent) }) : t('common.na')),
    chip('header.data', vol(m.volumes.data), level(m.volumes.data.level)),
    chip('header.state', vol(m.volumes.state), level(m.volumes.state.level)),
    chip('header.uptime', t('header.uptime_value', { system: fmt.duration(m.uptime.system), container: fmt.duration(m.uptime.container) })),
    chip('header.version', m.version),
    chip('header.database', (db.type ? t('db.type.' + db.type) : t('common.na')) + ' · ' + (db.connected ? t('header.connected') : t('header.not_connected')) +
      (db.schema_version ? ' · ' + t('header.schema', { version: db.schema_version }) : ''), db.connected ? '' : 'crit'),
    h('a', { class: 'chip link', href: '#/jobs', title: t('header.jobs_running', { count: m.jobs_running }) }, h('span', { class: 'chip-label' }, t('header.jobs')), h('span', { class: 'chip-value' }, t('header.jobs_running', { count: m.jobs_running }))),
    disks.length ? h('details', { class: 'chip disks' }, h('summary', {}, t('header.disks', { count: disks.length })), h('div', { class: 'disk-panel' }, disks)) : null);
}

export async function route() {
  if (!shell) return;
  const generation = ++routeGeneration;
  const activeShell = shell;
  closeUserMenu();
  if (state.view && state.view.destroy) state.view.destroy();
  state.view = null;
  const hash = location.hash || '#/home';
  if ((hash === '#/admin' || hash.startsWith('#/admin/')) && !state.perms.manage_users) {
    location.hash = '#/home';
    return;
  }
  highlightSection(hash);
  const main = clear(shell.main);
  const found = ROUTES.find(([re]) => re.test(hash));
  if (!found) { mount(main, h('p', {}, t('error.not_found'))); return; }
  const m = hash.match(found[0]);
  try {
    const view = (await found[1]()(main, state, m.slice(1))) || null;
    if (generation !== routeGeneration || shell !== activeShell) { if (view?.destroy) view.destroy(); return; }
    state.view = view;
    highlightSection((state.view && state.view.section) || hash);
  } catch (e) {
    if (generation === routeGeneration && shell === activeShell) mount(main, h('p', { class: 'notice error' }, e.message || t('error.generic', { code: '' })));
  }
}

function highlightSection(hash) {
  const section = hash === '#' || hash === '#/' ? '#/home' : hash;
  const adminActive = state.perms.manage_users && (section === '#/admin' || section.startsWith('#/admin/'));
  const userButton = shell.header.querySelector('.user-btn');
  userButton.classList.toggle('active', Boolean(adminActive));
  if (adminActive) userButton.setAttribute('aria-current', 'page');
  else userButton.removeAttribute('aria-current');
  for (const a of shell.nav.querySelectorAll('a')) {
    const href = a.getAttribute('href');
    const active = section === href || section.startsWith(href + '/');
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
}

window.addEventListener('hashchange', route);
window.addEventListener('po:unauthenticated', () => {
  if (!shell) return;
  clearSession();
  renderLogin(root, state, boot);
});
window.addEventListener('po:password-required', () => { if (location.hash !== '#/me') location.hash = '#/me'; });
onLanguage(() => {
  document.title = t('app.name');
  if (shell) { renderShell(); }            // instant language switch, no reload
});

boot().catch(() => mount(root, h('p', { class: 'notice error' }, t('error.network'))));
