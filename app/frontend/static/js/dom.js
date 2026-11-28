// DOM helpers. Text is always inserted as text nodes (never innerHTML).
// Every visible string must come from t(...) or from data (paths, values).
import { t, fmt } from './i18n.js';

const listeners = new WeakMap();
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') {
      const event = k.slice(2).toLowerCase();
      el.addEventListener(event, v);
      if (!listeners.has(el)) listeners.set(el, {});
      listeners.get(el)[event] = v;
    }
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

function reconcile(current, next) {
  if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName) {
    current.replaceWith(next);
    return;
  }
  if (current.nodeType === Node.TEXT_NODE) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return;
  }
  for (const attr of [...current.attributes]) if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
  for (const attr of next.attributes) if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
  const oldEvents = listeners.get(current) || {};
  const newEvents = listeners.get(next) || {};
  for (const [event, fn] of Object.entries(oldEvents)) current.removeEventListener(event, fn);
  for (const [event, fn] of Object.entries(newEvents)) current.addEventListener(event, fn);
  listeners.set(current, newEvents);
  const oldChildren = [...current.childNodes], newChildren = [...next.childNodes];
  newChildren.forEach((child, index) => {
    if (oldChildren[index]) reconcile(oldChildren[index], child);
    else current.appendChild(child);
  });
  oldChildren.slice(newChildren.length).forEach((child) => child.remove());
  if (current !== document.activeElement) {
    if (current.matches('input, select, textarea') && current.value !== next.value) current.value = next.value;
    if (current.matches('input') && current.checked !== next.checked) current.checked = next.checked;
  }
}

export function dataView(container) {
  let previous = null;
  return (data, render) => {
    const signature = JSON.stringify(data);
    if (signature === previous) return false;
    const next = h(container.tagName, {}, render(data));
    const scroll = window.scrollY;
    const positions = [container, ...container.querySelectorAll('*')]
      .filter((node) => node.scrollTop || node.scrollLeft)
      .map((node) => ({ node, top: node.scrollTop, left: node.scrollLeft }));
    const oldChildren = [...container.childNodes], newChildren = [...next.childNodes];
    newChildren.forEach((child, index) => {
      if (oldChildren[index]) reconcile(oldChildren[index], child);
      else container.appendChild(child);
    });
    oldChildren.slice(newChildren.length).forEach((child) => child.remove());
    window.scrollTo(0, scroll);
    for (const { node, top, left } of positions) {
      node.scrollTop = top;
      node.scrollLeft = left;
    }
    previous = signature;
    return true;
  };
}

export function field(labelKey, input, hintKey) {
  const id = 'f' + Math.random().toString(36).slice(2, 9);
  const control = input.querySelector('[role="combobox"]') || input;
  control.id = control.id || id;
  if (input.matches('input[type="checkbox"]')) {
    return h('div', { class: 'field checkbox-field' },
      h('label', { for: input.id }, input, t(labelKey)),
      hintKey ? h('small', { class: 'hint' }, t(hintKey)) : null);
  }
  return h('div', { class: 'field' }, h('label', { for: control.id }, t(labelKey)), input, hintKey ? h('small', { class: 'hint' }, t(hintKey)) : null);
}

export function button(labelKey, onClick, cls = '', params) {
  return h('button', { type: 'button', class: 'btn ' + cls, onclick: onClick }, t(labelKey, params));
}

export function badge(text, kind = '') { return h('span', { class: 'badge ' + kind }, text); }

export function instant(iso) {
  return h('time', { datetime: iso || undefined, title: fmt.dateTooltip(iso) }, fmt.date(iso));
}

export function progress(done, total) {
  const pct = total > 0 ? Math.min(100, Math.round((100 * done) / total)) : null;
  const bar = h('div', { class: 'progress' + (pct === null ? ' indeterminate' : ''), role: 'progressbar',
    'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': pct === null ? undefined : String(pct) },
  h('div', { class: 'bar', style: { inlineSize: pct === null ? '30%' : pct + '%' } }));
  return bar;
}

export function table(headKeys, rows) {
  if (!rows.length) return h('p', { class: 'empty-state muted' }, t('common.empty_list'));
  return h('div', { class: 'table-wrap', tabindex: '0' }, h('table', {},
    h('thead', {}, h('tr', {}, headKeys.map((k) => h('th', { scope: 'col' }, k ? t(k) : '')))),
    h('tbody', {}, rows)));
}

export function modal({ titleKey, body, confirmKey = 'common.confirm', cancelKey = 'common.cancel', danger = false, requireCheckKey, onConfirm }) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const titleId = 'dialog-' + Math.random().toString(36).slice(2);
    const check = requireCheckKey ? h('input', { type: 'checkbox' }) : null;
    const ok = h('button', { type: 'button', class: 'btn ' + (danger ? 'danger' : 'primary'), disabled: !!check }, t(confirmKey));
    const close = (v) => { overlay.remove(); if (previous?.isConnected) previous.focus(); resolve(v); };
    if (check) check.addEventListener('change', () => { ok.disabled = !check.checked; });
    ok.addEventListener('click', async () => {
      ok.disabled = true;
      try {
        if (!onConfirm || await onConfirm()) close(true);
      } catch (e) { toast(e.message, 'error'); }
      finally { if (ok.isConnected) ok.disabled = check ? !check.checked : false; }
    });
    const overlay = h('div', { class: 'modal-overlay', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId },
      h('div', { class: 'modal' },
        h('h2', { id: titleId }, t(titleKey)),
        h('div', { class: 'modal-body' }, body),
        check ? h('label', { class: 'check' }, check, ' ', t(requireCheckKey)) : null,
        h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn', onclick: () => close(false) }, t(cancelKey)), ok)));
    overlay.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
      if (e.key === 'Tab') {
        const controls = [...overlay.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]')]
          .filter((node) => node.getClientRects().length);
        if (e.shiftKey && document.activeElement === controls[0]) { e.preventDefault(); controls.at(-1)?.focus(); }
        else if (!e.shiftKey && document.activeElement === controls.at(-1)) { e.preventDefault(); controls[0]?.focus(); }
      }
    });
    document.body.appendChild(overlay);
    (onConfirm ? overlay.querySelector('input, select, textarea') || ok : ok).focus();
  });
}

let toastBox;
export function toast(text, kind = 'info') {
  if (!toastBox) { toastBox = h('div', { class: 'toasts', 'aria-live': 'polite' }); document.body.appendChild(toastBox); }
  const el = h('div', { class: 'toast ' + kind }, text);
  toastBox.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 8000 : 4000);
}
