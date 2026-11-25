// Minimal i18n module (no external library, works offline).
// Locale file: {"meta": {...}, "messages": {"key": "text {param}", "plural.key": {"one": "...", "other": "..."}}}
// Missing keys fall back to English; plural forms are chosen with Intl.PluralRules.

let lang = 'en';
let en = {};
let cur = {};
let meta = {};
const cache = new Map();
const listeners = new Set();

async function fetchLocale(code) {
  if (cache.has(code)) return cache.get(code);
  const r = await fetch('/api/locales/' + encodeURIComponent(code), { credentials: 'same-origin' });
  if (!r.ok) throw new Error('locale');
  const data = await r.json();
  cache.set(code, data);
  return data;
}

export async function listLocales() {
  const r = await fetch('/api/locales', { credentials: 'same-origin' });
  return r.ok ? r.json() : [{ code: 'en', name: 'English', dir: 'ltr', status: 'complete' }];
}

export async function setLanguage(code) {
  const base = await fetchLocale('en');
  en = base.messages;
  let data = base;
  try { data = code === 'en' ? base : await fetchLocale(code); } catch (e) { code = 'en'; }
  lang = code;
  cur = data.messages;
  meta = data.meta || {};
  document.documentElement.lang = code;
  document.documentElement.dir = meta.dir === 'rtl' ? 'rtl' : 'ltr';
  document.cookie = 'tc_lang=' + encodeURIComponent(code) + '; Path=/; SameSite=Strict; Max-Age=31536000';
  listeners.forEach((fn) => fn(code));
}

export function onLanguage(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function currentLang() { return lang; }
export function isRtl() { return meta.dir === 'rtl'; }

export function browserLanguage(available) {
  const codes = available.map((l) => l.code);
  for (const pref of navigator.languages || [navigator.language || 'en']) {
    if (codes.includes(pref)) return pref;
    const base = pref.split('-')[0];
    const hit = codes.find((c) => c === base || c.split('-')[0] === base);
    if (hit) return hit;
  }
  return null;
}

function pick(value, count, fallback) {
  if (value === null || typeof value !== 'object') return value;
  let cat = 'other';
  try { cat = new Intl.PluralRules(lang).select(Number(count)); } catch (e) { /* keep other */ }
  if (Number(count) === 0 && value.zero !== undefined && lang !== 'en') return value.zero;
  return value[cat] ?? value.other ?? (fallback && typeof fallback === 'object' ? (fallback[cat] ?? fallback.other) : undefined);
}

export function has(key) { return cur[key] !== undefined || en[key] !== undefined; }

export function t(key, params = {}) {
  let v = pick(cur[key], params.count, en[key]);
  if (v === undefined) v = pick(en[key], params.count);
  if (v === undefined) return '\u2026';          // never show a raw key
  return String(v).replace(/\{(\w+)\}/g, (m, k) => {
    if (!(k in params)) return m;
    const p = params[k];
    return typeof p === 'number' ? fmt.number(p) : String(p);
  });
}

export const fmt = {
  number(n, opts) { return n === null || n === undefined ? t('common.na') : new Intl.NumberFormat(lang, opts).format(n); },
  percent(p) { return p === null || p === undefined ? t('common.na') : new Intl.NumberFormat(lang, { maximumFractionDigits: 1 }).format(p) + '\u00a0%'; },
  bytes(n) {
    if (n === null || n === undefined) return t('common.na');
    const units = ['byte', 'kilobyte', 'megabyte', 'gigabyte', 'terabyte', 'petabyte'];
    let i = 0; let v = Number(n);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return new Intl.NumberFormat(lang, { style: 'unit', unit: units[i], unitDisplay: 'short', maximumFractionDigits: i ? 1 : 0 }).format(v);
  },
  date(iso) {
    if (!iso) return t('common.na');
    const d = new Date(iso.length <= 19 ? iso + (iso.length === 10 ? 'T00:00:00' : '') : iso);
    if (isNaN(d)) return iso;
    return new Intl.DateTimeFormat(lang, { dateStyle: 'medium', timeStyle: iso.length > 10 ? 'medium' : undefined }).format(d);
  },
  localDate(iso) {
    // capture dates are local wall-clock times without a zone: show them as they are
    if (!iso) return t('common.na');
    const [d, tm] = iso.split('T');
    const [y, mo, da] = d.split('-').map(Number);
    const [h, mi, s] = (tm || '00:00:00').split(':').map(Number);
    const dt = new Date(Date.UTC(y, mo - 1, da, h, mi, s || 0));
    return new Intl.DateTimeFormat(lang, { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' }).format(dt);
  },
  exifDate(iso, offset = '') {
    if (!iso) return t('common.na');
    const shown = fmt.localDate(iso.slice(0, 19));
    return offset ? `${shown} ${offset}` : shown;
  },
  duration(sec) {
    if (sec === null || sec === undefined) return t('common.na');
    const s = Math.floor(sec); const d = Math.floor(s / 86400); const h = Math.floor((s % 86400) / 3600); const m = Math.floor((s % 3600) / 60);
    const parts = [];
    if (d) parts.push(new Intl.NumberFormat(lang, { style: 'unit', unit: 'day', unitDisplay: 'narrow' }).format(d));
    if (h || d) parts.push(new Intl.NumberFormat(lang, { style: 'unit', unit: 'hour', unitDisplay: 'narrow' }).format(h));
    parts.push(new Intl.NumberFormat(lang, { style: 'unit', unit: 'minute', unitDisplay: 'narrow' }).format(m));
    return parts.join(' ');
  },
};
