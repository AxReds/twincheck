// DOM helpers. Text is always inserted as text nodes (never innerHTML).
// Every visible string must come from t(...) or from data (paths, values).
import { t } from './i18n.js';

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function mount(el, ...children) { clear(el); append(el, children); return el; }

export function field(labelKey, input, hintKey) {
  const id = 'f' + Math.random().toString(36).slice(2, 9);
  input.id = input.id || id;
  return h('div', { class: 'field' }, h('label', { for: input.id }, t(labelKey)), input, hintKey ? h('small', { class: 'hint' }, t(hintKey)) : null);
}

export function button(labelKey, onClick, cls = '', params) {
  return h('button', { type: 'button', class: 'btn ' + cls, onclick: onClick }, t(labelKey, params));
}

export function badge(text, kind = '') { return h('span', { class: 'badge ' + kind }, text); }

export function progress(done, total) {
  const pct = total > 0 ? Math.min(100, Math.round((100 * done) / total)) : null;
  const bar = h('div', { class: 'progress' + (pct === null ? ' indeterminate' : ''), role: 'progressbar',
    'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': pct === null ? undefined : String(pct) },
  h('div', { class: 'bar', style: { inlineSize: pct === null ? '30%' : pct + '%' } }));
  return bar;
}

export function table(headKeys, rows) {
  return h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, headKeys.map((k) => h('th', { scope: 'col' }, k ? t(k) : '')))),
    h('tbody', {}, rows)));
}

export function modal({ titleKey, body, confirmKey = 'common.confirm', cancelKey = 'common.cancel', danger = false, requireCheckKey }) {
  return new Promise((resolve) => {
    const check = requireCheckKey ? h('input', { type: 'checkbox' }) : null;
    const ok = h('button', { type: 'button', class: 'btn ' + (danger ? 'danger' : 'primary'), disabled: !!check }, t(confirmKey));
    const close = (v) => { overlay.remove(); resolve(v); };
    if (check) check.addEventListener('change', () => { ok.disabled = !check.checked; });
    ok.addEventListener('click', () => close(true));
    const overlay = h('div', { class: 'modal-overlay', role: 'dialog', 'aria-modal': 'true' },
      h('div', { class: 'modal' },
        h('h2', {}, t(titleKey)),
        h('div', { class: 'modal-body' }, body),
        check ? h('label', { class: 'check' }, check, ' ', t(requireCheckKey)) : null,
        h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn', onclick: () => close(false) }, t(cancelKey)), ok)));
    overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(false); });
    document.body.appendChild(overlay);
    ok.focus();
  });
}

let toastBox;
export function toast(text, kind = 'info') {
  if (!toastBox) { toastBox = h('div', { class: 'toasts', 'aria-live': 'polite' }); document.body.appendChild(toastBox); }
  const el = h('div', { class: 'toast ' + kind }, text);
  toastBox.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 8000 : 4000);
}
