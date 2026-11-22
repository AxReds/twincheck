import { h, toast } from './dom.js';
import { t } from './i18n.js';
import { post } from './api.js';

const system = matchMedia('(prefers-color-scheme: dark)');
const choices = ['auto', 'light', 'dark'];
let user = null;
let saving = false;

function storedTheme() {
  try {
    const value = localStorage.getItem('tc_theme');
    return choices.includes(value) ? value : 'auto';
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    console.warn('THEME_STORAGE_UNAVAILABLE', error.name);
    return 'auto';
  }
}

let preference = storedTheme();
const effectiveTheme = () => preference === 'auto' ? (system.matches ? 'dark' : 'light') : preference;

function updateControl(control) {
  control.disabled = saving;
  if (control.classList.contains('theme-select')) control.value = preference;
  else {
    const dark = effectiveTheme() === 'dark';
    const label = t(dark ? 'theme.to_light' : 'theme.to_dark');
    control.setAttribute('aria-label', label);
    control.title = label;
    control.querySelector('span').textContent = dark ? '\u2600' : '\u263e';
  }
}

function apply() {
  document.documentElement.dataset.theme = effectiveTheme();
  document.querySelectorAll('.theme-select, .theme-toggle').forEach(updateControl);
}

function rememberTheme(value) {
  try { localStorage.setItem('tc_theme', value); }
  catch (error) {
    if (!(error instanceof DOMException)) throw error;
    console.warn('THEME_STORAGE_UNAVAILABLE', error.name);
    toast(t('theme.storage_unavailable'), 'error');
  }
}

export function setThemeUser(value) {
  user = value;
  preference = value ? (choices.includes(value.theme) ? value.theme : 'auto') : storedTheme();
  if (value) rememberTheme(preference);
  apply();
}

async function changeTheme(value) {
  if (saving) return;
  saving = true;
  apply();
  const currentUser = user;
  try {
    if (currentUser) await post('/api/me/theme', { theme: value });
    if (user !== currentUser) return;
    preference = value;
    if (currentUser) currentUser.theme = value;
    rememberTheme(value);
  } catch (error) { toast(error.message, 'error'); }
  finally { saving = false; apply(); }
}

export function themeSelect() {
  const control = h('select', { class: 'theme-select', 'aria-label': t('theme.label') },
    choices.map((choice) => h('option', { value: choice }, t('theme.' + choice))));
  control.addEventListener('change', () => changeTheme(control.value));
  updateControl(control);
  return control;
}

export function themeToggle() {
  const control = h('button', { type: 'button', class: 'btn theme-toggle' }, h('span', { 'aria-hidden': 'true' }));
  control.addEventListener('click', () => changeTheme(effectiveTheme() === 'dark' ? 'light' : 'dark'));
  updateControl(control);
  return control;
}

system.addEventListener('change', () => { if (preference === 'auto') apply(); });
apply();
