/* AMS-автопилот: доктор, правила владельца, советы и откат. Без команд без кнопки. */
(() => {
'use strict';
const { $, esc, num, toast, fail, confirmDanger } = PF.ui;
const { get, post } = PF.api;
let state = { pid: '', at: 0, loading: false, rules: [], actions: [] };
const UNDOABLE = new Set(['auto_create', 'auto_bind', 'auto_move', 'auto_unbind',
  'auto_empty', 'auto_update', 'refill', 'loss', 'runout']);
const LABELS = {
  auto_create: 'Новая катушка', auto_bind: 'Привязка', auto_move: 'Переезд',
  auto_unbind: 'Отвязка', auto_empty: 'Слот опустел', auto_update: 'Обновление',
  refill: 'Долив', loss: 'Потеря', runout: '0% / сход',
  unrecognized: 'Ручная привязка', push: 'Настройки отправлены',
  push_error: 'Отказ записи', undo: 'Откат',
};
const pid = () => PF.state.activePrinter || '';
const shown = () => { const pane = $('ptab-ams'); return pane && !pane.hidden && PF.viewOn('printers'); };

function renderDoctor(data) {
  const issues = data.issues || [];
  const counts = data.counts || {};
  const badge = $('aa_status');
  badge.className = 'chip ' + (counts.error ? 'bad' : counts.warning ? 'warn' : 'ok');
  badge.textContent = counts.error ? `Проблем: ${counts.error}`
    : counts.warning ? `Проверить: ${counts.warning}` : 'Нет критичных проблем';
  $('aa_doctor').innerHTML = issues.length ? issues.map((i) =>
    `<div class="aa-item"><span class="chip ${i.level === 'error' ? 'bad' : i.level === 'warning' ? 'warn' : 'outline'}">${i.level === 'error' ? '!' : i.level === 'warning' ? '⚠' : 'i'}</span>`
    + `<span><b>${esc(i.title)}</b><small>${esc(i.detail)}</small></span></div>`).join('')
    : '<div class="aa-item"><span class="chip ok">✓</span><span><b>По доступным данным всё в порядке</b><small>Телеметрия и история обновляются при связи с принтером.</small></span></div>';
}

function renderRules(data) {
  state.rules = data.rules || [];
  $('aa_push').checked = data.push_enabled !== false;
  $('aa_rules').innerHTML = state.rules.length ? state.rules.map((r) =>
    `<div class="aa-item"><span><b>${esc(r.match_material || 'Любой материал')}`
    + `${r.match_color ? ' · ' + esc(r.match_color) : ''}</b>`
    + `<small>${r.match_uuid ? 'RFID ' + esc(r.match_uuid) + ' · ' : ''}`
    + `${esc(r.brand || 'любой бренд')} · ${num(r.total_grams) || 1000} г`
    + `${num(r.price) ? ' · ' + esc(String(r.price)) + ' ₽' : ' · цена из справочника'}`
    + `${r.temp_min || r.temp_max ? ' · ' + esc(String(r.temp_min || '…')) + '–' + esc(String(r.temp_max || '…')) + ' °C' : ''}`
    + `${num(r.enabled) ? '' : ' · выключено'}</small></span>`
    + `<button class="btn sm ghost" type="button" data-aa-edit="${esc(r.id)}">Править</button>`
    + `<button class="btn sm ghost" type="button" data-aa-delete="${esc(r.id)}" title="Удалить правило">×</button></div>`
  ).join('') : '<div class="aa-item"><span><b>Пока нет правил</b><small>Новые RFID-катушки получат значения из справочника материалов.</small></span></div>';
}

function renderPlan(data) {
  const rows = data.advice || [];
  $('aa_plan').innerHTML = rows.length ? rows.map((r) =>
    `<div class="aa-item"><span><b>${esc(r.material)}${r.color ? ' · ' + esc(r.color) : ''}`
    + `${r.order ? ' · №' + esc(r.order) : ''}`
    + `${r.multi_color ? ' · <span class="chip warn">мультицвет</span>' : ''}</b><small>${esc(r.suggestion)}`
    + `${r.slot !== '' ? ' · слот ' + (num(r.slot) + 1) : ''}`
    + `${r.remaining_grams ? ' · осталось ' + Math.round(num(r.remaining_grams)) + ' г' : ''}`
    + `${r.grams ? ' · нужно ' + Math.round(num(r.grams)) + ' г' : ''}</small></span></div>`).join('')
    : '<div class="aa-item"><span><b>В очереди нет материалов</b><small>Введите материал ниже — подскажем подходящую катушку без изменения AMS.</small></span></div>';
}

function renderActions(data) {
  state.actions = data.actions || [];
  $('aa_actions').innerHTML = state.actions.length ? state.actions.slice(0, 40).map((a) =>
    `<div class="aa-item"><span><b>${esc(LABELS[a.action] || a.action)}`
    + `${a.slot !== '' ? ' · слот ' + (num(a.slot) + 1) : ''}</b>`
    + `<small>${esc(a.detail || '')} · ${esc(new Date(a.at).toLocaleString('ru-RU'))}`
    + `${a.undone_at ? ' · отменено' : ''}</small></span>`
    + `<button class="btn sm ghost" type="button" data-aa-detail="${esc(a.id)}">До/после</button>`
    + (UNDOABLE.has(a.action) && !a.undone_at
      ? `<button class="btn sm ghost" type="button" data-aa-undo="${esc(a.id)}">Откатить</button>` : '')
    + '</div>').join('')
    : '<div class="aa-item"><span><b>Решений пока нет</b><small>При первом синке здесь появятся снимки до и после.</small></span></div>';
}

async function refresh(force = false) {
  const current = pid();
  if (!shown() || !current || state.loading) return;
  if (!force && state.pid === current && Date.now() - state.at < 60000) return;
  state.loading = true;
  try {
    const [doctor, rules, plan, actions] = await Promise.all([
      get('/api/ams/doctor', { printer_id: current }), get('/api/ams/rules'),
      get('/api/ams/plan', { printer_id: current }), get('/api/ams/actions', { printer_id: current }),
    ]);
    if (current !== pid()) return;
    renderDoctor(doctor); renderRules(rules); renderPlan(plan); renderActions(actions);
    state.pid = current; state.at = Date.now();
  } catch (err) {
    $('aa_status').className = 'chip bad';
    $('aa_status').textContent = 'Проверка недоступна';
    $('aa_doctor').innerHTML = `<div class="aa-item"><span><b>Не удалось прочитать AMS</b><small>${esc(err.message)}</small></span></div>`;
  } finally { state.loading = false; }
}

function resetRule() {
  const form = $('aa_rule_form');
  form.reset();
  form.elements.namedItem('id').value = '';
  $('aa_rule_details').querySelector('summary').textContent = 'Добавить правило';
}
function editRule(id) {
  const rule = state.rules.find((r) => r.id === id);
  if (!rule) return;
  const form = $('aa_rule_form');
  for (const field of ['id', 'match_material', 'match_color', 'match_uuid', 'material',
    'color_hex', 'brand', 'total_grams', 'price', 'temp_min', 'temp_max']) {
    form.elements.namedItem(field).value = rule[field] == null ? '' : String(rule[field]);
  }
  $('aa_rule_details').open = true;
  $('aa_rule_details').querySelector('summary').textContent = 'Правка правила';
  form.elements.namedItem('match_material').focus();
}

function bind() {
  const host = $('ams_auto_card');
  if (!host) return;
  const tabs = $('pr_detail_tabs');
  if (tabs) tabs.addEventListener('click', (e) => {
    if (e.target.closest('[data-ptab="ams"]')) setTimeout(() => refresh(true), 0);
  });
  const park = $('pr_park');
  if (park) park.addEventListener('click', (e) => {
    if (e.target.closest('[data-pcard]')) setTimeout(() => refresh(true), 0);
  });
  $('aa_refresh').addEventListener('click', () => refresh(true));
  $('aa_tidy').addEventListener('click', async () => {
    const button = $('aa_tidy');
    button.disabled = true;
    try {
      const data = await post('/api/ams/tidy', { printer_id: pid() });
      toast('Учёт AMS обновлён', data.push.reason ||
        `Новых: ${data.created} · привязок/остатков: ${data.updated} · команд отправлено: ${data.push.sent}`);
      await PF.refreshCore();
      state.at = 0;
      await refresh(true);
    } catch (err) { fail(err); }
    finally { button.disabled = false; }
  });
  $('aa_export').addEventListener('click', async () => {
    const button = $('aa_export');
    if (!pid()) return fail(new Error('Не выбран принтер'));
    button.disabled = true;
    try {
      const data = await post('/api/ams/export', { printer_id: pid() });
      const missing = (data.advice || []).filter((a) => a.suggestion === 'Подходящей катушки нет');
      if (!data.ok) toast('Экспорт в AMS отложен', data.reason || 'Принтер занят или не подключён');
      else toast('Экспорт в AMS',
        `Команд отправлено: ${data.sent} · пропущено: ${data.skipped}`
        + ((data.errors || []).length ? ' · отказов: ' + data.errors.length : '')
        + (missing.length ? ' · нет подходящей катушки: ' + missing.length : ''));
      await PF.refreshCore();
      state.at = 0;
      await refresh(true);
    } catch (err) { fail(err); }
    finally { button.disabled = false; }
  });
  $('aa_push').addEventListener('change', async (e) => {
    const on = e.target.checked;
    try {
      const data = await post('/api/settings', { ams_push_settings: on });
      PF.setSettings(data.settings || { ams_push_settings: on });
      toast(on ? 'Автозапись AMS включена' : 'Автозапись AMS отключена');
      await refresh(true);
    } catch (err) { e.target.checked = !on; fail(err); }
  });
  $('aa_rule_form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target));
    if (!data.match_material && !data.match_uuid && !data.match_color) {
      return fail(new Error('Укажите тип материала, RFID или цвет для правила'));
    }
    try {
      await post('/api/ams/rules/save', data);
      toast('Правило AMS сохранено'); resetRule(); await refresh(true);
    } catch (err) { fail(err); }
  });
  $('aa_rule_reset').addEventListener('click', resetRule);
  $('aa_plan_form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    try {
      const data = await post('/api/ams/plan', { printer_id: pid(), materials: [f] });
      renderPlan(data); toast('План только подсказывает', 'Ни один слот не изменён', 'info');
    } catch (err) { fail(err); }
  });
  host.addEventListener('click', async (e) => {
    const edit = e.target.closest('[data-aa-edit]');
    if (edit) return editRule(edit.dataset.aaEdit);
    const del = e.target.closest('[data-aa-delete]');
    if (del) {
      if (!confirmDanger('Удалить правило AMS? Уже созданные катушки останутся без изменений.')) return;
      try { await post('/api/ams/rules/delete', { id: del.dataset.aaDelete });
        toast('Правило удалено'); resetRule(); await refresh(true); } catch (err) { fail(err); }
      return;
    }
    const undo = e.target.closest('[data-aa-undo]');
    if (undo) {
      if (!confirmDanger('Откатить это решение только в базе? Физическое состояние AMS не изменится.')) return;
      try { await post('/api/ams/actions/undo', { id: undo.dataset.aaUndo });
        toast('Учёт возвращён к снимку до решения'); await PF.refreshCore(); await refresh(true);
      } catch (err) { fail(err); }
      return;
    }
    const detail = e.target.closest('[data-aa-detail]');
    if (detail) {
      try {
        const data = await get('/api/ams/actions/detail', { id: detail.dataset.aaDetail });
        let box = detail.parentElement.querySelector('.aa-snap');
        if (box) { box.remove(); return; }
        box = document.createElement('pre'); box.className = 'aa-snap';
        box.textContent = 'ДО\n' + JSON.stringify(data.before, null, 2)
          + '\n\nПОСЛЕ\n' + JSON.stringify(data.after, null, 2);
        detail.parentElement.appendChild(box);
      } catch (err) { fail(err); }
    }
  });
  PF.on('live', () => { refresh(); });
  PF.on('view', () => { setTimeout(() => refresh(), 0); });
  setTimeout(() => refresh(), 0);
}
PF.on('ready', bind);
})();
