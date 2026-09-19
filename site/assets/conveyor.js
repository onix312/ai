/* PrintFlow 18.8 — раздел «Конвейер» (FarmLoop Stage 1, P1S).
   Вкладка вынесена из «Печати» и «Настроек» (решение владельца): конвейер
   — это цех, а не бумага. Здесь — статус обвязки, допуски на работу без
   человека (safety-gate), датчики, серия и журнал. Автоматика включится
   только когда каждый допуск подтверждён; пока — человек снимает деталь.

   Грузится лениво (идея 47), как остальные тяжёлые разделы. */
(() => {
'use strict';

const { $, esc, num, toast, fail, agoText, dateTimeText } = PF.ui;
const { get, post } = PF.api;
const { settingGroup } = PF.modules.settings;

/* Группа конвейера переехала сюда из app.js (18.8): ключи те же, что в
   DEFAULT_SETTINGS, — контракт test_farmloop_settings_ui держит полноту. */
const FARMLOOP = [
  ['farmloop_profile', 'FarmLoop: профиль', 'Профиль конвейера под вашу обвязку', 'text'],
  ['farmloop_mechanics_verified', 'Механика конвейера проверена (допуск)', 'Допуск на работу без человека: толкатель и направляющие выставлены и проверены', 'bool'],
  ['farmloop_template_verified', 'Шаблон серии проверен (допуск)', 'Допуск на работу без человека: задание уходит в цикл корректно', 'bool'],
  ['farmloop_pusher_enabled', 'Толкатель установлен (допуск)', 'Конвейер может снимать детали сам', 'bool'],
  ['farmloop_bender_enabled', 'Изгибатель установлен (допуск)', 'Детали отгибаются от стола после печати', 'bool'],
  ['farmloop_sensor_mode', 'Подтверждение пустой платформы', 'Как конвейер понимает, что деталь снята', 'select', [
    ['manual', 'manual — Подтверждает человек'],
    ['sensor', 'sensor — По датчику'],
    ['camera', 'camera — По камере'],
    ['both', 'both — Датчик и камера'],
  ]],
  ['farmloop_sensor_timeout_s', 'Таймаут подтверждения, с', 'Сколько секунд ждать подтверждения пустой платформы', 'num', 1],
  ['farmloop_camera_threshold_pct', 'Порог камеры, %', 'Насколько (%) должен опустеть кадр платформы, чтобы цикл продолжился', 'num', 0.5],
  ['farmloop_cooldown_s', 'Пауза между циклами, с', 'Охлаждение конвейера перед следующим циклом', 'num', 1],
  ['farmloop_auto_next', 'Следующий цикл автоматически', 'Требует допуска: механика, шаблон, толкатель, изгибатель и датчик/камера', 'bool'],
  ['farmloop_max_cycles', 'Максимум циклов за серию', 'Сколько деталей конвейер напечатает подряд (1 — одна деталь за серию)', 'num', 1],
  ['farmloop_unattended_series', 'Бесконтрольная серия (без человека)', 'Допуск на серию без присмотра: требует авто-цикл и больше одного цикла', 'bool'],
  ['farmloop_max_detach_attempts', 'Попытки снятия детали', 'Сколько попыток снять деталь, прежде чем позвать человека', 'num', 1],
];

let bound = false;
let gates = null; // вычисленные гейты из /api/farmloop/settings

const put = (id, html) => {
  const el = $(id);
  if (el) el.innerHTML = html;
  return el;
};

/* ------------------------------------------------------------ статус */
async function loadConveyorStatus() {
  const text = $('cv_status_text');
  const meta = $('cv_status_meta');
  const tag = $('cv_status_tag');
  const head = $('cv_tag');
  if (!text) return;
  try {
    const profile = await get('/api/farmloop/profile');
    if (profile.template_installed) {
      if (tag) { tag.textContent = 'Шаблон установлен'; tag.className = 'tag ok'; }
      if (head) { head.textContent = 'Готов'; head.className = 'tag ok'; head.hidden = false; }
      if (text) {
        text.textContent = 'Проверенный шаблон на месте: файлы серии собираются '
          + 'с блоком автоматического снятия. Загрузка задания с профилем — и конвейер работает.';
      }
      if (meta) meta.textContent = `${profile.template_name} · вход: ${profile.input_format} · профиль ${profile.id || ''}`;
    } else {
      if (tag) { tag.textContent = 'Ждёт шаблон'; tag.className = 'tag warn'; }
      if (head) { head.textContent = 'Не готов'; head.className = 'tag warn'; head.hidden = false; }
      if (text) {
        text.textContent = profile.blocked_reason
          || 'Сначала соберите и проверьте механику FarmLoop Stage 1 на P1S.';
      }
      if (meta) meta.textContent = `Профиль ${profile.id || ''} · вход: ${profile.input_format || ''}`;
    }
  } catch (error) {
    if (tag) { tag.textContent = 'Нет связи'; tag.className = 'tag bad'; }
    if (head) { head.textContent = 'Нет связи'; head.className = 'tag bad'; head.hidden = false; }
    if (text) text.textContent = 'Не удалось проверить конвейер. Повторите после восстановления связи.';
    if (meta) meta.textContent = '';
  }
}

/* ------------------------------------------------------------ допуски */
function renderConveyorSettings() {
  put('cv_settings', settingGroup(FARMLOOP));
}

function renderConveyorGates() {
  const note = $('cv_gate_note');
  if (!note) return;
  if (!gates) { note.hidden = true; return; }
  const s = gates.settings || {};
  const chain = [];
  if (s.can_prepare) chain.push('шаблон');
  if (s.can_auto_next) chain.push('автоснятие');
  if (s.can_unattended_series) chain.push('серия без человека');
  if (!s.can_prepare) {
    note.hidden = false;
    note.className = 'cv-gate-note warn';
    note.textContent = '⚠ Конвейер не готов: ' + (gates.blocked_reason || 'нет установленного шаблона')
      + '. Автоматика включится, когда будут все допуски.';
    return;
  }
  if (s.can_auto_next && s.can_unattended_series) {
    note.hidden = false;
    note.className = 'cv-gate-note ok';
    note.textContent = '✓ Все допуски на месте: ' + chain.join(' → ')
      + '. Следующий цикл стартует после подтверждения пустой платформы.';
    return;
  }
  if (s.can_auto_next) {
    note.hidden = false;
    note.className = 'cv-gate-note info';
    note.textContent = '✓ Автоснятие готово. Для серии без человека включите допуск '
      + '«Бесконтрольная серия» и задайте больше одного цикла.';
    return;
  }
  note.hidden = false;
  note.className = 'cv-gate-note warn';
  note.textContent = '⚠ Шаблон на месте, но автоснятие не включено: '
    + (gates.blocked_reason || 'не подтверждены механика и пустая платформа')
    + '. Пока деталь снимает человек.';
}

async function loadConveyorGates() {
  try {
    const data = await get('/api/farmloop/settings');
    gates = data;
    renderConveyorGates();
  } catch (error) {
    const note = $('cv_gate_note');
    if (note) {
      note.hidden = false;
      note.className = 'cv-gate-note warn';
      note.textContent = 'Гейты не подтянулись: ' + (error && error.message ? error.message : 'нет связи');
    }
  }
}

async function saveConveyorSettings() {
  const host = $('cv_settings');
  const btn = $('cv_save');
  if (!host) return;
  if (btn) btn.disabled = true;
  try {
    const payload = {};
    host.querySelectorAll('[data-setting]').forEach((el) => {
      const k = el.dataset.setting;
      if (el.type === 'checkbox') payload[k] = el.checked;
      else if (el.type === 'number') payload[k] = num(el.value);
      else payload[k] = el.value;
    });
    const res = await post('/api/settings', payload);
    PF.setSettings(res.settings || {});
    renderConveyorSettings();
    toast('Конвейер сохранён', 'Допуски и датчики применены');
    loadConveyorGates();
  } catch (error) {
    fail(error);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ------------------------------------------------------------ история */
async function loadConveyorHistory() {
  const host = $('cv_history');
  if (!host) return;
  let data;
  try {
    data = await get('/api/events', { kind: 'farmloop', limit: '30' });
  } catch (error) {
    host.innerHTML = `<div class="cv-history-empty">Журнал не подтянулся: `
      + `${esc(error && error.message ? error.message : 'нет связи')}</div>`;
    return;
  }
  const events = data.events || [];
  if (!events.length) {
    host.innerHTML = '<div class="cv-history-empty">Событий конвейера ещё нет. '
      + 'Здесь появятся подготовленные файлы серии и автоматические снятия детали.</div>';
    return;
  }
  host.innerHTML = events.map((e) =>
    `<div class="cv-event"><span class="cv-event-dot" aria-hidden="true"></span>`
    + `<div class="cv-event-body"><b>${esc(e.title || 'Событие')}</b>`
    + `<small>${esc(e.detail || '')} · ${dateTimeText(e.at || '')} · ${agoText(e.at || '')}</small></div></div>`
  ).join('');
}

/* ================================================================ bind */
function bind() {
  if (bound) return;
  bound = true;
  const save = $('cv_save');
  if (save) save.addEventListener('click', saveConveyorSettings);
  const refresh = $('cv_refresh');
  if (refresh) refresh.addEventListener('click', refreshAll);
}

async function refreshAll() {
  renderConveyorSettings();
  await Promise.all([loadConveyorStatus(), loadConveyorGates(), loadConveyorHistory()]);
}

PF.module('conveyor', () => {
  bind();
  refreshAll();
});

PF.on('view', (detail) => {
  if (!detail || detail.view !== 'conveyor') return;
  bind();
  if (!gates) refreshAll();
});
})();
