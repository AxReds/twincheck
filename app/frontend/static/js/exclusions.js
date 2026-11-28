// Managed scan exclusions; matching and validation use the scanner's server semantics.
import { h, mount, button, table, field, modal, badge } from './dom.js';
import { t } from './i18n.js';
import { post } from './api.js';

const DESCRIPTIONS = {
  '.*': 'hidden', '@eaDir': 'synology_thumbnails', '#recycle': 'synology_recycle',
  '$RECYCLE.BIN': 'windows_recycle', 'lost+found': 'linux_recovery',
  'Thumbs.db': 'windows_thumbnails', 'desktop.ini': 'windows_settings',
};

export function excludedNames(scan, defaults) {
  const original = scan.exclude.map((pattern) => ({ pattern, description: scan.exclude_descriptions?.[pattern] || '' }));
  let rows = original.map((row) => ({ ...row }));
  const box = h('div', { class: 'exclusions-list' });
  const patterns = () => rows.map((row) => row.pattern);
  const validate = async (exclude, name = '') => post('/api/admin/settings/exclusions/test', { exclude, name });
  const edit = async (index = -1) => {
    const pattern = h('input', { type: 'text', maxlength: '100', value: rows[index]?.pattern || '', autocomplete: 'off' });
    const description = h('input', { type: 'text', maxlength: '300', value: rows[index]?.description || '' });
    const name = h('input', { type: 'text', autocomplete: 'off' });
    const result = h('p', { class: 'notice', role: 'status', 'aria-live': 'polite' }, t('exclusions.test_hint'));
    let generation = 0;
    const proposed = () => index < 0 ? [...patterns(), pattern.value] : patterns().map((p, i) => i === index ? pattern.value : p);
    const test = async () => {
      const current = ++generation;
      try {
        const data = await validate(proposed(), name.value);
        if (current !== generation || !result.isConnected) return;
        result.textContent = !name.value ? t('exclusions.test_hint') : data.matches.length
          ? t('exclusions.matches', { patterns: data.matches.join(' · ') }) : t('exclusions.no_match');
      } catch (e) {
        if (current === generation && result.isConnected) result.textContent = e.message;
      }
    };
    pattern.addEventListener('input', test);
    name.addEventListener('input', test);
    const body = h('div', { class: 'form exclusion-dialog' }, field('exclusions.pattern', pattern),
      h('p', { class: 'hint' }, t('exclusions.pattern_hint')), field('exclusions.description', description),
      field('exclusions.test_name', name), result);
    const saved = await modal({ titleKey: index < 0 ? 'exclusions.add' : 'admin.edit', body,
      confirmKey: 'common.save', onConfirm: async () => {
        await validate(proposed());
        return true;
      } });
    generation++;
    if (!saved) return;
    const row = { pattern: pattern.value, description: description.value };
    if (index < 0) rows.push(row); else rows[index] = row;
    render();
  };
  const render = () => mount(box,
    rows.length ? table(['exclusions.pattern', 'exclusions.description', ''], rows.map((row, index) => h('tr', {},
      h('td', {}, h('code', {}, row.pattern), defaults.includes(row.pattern) ? [' ', badge(t('exclusions.default'), 'muted')] : null),
      h('td', {}, row.description || (DESCRIPTIONS[row.pattern] ? t('exclusions.default.' + DESCRIPTIONS[row.pattern]) : '')),
      h('td', { class: 'actions' }, button('admin.edit', () => edit(index), 'small'), button('common.remove', async () => {
        if (await modal({ titleKey: 'common.remove', body: h('p', {}, t('exclusions.remove_confirm', { pattern: row.pattern })) })) {
          rows.splice(index, 1); render();
        }
      }, 'small danger-outline'))))) : h('p', {}, t('exclusions.empty')),
    h('div', { class: 'actions' }, button('exclusions.add', () => edit()), button('exclusions.restore', async () => {
      if (await modal({ titleKey: 'exclusions.restore', body: h('p', {}, t('exclusions.restore_confirm')) })) {
        rows = defaults.map((pattern) => ({ pattern, description: '' })); render();
      }
    })), h('p', { class: 'hint' }, t('exclusions.next_analysis')), h('p', { class: 'hint' }, t('exclusions.internal')));
  render();
  return {
    el: box,
    async changes() {
      if (JSON.stringify(rows) === JSON.stringify(original)) return null;
      await validate(patterns());
      const added = rows.filter((row) => !original.some((old) => old.pattern === row.pattern)).map((row) => row.pattern);
      const removed = original.filter((row) => !rows.some((next) => next.pattern === row.pattern)).map((row) => row.pattern);
      const edited = rows.filter((row) => original.some((old) => old.pattern === row.pattern && old.description !== row.description)).map((row) => row.pattern);
      const body = h('div', {}, h('p', {}, t('exclusions.next_analysis')),
        added.length ? h('p', {}, t('exclusions.added', { patterns: added.join(' · ') })) : null,
        removed.length ? h('p', {}, t('exclusions.removed', { patterns: removed.join(' · ') })) : null,
        edited.length ? h('p', {}, t('exclusions.edited', { patterns: edited.join(' · ') })) : null);
      if (!await modal({ titleKey: 'exclusions.save_title', body })) return false;
      return { exclude: patterns(), exclude_descriptions: Object.fromEntries(rows.filter((row) => row.description)
        .map((row) => [row.pattern, row.description])), confirm: true };
    },
    saved() { original.splice(0, original.length, ...rows.map((row) => ({ ...row }))); },
  };
}
