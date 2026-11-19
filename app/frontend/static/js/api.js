// Same-origin JSON API client with CSRF header and translated errors.
import { t, has } from './i18n.js';

let csrf = '';
let authGeneration = 0;
export function setCsrf(v) { csrf = v || ''; authGeneration++; }

export class ApiError extends Error {
  constructor(status, code, params) {
    super(code);
    this.status = status; this.code = code; this.params = params || {};
    // translated lazily, so a language switch also changes later messages
    Object.defineProperty(this, 'message', { get: () => errorText(this.code, this.params), configurable: true });
  }
}

export function errorText(code, params = {}) {
  const key = 'error.' + code;
  if (code === 'weak_password' && Array.isArray(params.reasons)) {
    return params.reasons.map((r) => t('error.password_' + r, { min_length: params.min_length || 12 })).join(' ');
  }
  return has(key) ? t(key, params) : t('error.generic', { code });
}

export async function api(method, url, body, opts = {}) {
  const generation = authGeneration;
  const headers = { 'X-Requested-With': 'twincheck' };
  if (csrf) headers['X-CSRF-Token'] = csrf;
  let payload;
  if (body instanceof Blob) { payload = body; headers['Content-Type'] = 'application/octet-stream'; }
  else if (body !== undefined) { payload = JSON.stringify(body); headers['Content-Type'] = 'application/json'; }
  const r = await fetch(url, { method, headers, body: payload, credentials: 'same-origin', cache: 'no-store' });
  if (opts.raw) return r;
  let data = null;
  const text = await r.text();
  if (generation !== authGeneration && !opts.allow401) throw new ApiError(401, 'unauthenticated');
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
  if (!r.ok) {
    const err = new ApiError(r.status, (data && data.error) || 'http_' + r.status, data && data.params);
    if (r.status === 401 && !opts.allow401) window.dispatchEvent(new CustomEvent('po:unauthenticated'));
    if (r.status === 403 && err.code === 'password_change_required') window.dispatchEvent(new CustomEvent('po:password-required'));
    throw err;
  }
  return data;
}

export const get = (u, o) => api('GET', u, undefined, o);
export const post = (u, b, o) => api('POST', u, b === undefined ? {} : b, o);
export const patch = (u, b) => api('PATCH', u, b);
export const del = (u) => api('DELETE', u, {});

export function uploadFile(url, file, onProgress) {
  const generation = authGeneration;
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.setRequestHeader('X-Requested-With', 'twincheck');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total); };
    xhr.onload = () => {
      if (generation !== authGeneration) { reject(new ApiError(401, 'unauthenticated')); return; }
      if (xhr.status === 401) window.dispatchEvent(new CustomEvent('po:unauthenticated'));
      let d = null; try { d = JSON.parse(xhr.responseText); } catch (e) { d = null; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(d); else reject(new ApiError(xhr.status, (d && d.error) || 'http_' + xhr.status, d && d.params));
    };
    xhr.onerror = () => reject(new ApiError(0, 'network'));
    xhr.send(file);
  });
}
