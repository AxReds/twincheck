import { h, mount, field, toast } from '../dom.js';
import { t } from '../i18n.js';
import { post } from '../api.js';
import { langSelect } from '../app.js';

export function renderLogin(root, state, reboot) {
  const render = () => {
    const user = h('input', { type: 'text', autocomplete: 'username', required: true });
    const pass = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    const msg = h('p', { class: 'notice error', hidden: true, role: 'alert' });
    const form = h('form', { class: 'form', onsubmit: async (e) => {
      e.preventDefault();
      msg.hidden = true;
      try {
        await post('/api/login', { username: user.value, password: pass.value }, { allow401: true });
        location.hash = '#/home';
        await reboot();
      } catch (err) {
        msg.textContent = err.code === 'too_many_attempts' ? t('error.too_many_attempts', { seconds: err.params.retry_after }) : err.message;
        msg.hidden = false;
        if (err.code === 'network') toast(err.message, 'error');
      }
    } },
    field('login.username', user), field('login.password', pass), msg,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, t('login.submit'))));
    mount(root, h('div', { class: 'auth-page' }, h('div', { class: 'auth-card' },
      h('div', { class: 'auth-head' }, h('img', { src: '/static/img/logo.svg', alt: '', class: 'logo' }), h('h1', {}, t('app.name')), langSelect(() => render())),
      h('h2', {}, t('login.title')), form, h('p', { class: 'hint' }, t('login.plain_http_notice')))));
    user.focus();
  };
  render();
}
