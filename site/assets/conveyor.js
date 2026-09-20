/* PrintFlow 18.9 — раздел «Конвейер» (FarmLoop Stage 1, P1S).
   Вкладка объединяет сквозной процесс: мастер «Подготовка серии»,
   блочный конструктор финала G-code, тестовую очистку стола и safety-gate
   допусков автоматики.

   Грузится лениво, как остальные тяжёлые разделы. */
(() => {
'use strict';

const { $, esc, num, toast, fail, agoText, dateTimeText, confirmDanger } = PF.ui;
const { get, post } = PF.api;
const { settingGroup } = PF.modules.settings;

/* Группа конвейера: ключи те же, что в DEFAULT_SETTINGS, —
   контракт test_farmloop_settings_ui держит полноту. */
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
let conveyorSpools = [];
let conveyorModels = [];
let prepFile = null;
let prepSliced = null;

const put = (id, html) => {
  const el = $(id);
  if (el) el.innerHTML = html;
  return el;
};

function slStemOf(name) {
  const base = String(name || '').split('/').pop() || '';
  return base.replace(/\.[^.]+$/, '');
}

function parseAmsSlot(raw) {
  const s = String(raw ?? '').trim();
  if (!s) return null;
  const m = s.match(/(\d+)\s*$/);
  const cand = m ? m[1] : s;
  const n = Number(cand);
  if (!Number.isFinite(n)) return null;
  const iv = Math.trunc(n);
  if ((iv >= 0 && iv <= 15) || iv === 254) return iv;
  return null;
}

function filterUsableSpools(list) {
  return (list || []).filter((sp) => {
    if (sp.archived) return false;
    const rem = Number(sp.remaining_grams);
    if (Number.isFinite(rem) && rem <= 0) return false;
    return true;
  });
}

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

/* ------------------------------------------------------------ тестовая очистка стола */
/* 18.12.1: станок выбирает оператор, а не «первый в словаре». Раньше панель
   слала {confirmed: true} без printer_id, сервер брал первый принтер пула —
   занятый или отключённый, — и кнопка отвечала «Принтер занят» при живом
   свободном станке рядом. Список тянется из /api/state: ✓ = подключен и
   свободен (IDLE/FINISH), · = занят или не в сети. */
let testCleanPrinter = '';

function testCleanSelectable(p) {
  const connected = !!(p.connection && p.connection.connected);
  const state = String((p.printer && p.printer.state) || '').toUpperCase();
  return connected && (state === 'IDLE' || state === 'FINISH');
}

async function loadTestCleanPrinters() {
  const sel = $('cv_test_clean_printer');
  if (!sel) return;
  let printers = [];
  try {
    const state = await get('/api/state');
    printers = Array.isArray(state.printers) ? state.printers : [];
  } catch (err) {
    sel.innerHTML = '<option value="">Станок: первый свободный</option>';
    sel.disabled = true;
    sel.title = 'Список станков не подтянулся — сервер выберет первый свободный сам';
    return;
  }
  sel.disabled = false;
  const opts = ['<option value="">Станок: первый свободный</option>'];
  printers.forEach((p) => {
    const id = String(p.id || '');
    if (!id) return;
    const free = testCleanSelectable(p);
    const stateLabel = String((p.printer && (p.printer.state_label || p.printer.state)) || 'нет связи');
    opts.push(`<option value="${esc(id)}">${esc(p.name || id)} — ${esc(stateLabel)} ${free ? '✓' : '·'}</option>`);
  });
  sel.innerHTML = opts.join('');
  sel.value = printers.some((p) => String(p.id || '') === testCleanPrinter) ? testCleanPrinter : '';
  testCleanPrinter = sel.value;
  sel.title = printers.length
    ? '✓ — станок подключен и свободен (IDLE/FINISH), · — занят или не в сети'
    : 'Станков в парке нет — добавьте принтер в разделе «Принтеры»';
}

async function handleTestClean() {
  const sel = $('cv_test_clean_printer');
  const printerId = (sel && sel.value) || '';
  const where = printerId
    ? `на выбранном станке (${(sel.options[sel.selectedIndex] || {}).text || printerId})`
    : 'на первом свободном станке';
  const msg = 'Проверьте, что стол свободен, сопло остыло, корзина для деталей установлена.\n\n'
    + `Запустить тестовый проход толкателя по шаблону FarmLoop ${where}?`;
  if (!confirmDanger(msg)) return;

  const btn = $('cv_test_clean_btn');
  if (btn) btn.disabled = true;
  try {
    const payload = { confirmed: true };
    if (printerId) payload.printer_id = printerId;
    const res = await post('/api/farmloop/test-clean', payload);
    toast('Тестовая очистка стола', `Команд выполнено: ${res.executed_commands} на «${res.printer}»`);
    await Promise.all([loadConveyorHistory(), loadTestCleanPrinters()]);
  } catch (err) {
    fail(err);
    loadTestCleanPrinters();
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ------------------------------------------------------------ конструктор финала */
async function loadConstructorTemplate() {
  const tempEl = $('cv_c_temp');
  const zEl = $('cv_c_zlift');
  const yEl = $('cv_c_y');
  const speedEl = $('cv_c_speed');
  const fanEl = $('cv_c_fan');
  const customEl = $('cv_c_custom');
  const prevEl = $('cv_c_preview');
  if (!tempEl) return;
  try {
    const res = await get('/api/farmloop/template');
    const b = res.blocks || {};
    if (tempEl) tempEl.value = b.cooldown_temp || 35;
    if (fanEl) fanEl.checked = !!b.fan_assist;
    if (zEl) zEl.value = b.z_lift || 15;
    if (yEl) yEl.value = b.pusher_y || 245;
    if (speedEl) speedEl.value = b.pusher_speed || 2400;
    if (customEl) customEl.value = b.custom_gcode || '';
    if (prevEl) prevEl.textContent = res.gcode || '';
  } catch (err) {
    // не роняем панель при сетевой ошибке
  }
}

async function saveConstructorTemplate(resetDefault = false) {
  const btn = $('cv_c_save');
  const rBtn = $('cv_c_reset');
  const st = $('cv_c_status');
  if (btn) btn.disabled = true;
  if (rBtn) rBtn.disabled = true;
  if (st) st.textContent = 'Сохраняем шаблон…';

  try {
    let payload;
    if (resetDefault) {
      payload = { reset_default: true };
    } else {
      payload = {
        blocks: {
          cooldown_temp: num($('cv_c_temp')?.value, 35),
          fan_assist: !!$('cv_c_fan')?.checked,
          z_lift: num($('cv_c_zlift')?.value, 15),
          pusher_y: num($('cv_c_y')?.value, 245),
          pusher_speed: num($('cv_c_speed')?.value, 2400),
          custom_gcode: $('cv_c_custom')?.value || '',
        },
      };
    }
    const res = await post('/api/farmloop/template', payload);
    const prev = $('cv_c_preview');
    if (prev && res.gcode) prev.textContent = res.gcode;
    toast('Шаблон FarmLoop сохранён', `Команд в блоке: ${res.commands}`);
    if (st) st.textContent = '✓ Шаблон успешно сохранён и проверен';
    await Promise.all([loadConveyorStatus(), loadConveyorGates(), loadConstructorTemplate()]);
  } catch (err) {
    fail(err);
    if (st) st.textContent = `Ошибка: ${err.message || err}`;
  } finally {
    if (btn) btn.disabled = false;
    if (rBtn) rBtn.disabled = false;
  }
}

function toggleGcodePreview() {
  const prev = $('cv_c_preview');
  const btn = $('cv_c_preview_btn');
  if (!prev) return;
  prev.hidden = !prev.hidden;
  if (btn) btn.textContent = prev.hidden ? 'Показать итоговый G-code' : 'Скрыть G-code';
}

/* ------------------------------------------------------------ мастер подготовки серии */
async function loadWarehouseSpools() {
  try {
    const res = await get('/api/spools');
    conveyorSpools = filterUsableSpools(res.spools || []);
    renderSpoolOptions();
  } catch (err) {
    conveyorSpools = [];
  }
}

function renderSpoolOptions() {
  const sel = $('cv_prep_spool');
  if (!sel) return;
  const currentVal = sel.value;
  const opts = ['<option value="">Авто-подбор по материалу</option>'];
  filterUsableSpools(conveyorSpools).forEach((s) => {
    const slotNum = parseAmsSlot(s.ams_slot);
    const slot = slotNum != null ? ` · AMS ${slotNum}` : (s.ams_slot ? ` · AMS ${esc(s.ams_slot)}` : '');
    const rem = s.remaining_grams ? ` · ${Math.round(s.remaining_grams)}г` : '';
    opts.push(`<option value="${esc(s.id)}">${esc(s.material || 'PLA')} ${esc(s.color_name || '')}${slot}${rem}</option>`);
  });
  sel.innerHTML = opts.join('');
  if (currentVal) sel.value = currentVal;
}

function autoSelectSpool(material) {
  const sel = $('cv_prep_spool');
  if (!sel || !material) return;
  const matLower = String(material).toLowerCase();
  const usable = filterUsableSpools(conveyorSpools);
  const found = usable.find((s) =>
    String(s.material || '').toLowerCase() === matLower && (Number(s.remaining_grams) || 0) > 0
  );
  if (found) sel.value = found.id;
}

async function loadLibraryModels() {
  const sel = $('cv_library_select');
  if (!sel) return;
  try {
    const res = await get('/api/library', { kind: 'stl' });
    conveyorModels = res.files || [];
    const opts = ['<option value="">Или выберите модель из библиотеки…</option>'];
    conveyorModels.forEach((m) => {
      opts.push(`<option value="${esc(m.id)}">${esc(m.name || m.id)}</option>`);
    });
    sel.innerHTML = opts.join('');
  } catch (err) {
    if (sel) sel.innerHTML = '<option value="">Не удалось загрузить модели</option>';
  }
}

async function selectModel(id, name) {
  if (!id) {
    resetPrepMaster();
    return;
  }
  prepFile = { type: 'mesh', id, name: name || id };
  const pBox = $('cv_prep_params');
  const rBox = $('cv_prep_result');
  const st = $('cv_prep_status');
  if (pBox) pBox.hidden = false;
  if (rBox) rBox.hidden = true;
  if (st) st.textContent = 'Считаем план модели…';

  try {
    const plan = await post('/api/slicer/plan', { id });
    if (st) {
      const b = plan.model && plan.model.box_mm ? plan.model.box_mm.map((x) => Math.round(x)).join('×') + ' мм' : '';
      st.textContent = `✓ Модель готова (${b}). Проверьте параметры и нажмите «Нарезать модель».`;
    }
    autoSelectSpool((plan.settings && plan.settings.material) || 'PLA');
  } catch (err) {
    if (st) st.textContent = `План: ${err.message || err}`;
  }
}

async function handleFileUpload(file) {
  if (!file) return;
  const name = String(file.name || '');
  const isMesh = /\.(stl|obj)$/i.test(name);
  const isGcodeOr3mf = /\.(3mf|gcode(?:\.3mf)?)$/i.test(name);
  if (!isMesh && !isGcodeOr3mf) {
    fail(new Error('Поддерживаются файлы .stl, .obj, .3mf и .gcode'));
    return;
  }
  const dzSub = document.querySelector('.cv-dropzone-sub');
  if (dzSub) dzSub.textContent = `Загружаем «${name}»…`;
  try {
    if (isMesh) {
      const form = new FormData();
      form.append('file', file);
      const res = await post('/api/library/upload', form);
      await loadLibraryModels();
      const sel = $('cv_library_select');
      if (sel) sel.value = res.id;
      await selectModel(res.id, res.name);
    } else {
      const form = new FormData();
      form.append('file', file);
      const res = await post('/api/estimate/upload', form);
      handleGcodeReady({
        file: res.file || name,
        grams: num(res.grams) || (res.estimate && num(res.estimate.total_grams)) || 0,
        minutes: num(res.minutes) || (res.estimate && num(res.estimate.total_minutes)) || 0,
        material: res.material || '',
        layers: (res.estimate && res.estimate.layers) || 0,
      });
    }
  } catch (err) {
    fail(err);
  } finally {
    if (dzSub) dzSub.textContent = 'или нажмите кнопку для выбора файла с компьютера';
  }
}

function handleGcodeReady(info) {
  const c = Math.max(1, Math.min(100, num($('cv_prep_cycles')?.value, 1)));
  const spoolSel = $('cv_prep_spool');
  const spool = filterUsableSpools(conveyorSpools).find((s) => s.id === (spoolSel && spoolSel.value)) || null;
  prepSliced = {
    output: info.file,
    stem: slStemOf(info.file),
    minutes: info.minutes || 0,
    grams: info.grams || 0,
    layers: info.layers || 0,
    material: (spool && spool.material) || info.material || 'PLA',
    spool,
    cycles: c,
    farmloop_profile: 'bambu-p1s-farmloop-stage1',
  };
  renderPrepResult(prepSliced);
}

async function handleSlice() {
  if (!prepFile || prepFile.type !== 'mesh') return;
  const btn = $('cv_prep_slice');
  const st = $('cv_prep_status');
  if (btn) btn.disabled = true;
  if (st) st.textContent = 'Нарезаем модель Stage 1…';

  const layer = num($('cv_prep_layer')?.value, 0.20);
  const infill = num($('cv_prep_infill')?.value, 15);
  const cycles = Math.max(1, Math.min(100, num($('cv_prep_cycles')?.value, 1)));
  const spoolId = $('cv_prep_spool')?.value || '';
  const isFarm = $('cv_prep_farm')?.checked;

  const payload = {
    id: prepFile.id,
    layer_height: layer,
    infill_percent: infill,
    cycles,
  };
  const spool = filterUsableSpools(conveyorSpools).find((s) => s.id === spoolId);
  if (spool) {
    payload.spool_id = spool.id;
    if (spool.material) payload.material = spool.material;
    const slotNum = parseAmsSlot(spool.ams_slot);
    if (slotNum != null) payload.ams_slot = slotNum;
    else if (spool.ams_slot) payload.ams_slot = spool.ams_slot;
  }
  if (isFarm) {
    payload.farmloop_profile = (gates && gates.profile) || 'bambu-p1s-farmloop-stage1';
  }

  try {
    const res = await post('/api/slicer/slice', payload);
    const report = res.report || {};
    prepSliced = {
      output: res.output,
      stem: slStemOf(res.output),
      minutes: report.minutes || 0,
      grams: report.grams || 0,
      layers: report.layers || 0,
      material: (spool && spool.material) || report.material || 'PLA',
      spool,
      cycles,
      farmloop_profile: payload.farmloop_profile || '',
    };
    renderPrepResult(prepSliced);
    toast('Модель нарезана', `${report.minutes || 0} мин · ${report.grams || 0} г`);
  } catch (err) {
    fail(err);
    if (st) st.textContent = `Ошибка нарезки: ${err.message || err}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderPrepResult(data) {
  const pBox = $('cv_prep_params');
  const rBox = $('cv_prep_result');
  const kpis = $('cv_prep_kpis');
  const enqBtn = $('cv_prep_enqueue');
  const dlBtn = $('cv_prep_download');
  if (pBox) pBox.hidden = true;
  if (rBox) rBox.hidden = false;

  const c = data.cycles || 1;
  const slotNum = data.spool ? parseAmsSlot(data.spool.ams_slot) : null;
  const spoolDesc = data.spool ? `${data.spool.material} ${data.spool.color_name || ''} (AMS: ${slotNum != null ? slotNum : (data.spool.ams_slot || '—')})` : 'По умолчанию';

  if (kpis) {
    kpis.innerHTML = `
      <div class="cv-prep-kpi"><span>Время печати</span><b>≈ ${data.minutes} мин</b></div>
      <div class="cv-prep-kpi"><span>Расход пластика</span><b>≈ ${data.grams} г</b></div>
      <div class="cv-prep-kpi"><span>Слоёв</span><b>${data.layers || '—'}</b></div>
      <div class="cv-prep-kpi"><span>Катушка склада</span><b>${esc(spoolDesc)}</b></div>
      <div class="cv-prep-kpi"><span>Файл</span><b style="font-size:12px;overflow:hidden;text-overflow:ellipsis">${esc(data.output)}</b></div>
      <div class="cv-prep-kpi"><span>Блок FarmLoop</span><b style="color:var(--ok)">✓ Внедрён</b></div>
    `;
  }
  if (enqBtn) {
    enqBtn.textContent = `В конвейер (${c} детал${c === 1 ? 'ь' : (c < 5 ? 'и' : 'ей')})`;
  }
  if (dlBtn && data.output) {
    dlBtn.hidden = false;
    dlBtn.href = `/api/uploads?file=${encodeURIComponent(data.output)}`;
  }
}

async function handleEnqueueSeries() {
  if (!prepSliced) return;
  const c = Math.max(1, Math.min(100, num(prepSliced.cycles) || 1));
  const msg = `Поставить в конвейер серию из ${c} деталей подряд?\n\n`
    + `Задания будут запускаться автоматически, пока конвейер подтверждает сброс деталей.`;
  if (!confirmDanger(msg)) return;

  const btn = $('cv_prep_enqueue');
  if (btn) btn.disabled = true;
  try {
    const spool = prepSliced.spool;
    const slotNum = spool ? parseAmsSlot(spool.ams_slot) : null;
    const payload = {
      file: prepSliced.output,
      name: prepSliced.stem || prepSliced.output,
      plate: 1,
      no_auto: 1,
      allow_auto_start: false,
      cycles: c,
      source: 'printflow-conveyor',
      farmloop_profile: prepSliced.farmloop_profile || 'bambu-p1s-farmloop-stage1',
      est_minutes: prepSliced.minutes || 0,
      est_grams: prepSliced.grams || 0,
      material: (spool && spool.material) || prepSliced.material || '',
      spool_id: (spool && spool.id) || '',
      ams_mapping: slotNum != null ? [slotNum] : [],
    };
    await post('/api/jobs/enqueue', payload);
    toast('Серия в конвейере', `${c} одинаковых заданий поставлены в очередь.`);
    await loadConveyorHistory();
    resetPrepMaster();
  } catch (err) {
    fail(err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function resetPrepMaster() {
  prepFile = null;
  prepSliced = null;
  const pBox = $('cv_prep_params');
  const rBox = $('cv_prep_result');
  const sel = $('cv_library_select');
  const fileInput = $('cv_file_input');
  const st = $('cv_prep_status');
  const dl = $('cv_prep_download');
  if (pBox) pBox.hidden = true;
  if (rBox) rBox.hidden = true;
  if (sel) sel.value = '';
  if (fileInput) fileInput.value = '';
  if (st) st.textContent = '';
  if (dl) dl.hidden = true;
}

function initDropzone() {
  const dz = $('cv_dropzone');
  const fileInput = $('cv_file_input');
  const browseBtn = $('cv_browse_btn');
  if (!dz || dz._bound) return;
  dz._bound = true;

  if (browseBtn && fileInput) {
    browseBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      fileInput.click();
    });
  }
  dz.addEventListener('click', () => {
    if (fileInput) fileInput.click();
  });
  if (fileInput) {
    fileInput.addEventListener('change', () => {
      if (fileInput.files && fileInput.files[0]) {
        handleFileUpload(fileInput.files[0]);
        fileInput.value = '';
      }
    });
  }

  ['dragenter', 'dragover'].forEach((type) => {
    dz.addEventListener(type, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.add('dragover');
    });
  });
  ['dragleave', 'drop'].forEach((type) => {
    dz.addEventListener(type, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.remove('dragover');
    });
  });
  dz.addEventListener('drop', (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files[0]) {
      handleFileUpload(files[0]);
    }
  });
}

/* ================================================================ bind */
function bind() {
  if (bound) return;
  bound = true;

  const save = $('cv_save');
  if (save) save.addEventListener('click', saveConveyorSettings);

  const refresh = $('cv_refresh');
  if (refresh) refresh.addEventListener('click', refreshAll);

  const testClean = $('cv_test_clean_btn');
  if (testClean) testClean.addEventListener('click', handleTestClean);

  // Выбор станка запоминается: обновление списка не сбрасывает его.
  const testCleanSel = $('cv_test_clean_printer');
  if (testCleanSel) testCleanSel.addEventListener('change', () => {
    testCleanPrinter = testCleanSel.value;
  });

  const cSave = $('cv_c_save');
  if (cSave) cSave.addEventListener('click', () => saveConstructorTemplate(false));

  const cReset = $('cv_c_reset');
  if (cReset) cReset.addEventListener('click', () => saveConstructorTemplate(true));

  const cPrev = $('cv_c_preview_btn');
  if (cPrev) cPrev.addEventListener('click', toggleGcodePreview);

  const libSel = $('cv_library_select');
  if (libSel) libSel.addEventListener('change', () => selectModel(libSel.value));

  const libRef = $('cv_library_refresh');
  if (libRef) libRef.addEventListener('click', loadLibraryModels);

  const sliceBtn = $('cv_prep_slice');
  if (sliceBtn) sliceBtn.addEventListener('click', handleSlice);

  const cancelBtn = $('cv_prep_cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', resetPrepMaster);

  const enqBtn = $('cv_prep_enqueue');
  if (enqBtn) enqBtn.addEventListener('click', handleEnqueueSeries);

  const resReset = $('cv_prep_reset');
  if (resReset) resReset.addEventListener('click', resetPrepMaster);

  initDropzone();
}

async function refreshAll() {
  renderConveyorSettings();
  await Promise.all([
    loadConveyorStatus(),
    loadConveyorGates(),
    loadConveyorHistory(),
    loadConstructorTemplate(),
    loadLibraryModels(),
    loadWarehouseSpools(),
    loadTestCleanPrinters(),
  ]);
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
