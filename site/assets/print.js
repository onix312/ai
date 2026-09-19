/* PrintFlow 17.1 — раздел «Печать» (идея: печатный контур цеха).
   Каталог печатных форм приходит из реестра на сервере (`/api/print/forms`
   вместе с параметрами каждой формы), лист собирает сервер: на бумаге форма
   одинаковая и с телефона, и с ПК. Модуль только рисует каталог, собирает
   параметры и открывает готовый лист в окне печати — своих размеров,
   шаблонов и списков стикеров в браузере нет.

   Грузится лениво (идея 47): раздел открывают перед тиражом, а не в смену. */
(() => {
'use strict';

const { $, $$, esc, num, nfmt, toast, fail, render, html, raw,
        minutesText, confirmDanger } = PF.ui;
const { get, post } = PF.api;

let forms = [];
let groups = [];
let loaded = false;
let loading = false;
let farmloopLoaded = false;
let slicerLoaded = false;
let slicerReady = false;      // свой движок выбран и доступен (гейт engine)
let slicerFarm = null;        // 18.8: конвейерные гейты из /api/slicer/profile
let slicerModels = [];        // модели STL из библиотеки
let slicerSpools = [];        // 18.8: катушки склада пластика для нарезки
let slicerLast = null;        // результат последней нарезки: {output, stem, report, machine}

/* ============================================================ окно печати */
/* Печать серверного листа. Своё окно, а не iframe: браузер печатает его
   нативными настройками — масштаб и поля печатающей стороны остаются у
   оператора, а не «зашиты» в разметку. */
function printWindow(markup, title) {
  const popup = window.open('', '_blank', 'width=900,height=1100');
  if (!popup) {
    toast('Окно печати заблокировано', 'Разрешите всплывающие окна для PrintFlow', 'warn');
    return;
  }
  const source = /<html[\s>]/i.test(markup)
    ? markup
    : `<!doctype html><html lang="ru"><head><meta charset="utf-8">`
      + `<title>${esc(title || 'PrintFlow')}</title></head><body>${markup}</body></html>`;
  popup.document.write(source);
  popup.document.close();
  popup.focus();
  setTimeout(() => { try { popup.print(); } catch (e) { /* оператор нажмёт «Печать» сам */ } }, 450);
}

/* ====================================================== разбор параметров */
/* У каждой формы свой набор параметров, и он описан на сервере. Здесь мы
   просто превращаем описание в поля: `choices` → select, `source` →
   справочник из состояния панели, `min/max` → число, `text` → строка. */
function choiceField(name, spec) {
  const options = (spec.choices || []).map((choice) =>
    `<option value="${esc(choice.value)}">${esc(choice.label)}</option>`).join('');
  return `<label class="pr-field"><span>${esc(spec.title || name)}</span>`
    + `<select class="field" data-opt="${esc(name)}">${options}</select></label>`;
}

function sourceField(name, spec) {
  if (spec.source === 'customers') {
    const list = (PF.state.customers || []).map((c) =>
      `<option value="${esc(c.id)}">${esc(c.name || ('Клиент ' + c.id))}</option>`).join('');
    return `<label class="pr-field"><span>${esc(spec.title || name)}</span>`
      + `<select class="field" data-opt="${esc(name)}" data-source="customers">`
      + `<option value="">${esc(spec.empty || '—')}</option>${list}</select></label>`;
  }
  // Заказ выбирается по номеру: в цехе его называют именно так.
  const list = (PF.state.orders || []).map((o) =>
    `<option value="${esc(o.id)}">№${esc(o.number || o.id)} · ${esc(o.title || o.item || '')}</option>`).join('');
  return `<label class="pr-field"><span>${esc(spec.title || name)}</span>`
    + `<select class="field" data-opt="${esc(name)}" data-source="orders"${spec.required ? ' data-required="1"' : ''}>`
    + `<option value="">— выберите заказ —</option>${list}</select></label>`;
}

function fieldHtml(name, spec) {
  if (spec.choices) return choiceField(name, spec);
  if (spec.source) return sourceField(name, spec);
  if (spec.text) {
    return `<label class="pr-field"><span>${esc(spec.title || name)}</span>`
      + `<input class="field" data-opt="${esc(name)}" placeholder="${esc(spec.placeholder || '')}"></label>`;
  }
  const min = spec.min === undefined ? 0 : spec.min;
  return `<label class="pr-field"><span>${esc(spec.title || name)}</span>`
    + `<input class="field" type="number" inputmode="numeric" data-opt="${esc(name)}"`
    + ` min="${esc(min)}"${spec.max === undefined ? '' : ` max="${esc(spec.max)}"`}`
    + ` value="${esc(min)}"></label>`;
}

function cardHtml(form) {
  const options = form.options || {};
  const controls = Object.keys(options).map((name) => fieldHtml(name, options[name])).join('');
  const isApi = form.kind === 'api' && form.api;
  // Кнопка, путь к форме и блок полей — уже готовая разметка: экранируем
  // значения внутри, а сюда подставляем через raw(), иначе шаблон ядра
  // заэкранирует сам тег.
  const action = isApi
    ? `<button class="btn primary" type="button" data-print="${esc(form.id)}">Печать листа</button>`
    : `<a class="btn" href="${esc(form.page || '#print')}" target="_blank" rel="noopener">Открыть страницу →</a>`;
  const controlsBlock = controls ? `<div class="pr-card-controls">${controls}</div>` : '';
  const pathBlock = isApi ? `<span class="pr-card-path">${esc(form.api)}</span>` : '';
  return html`
   <article class="pr-card" data-form="${form.id}">
    <div class="pr-card-head">
     <div><span class="card-kicker">${form.group}</span><h2>${form.title}</h2></div>
     <span class="tag">${form.size}</span>
    </div>
    <p class="pr-card-note">${form.note}</p>
    <dl class="pr-card-facts">
     <div><dt>Бумага</dt><dd>${form.paper}</dd></div>
     <div><dt>Размер</dt><dd>${form.size}</dd></div>
     <div><dt>Данные</dt><dd>${form.source}</dd></div>
    </dl>
    ${raw(controlsBlock)}
    <div class="pr-card-foot">${raw(action)}
     ${raw(pathBlock)}
    </div>
   </article>`;
}

function showError(message) {
  const box = $('pr_error');
  const text = $('pr_error_text');
  if (!box || !text) return;
  text.textContent = message;
  box.hidden = false;
}

function clearError() {
  const box = $('pr_error');
  if (box) box.hidden = true;
}

async function loadFarmLoopStatus() {
  // 18.8: настройки и журнал конвейера живут во вкладке «Конвейер»; здесь —
  // только статус-ссылка, чтобы с «Печати» было куда перейти.
  if (farmloopLoaded) return;
  const text = $('pr_farmloop_text');
  const tag = $('pr_farmloop_tag');
  try {
    const data = await get('/api/farmloop/profile');
    farmloopLoaded = true;
    if (data.template_installed) {
      if (tag) { tag.textContent = 'Профиль готов'; tag.className = 'tag ok'; }
      if (text) text.textContent = 'Конвейер готов к серии — допуски, датчики и журнал во вкладке «Конвейер» →';
    } else {
      if (tag) { tag.textContent = 'Ждёт шаблон'; tag.className = 'tag warn'; }
      if (text) text.textContent = data.blocked_reason || 'Сначала установите и проверьте механику FarmLoop Stage 1 на P1S.';
    }
  } catch (error) {
    if (tag) { tag.textContent = 'Нет связи'; tag.className = 'tag bad'; }
    if (text) text.textContent = 'Не удалось проверить конвейер. Повторите после восстановления связи.';
  }
}

/* Свой движок нарезки PrintFlow. Карточка честно говорит, чем сейчас
   режем: своим движком или внешним CLI, и почему. Обещаний «всё готово»
   здесь нет — причина блокировки приходит с сервера. */
async function loadSlicerStatus() {
  const text = $('pr_slicer_text');
  const meta = $('pr_slicer_meta');
  const tag = $('pr_slicer_tag');
  if (!text) return;
  try {
    const engine = await get('/api/slicer/engine');
    const profile = await get('/api/slicer/profile');
    slicerLoaded = true;
    slicerFarm = profile.farmloop || null; // 18.8: конвейерные гейты
    const settings = profile.settings || {};
    if (meta) {
      meta.textContent = `printflow v${engine.version} · STL до ${engine.limits.model_mb} МБ · `
        + `слой ${settings.layer_height} мм · стенок ${settings.walls}`;
    }
    slicerReady = !!profile.can_slice;
    if (profile.can_slice) {
      if (tag) { tag.textContent = 'Движок готов'; tag.className = 'tag ok'; }
      if (text) {
        text.textContent = 'Нарезает STL сам: файл появится в библиотеке, '
          + 'а в очередь его ставит оператор после отчёта.';
      }
    } else {
      if (tag) { tag.textContent = 'Внешний CLI'; tag.className = 'tag warn'; }
      if (text) {
        text.textContent = profile.blocked_reason
          || 'Свой движок не выбран: нарезка идёт внешним CLI-слайсером.';
      }
    }
  } catch (error) {
    slicerReady = false;
    slicerFarm = null;
    if (tag) { tag.textContent = 'Нет связи'; tag.className = 'tag bad'; }
    if (text) text.textContent = 'Не удалось проверить движок нарезки. Повторите после восстановления связи.';
    if (meta) meta.textContent = '';
  }
  updateSlicerButtons();
}

/* 18.8: катушки склада пластика — нарезка сразу знает, чем печататься:
   материал и температура подхватываются из катушки, а после печати
   расход спишется именно с неё (spool_id в задании). */
async function loadSlicerSpools() {
  const sel = $('pr_sl_spool');
  if (!sel) return;
  try {
    const data = await get('/api/spools');
    slicerSpools = (data.spools || []).filter((s) => num(s.remaining_grams) > 0);
    const current = sel.value;
    const options = ['<option value="">Катушка: как в настройках</option>']
      .concat(slicerSpools.map((s) => {
        const label = [s.material, s.color_name, `${nfmt(num(s.remaining_grams), 0)} г`,
          s.ams_slot ? `AMS ${s.ams_slot}` : 'в кладовой'].filter(Boolean).join(' · ');
        return `<option value="${esc(s.id)}">${esc(label)}</option>`;
      })).join('');
    sel.innerHTML = options;
    if (slicerSpools.some((s) => s.id === current)) sel.value = current;
  } catch (error) {
    slicerSpools = [];
    sel.innerHTML = '<option value="">Склад не загрузился</option>';
  }
}

/* ================================================== слайсер: рабочая часть
   18.8: карточка «Свой слайсер» перестала быть статусной — оператор
   выбирает модель (из библиотеки или с компьютера), смотрит план (аудит
   без записи), нарезает (G-code уходит в библиотеку, отчёт — на экран)
   и сам решает, ставить ли результат в очередь. Всё — через существующие
   маршруты: /api/library, /api/library/upload, /api/slicer/plan,
   /api/slicer/slice, /api/jobs/enqueue. */
function slModelSelect() { return $('pr_sl_model'); }

function updateSlicerButtons() {
  const sel = slModelSelect();
  const plan = $('pr_sl_plan');
  const slice = $('pr_sl_slice');
  const hasModel = !!(sel && sel.value);
  if (plan) plan.disabled = !hasModel;
  if (slice) slice.disabled = !hasModel || !slicerReady;
}

async function loadSlicerModels() {
  const sel = slModelSelect();
  if (!sel) return;
  try {
    const data = await get('/api/library', { kind: 'stl', limit: '300' });
    slicerModels = data.files || [];
    const current = sel.value;
    const options = ['<option value="">— выберите модель из библиотеки —</option>']
      .concat(slicerModels.map((m) =>
        `<option value="${esc(m.id)}">${esc(m.name)}`
        + (m.size ? ` · ${nfmt(m.size / 1048576, 1)} МБ` : '') + '</option>')).join('');
    sel.innerHTML = options;
    if (slicerModels.some((m) => m.id === current)) sel.value = current;
    if (!slicerModels.length) sel.innerHTML = '<option value="">В библиотеке нет моделей STL — загрузите первую</option>';
  } catch (error) {
    sel.innerHTML = '<option value="">Список моделей не загрузился</option>';
  }
  updateSlicerButtons();
}

async function uploadSlicerModel(file) {
  if (!file) return;
  const form = new FormData();
  form.append('file', file, file.name);
  const btn = $('pr_sl_upload_btn');
  if (btn) btn.disabled = true;
  try {
    const data = await post('/api/library/upload', form);
    const rec = data.library || {};
    toast('Модель в библиотеке', `${rec.name || file.name} · ${rec.id || ''}`);
    await loadSlicerModels();
    const sel = slModelSelect();
    if (sel && rec.id) { sel.value = rec.id; updateSlicerButtons(); }
  } catch (error) {
    toast('Модель не загружена', error && error.message ? error.message : String(error), 'bad');
  } finally {
    if (btn) btn.disabled = false;
    const input = $('pr_sl_file');
    if (input) input.value = '';
  }
}

function slWarningsHtml(warnings) {
  const list = (warnings || []).filter(Boolean);
  if (!list.length) return '';
  return `<ul class="pr-sl-warn">${list.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>`;
}

function renderSlicerPlan(data) {
  const box = $('pr_sl_result');
  if (!box) return;
  const m = data.model || {};
  const bbox = (m.bbox_mm || [0, 0, 0]).map((v) => nfmt(v, 1)).join(' × ');
  box.hidden = false;
  box.innerHTML = `<div class="pr-sl-result">
   <h3>План нарезки · ${esc(m.model || '')}</h3>
   <dl>
    <div><dt>Сетка</dt><dd>${nfmt(m.triangles)} треугольников · ${m.closed === false ? '<span class="pr-sl-bad">не замкнута</span>' : m.closed === null ? 'проверка недоступна' : 'замкнута'}</dd></div>
    <div><dt>Размер</dt><dd>${bbox} мм</dd></div>
    <div><dt>Слоёв</dt><dd>${nfmt(m.layers)}</dd></div>
    <div><dt>Стол</dt><dd>${m.fits_bed ? '<span class="pr-sl-ok">влезает</span>' : '<span class="pr-sl-bad">не влезает</span>'}</dd></div>
   </dl>
   ${slWarningsHtml(data.warnings)}
  </div>`;
}

function renderSlicerSlice(data) {
  const box = $('pr_sl_result');
  const acts = $('pr_sl_actions');
  if (!box) return;
  const r = data.report || {};
  const lib = data.library || {};
  const machine = data.machine || {};
  const minutes = num(r.minutes);
  const grams = num(r.weight_g);
  const spool = data.spool || null;
  const isFarm = !!data.farmloop;
  const cycles = Math.max(1, num($('pr_sl_cycles') && $('pr_sl_cycles').value) || 1);
  const farmNote = (slicerFarm && !slicerFarm.can_series && slicerFarm.blocked_reason)
    ? `<p class="pr-sl-gate">⚠ Серия не продолжит сама: ${esc(slicerFarm.blocked_reason)} — каждый цикл стартует вручную.</p>`
    : '';
  box.hidden = false;
  box.innerHTML = `<div class="pr-sl-result">
   <h3>${isFarm ? 'Нарезка для конвейера' : 'Нарезка завершена'} · ${esc(data.output || '')}</h3>
   <dl>
    <div><dt>Слоёв</dt><dd>${nfmt(r.layers)}</dd></div>
    <div><dt>Размер</dt><dd>${(r.bbox_mm || []).map((v) => nfmt(v, 1)).join(' × ')} мм</dd></div>
    <div><dt>Время</dt><dd>≈ ${minutes ? minutesText(minutes) : '—'}</dd></div>
    <div><dt>Пластик</dt><dd>≈ ${grams ? nfmt(grams, 1) + ' г' : '—'}${isFarm && cycles > 1 ? ` · серия ${nfmt(cycles)} шт` : ''}</dd></div>
    ${spool ? `<div><dt>Катушка</dt><dd>${esc([spool.material, spool.color_name].filter(Boolean).join(' '))} · остаток ${nfmt(num(spool.remaining_grams), 0)} г${spool.ams_slot ? ` · AMS ${esc(spool.ams_slot)}` : ' · в кладовой'}</dd></div>` : ''}
    <div><dt>Библиотека</dt><dd>${esc(lib.name || data.output || '')}</dd></div>
   </dl>
   ${slWarningsHtml(data.warnings)}
   ${farmNote}
  </div>`;
  slicerLast = {
    output: data.output || '',
    stem: slStemOf((data.model_audit && data.model_audit.model) || data.output),
    minutes, grams,
    material: (r.settings && r.settings.material) || '',
    machine,
    farm: isFarm,
    cycles,
    spool: spool ? { id: spool.id, ams_slot: spool.ams_slot || '', material: spool.material || '' } : null,
  };
  if (acts) {
    acts.hidden = false;
    const gate = machine.blocked_reason || '';
    acts.innerHTML =
      `<a class="btn sm" href="/api/uploads?file=${encodeURIComponent(data.output || '')}" download>Скачать .gcode</a>`
      + (isFarm
        ? `<button class="btn sm primary" id="pr_sl_conveyor" type="button">В конвейер${cycles > 1 ? ` (${nfmt(cycles)} цикл.)` : ''}</button>`
        : `<button class="btn sm primary" id="pr_sl_enqueue" type="button">В очередь</button>`)
      + (isFarm ? `<button class="btn sm" id="pr_sl_enqueue" type="button">В очередь (1 шт)</button>` : '')
      + (gate ? `<p class="pr-sl-gate">⚠ ${esc(gate)}</p>` : '');
    const conv = $('pr_sl_conveyor');
    if (conv) conv.addEventListener('click', () => enqueueSlicerSeries(cycles));
    const enq = $('pr_sl_enqueue');
    if (enq) enq.addEventListener('click', enqueueSlicerJob);
  }
}

/* Имя без папок и расширения — человеческое имя нарезанной детали. */
function slStemOf(name) {
  const base = String(name || '').split('/').pop() || '';
  return base.replace(/\.[^.]+$/, '');
}

/* Заглушка занятости: кнопка «работает», а статус строка говорит, что ищем. */
function slSetBusy(busy, label) {
  const plan = $('pr_sl_plan');
  const slice = $('pr_sl_slice');
  const status = $('pr_sl_status');
  if (plan && busy) plan.disabled = true;
  if (slice && busy) slice.disabled = true;
  if (status) status.textContent = busy ? (label || 'Работаем…') : '';
  if (!busy) updateSlicerButtons();
}

async function runSlicerPlan() {
  const sel = slModelSelect();
  if (!sel || !sel.value) return;
  slSetBusy(true, 'Считаем план: аудит модели без записи…');
  try {
    const data = await post('/api/slicer/plan', { id: sel.value });
    renderSlicerPlan(data);
  } catch (error) {
    const box = $('pr_sl_result');
    if (box) {
      box.hidden = false;
      box.innerHTML = `<div class="pr-sl-result"><span class="pr-sl-bad">План не посчитался: `
        + `${esc(error && error.message ? error.message : String(error))}</span></div>`;
    }
  } finally {
    slSetBusy(false);
  }
}

/* 18.8: что оператор выбрал в карточке — конвейер, циклы, катушка.
   Материал и AMS-слот нарезки подхватываются из катушки склада: нарезка
   получает температуру нужного пластика, а задание — списание по бобине. */
function slSlicePayload() {
  const payload = { id: slModelSelect().value };
  const farm = $('pr_sl_farm');
  const cyclesEl = $('pr_sl_cycles');
  const spoolSel = $('pr_sl_spool');
  const spool = slicerSpools.find((s) => s.id === (spoolSel && spoolSel.value)) || null;
  if (spool) {
    payload.spool_id = spool.id;
    if (spool.material) payload.material = spool.material;
    if (spool.ams_slot) payload.ams_slot = spool.ams_slot;
  }
  if (farm && farm.checked) {
    payload.farmloop_profile = (slicerFarm && slicerFarm.profile_id) || 'bambu-p1s-farmloop-stage1';
    payload.cycles = Math.max(1, Math.min(100, num(cyclesEl && cyclesEl.value) || 1));
  }
  return { payload, spool, cycles: num(cyclesEl && cyclesEl.value) || 1 };
}

async function runSlicerSlice() {
  const sel = slModelSelect();
  if (!sel || !sel.value || !slicerReady) return;
  slSetBusy(true, 'Нарезаем: G-code уходит в библиотеку, отчёт — сюда…');
  try {
    const { payload } = slSlicePayload();
    const data = await post('/api/slicer/slice', payload);
    renderSlicerSlice(data);
  } catch (error) {
    const box = $('pr_sl_result');
    if (box) {
      box.hidden = false;
      box.innerHTML = `<div class="pr-sl-result"><span class="pr-sl-bad">Нарезка не прошла: `
        + `${esc(error && error.message ? error.message : String(error))}</span></div>`;
    }
  } finally {
    slSetBusy(false);
  }
}

function slEnqueueBase(extra) {
  // Общий скелет постановки из нарезки: файл, смета, катушка склада.
  // 18.8: spool_id связывает задание со складом — расход спишется с бобины,
  // а ams_mapping берётся из её AMS-слота (если катушка стоит в AMS).
  const spool = (slicerLast && slicerLast.spool) || null;
  return Object.assign({
    file: slicerLast.output,
    name: slicerLast.stem,
    plate: 1,
    no_auto: 1,
    allow_auto_start: false,
    est_minutes: slicerLast.minutes || 0,
    est_grams: slicerLast.grams || 0,
    material: (spool && spool.material) || slicerLast.material || '',
    spool_id: (spool && spool.id) || '',
    ams_mapping: (spool && spool.ams_slot) ? [num(spool.ams_slot)] : [],
  }, extra || {});
}

async function enqueueSlicerJob() {
  if (!slicerLast || !slicerLast.output) return;
  const btn = $('pr_sl_enqueue');
  const message = `Поставить «${slicerLast.output}» в очередь печати?\n`
    + `Файл — из нарезки PrintFlow. Автостарта не будет: старт даст оператор.`;
  if (!confirmDanger(message)) return;
  if (btn) btn.disabled = true;
  try {
    const data = await post('/api/jobs/enqueue', slEnqueueBase({ source: 'printflow-slicer' }));
    toast('В очереди', `Задание ${data.job && data.job.id ? data.job.id : ''} · старт — вручную`);
    const acts = $('pr_sl_actions');
    if (acts) acts.innerHTML = `<span class="pr-sl-ok">Задание ${esc((data.job && data.job.id) || '')} поставлено в очередь · старт — вручную</span>`;
    // Само задание в раздел «Очередь» приедет с ближайшим снимком /api/stream.
  } catch (error) {
    toast('В очередь не поставлено', error && error.message ? error.message : String(error), 'bad');
    if (btn) btn.disabled = false;
  }
}

/* 18.8: серия конвейера — N одинаковых заданий одной транзакцией.
   После каждого снятия детали следующее задание стартует само, когда
   это разрешают допуски (вкладка «Конвейер»). Событие попадает в
   историю конвейера. */
async function enqueueSlicerSeries(cycles) {
  if (!slicerLast || !slicerLast.output) return;
  cycles = Math.max(1, Math.min(100, num(cycles) || 1));
  const btn = $('pr_sl_conveyor');
  const message = `Поставить «${slicerLast.output}» в конвейер серией из ${nfmt(cycles)} цикл(ов)?\n`
    + `Файл — из нарезки PrintFlow с FarmLoop-блоком. Автостарта не будет: первый старт даст оператор.`;
  if (!confirmDanger(message)) return;
  if (btn) btn.disabled = true;
  try {
    const data = await post('/api/jobs/enqueue', slEnqueueBase({
      source: 'printflow-conveyor',
      cycles,
    }));
    const job = data.job || {};
    toast('В конвейере', `Серия ${nfmt(cycles)} цикл(ов) · первое задание ${job.id || ''}`);
    const acts = $('pr_sl_actions');
    if (acts) {
      acts.innerHTML = `<span class="pr-sl-ok">Серия ${nfmt(cycles)} цикл(ов) в очереди · `
        + `событие — в истории «Конвейера» · первый старт — вручную</span>`;
    }
    // Задания приедут в «Очередь» с ближайшим снимком /api/stream.
  } catch (error) {
    toast('В конвейер не поставлено', error && error.message ? error.message : String(error), 'bad');
    if (btn) btn.disabled = false;
  }
}

/* =============================================================== каталог */
function renderKpis() {
  const host = $('pr_kpis');
  if (!host) return;
  const stickers = forms.find((f) => f.id === 'stickers');
  const kinds = stickers && stickers.options && stickers.options.kind
    ? Math.max(0, (stickers.options.kind.choices || []).length - 1) : 0;
  const sizes = stickers && stickers.options && stickers.options.size
    ? (stickers.options.size.choices || []).length : 0;
  render(host, [
    ['Форм в каталоге', forms.length, groups.length + ' групп цеха'],
    ['Шаблонов стикеров', kinds, sizes + ' размеров наклейки'],
    ['На листе A4', '100 %', 'линейка для проверки масштаба'],
  ].map(([label, value, sub]) =>
    `<div class="kpi"><small>${esc(label)}</small><b class="value">${typeof value === 'number' ? nfmt(value) : esc(value)}</b><span class="sub">${esc(sub)}</span></div>`).join(''));
}

function renderCatalog() {
  const host = $('pr_forms');
  if (!host) return;
  const tag = $('pr_tag');
  if (tag) {
    tag.textContent = forms.length ? `${forms.length} форм` : '';
    tag.hidden = !forms.length;
  }
  renderKpis();
  if (!forms.length) {
    render(host, '<div class="empty pr-empty"><span class="big">▤</span><b>Каталог пока пуст</b>'
      + '<span>Сервер не отдал ни одной формы. Проверьте журнал коннектора и попробуйте ещё раз.</span>'
      + '<button class="btn sm" type="button" data-print-retry>Повторить загрузку</button></div>');
    return;
  }
  // Группы идут в том порядке, в каком их отдал реестр: это порядок работы цеха.
  const blocks = groups.map((group) => {
    const items = forms.filter((f) => f.group === group).map(cardHtml).join('');
    return `<section class="pr-group"><div class="pr-group-title">${esc(group)}</div>`
      + `<div class="pr-group-grid">${items}</div></section>`;
  }).join('');
  render(host, blocks);
  fillSources();
}

/* Справочники панели живут в состоянии: если клиенты или заказы приехали
   позже каталога, подставляем их в уже нарисованные списки, не теряя
   выбранные оператором значения. */
function fillSources() {
  $$('#pr_forms select[data-source="orders"]').forEach((select) => {
    const value = select.value;
    const list = (PF.state.orders || []).map((o) =>
      `<option value="${esc(o.id)}">№${esc(o.number || o.id)} · ${esc(o.title || o.item || '')}</option>`).join('');
    select.innerHTML = `<option value="">— выберите заказ —</option>${list}`;
    if (value) select.value = value;
  });
  $$('#pr_forms select[data-source="customers"]').forEach((select) => {
    const value = select.value;
    const list = (PF.state.customers || []).map((c) =>
      `<option value="${esc(c.id)}">${esc(c.name || ('Клиент ' + c.id))}</option>`).join('');
    select.innerHTML = `<option value="">Без персонализации</option>${list}`;
    if (value) select.value = value;
  });
}

async function loadCatalog(options = {}) {
  if (loading) return;
  loading = true;
  clearError();
  const refresh = $('pr_refresh');
  const host = $('pr_forms');
  if (refresh) {
    refresh.disabled = true;
    refresh.setAttribute('aria-busy', 'true');
  }
  if (host && !loaded) {
    host.innerHTML = '<div class="pr-loading"><div class="skeleton" style="height:190px"></div><span>Загружаем каталог форм…</span></div>';
  }
  try {
    const data = await get('/api/print/forms');
    forms = Array.isArray(data.forms) ? data.forms : [];
    groups = Array.isArray(data.groups) ? data.groups : [];
    loaded = true;
    renderCatalog();
  } catch (error) {
    if (!options.quiet) {
      showError('Каталог форм не загрузился: ' + (error && error.message ? error.message : error));
      fail(error);
    }
  } finally {
    loading = false;
    if (refresh) {
      refresh.disabled = false;
      refresh.removeAttribute('aria-busy');
    }
  }
}

/* ============================================================== печать */
function readParams(card, form) {
  const params = {};
  (form.options ? Object.keys(form.options) : []).forEach((name) => {
    const spec = form.options[name];
    const el = card ? card.querySelector(`[data-opt="${name}"]`) : null;
    let value = el && el.value !== undefined && el.value !== null ? String(el.value) : '';
    if (!value && spec.choices && spec.choices.length) value = String(spec.choices[0].value);
    if (!value && spec.min !== undefined) value = String(spec.min);
    params[name] = value;
    if (spec.required && !value) {
      throw new Error('Заполните поле «' + (spec.title || name) + '»');
    }
  });
  return params;
}

async function fetchSheet(url, params) {
  const query = new URLSearchParams();
  Object.keys(params).forEach((key) => {
    if (params[key] !== '') query.set(key, params[key]);
  });
  const target = query.toString() ? url + '?' + query.toString() : url;
  const res = await fetch(target, { headers: { Accept: 'text/html, application/json' } });
  const text = await res.text();
  let payload = null;
  if (text.trim().startsWith('{')) {
    try { payload = JSON.parse(text); } catch (e) { payload = null; }
  }
  if (!res.ok || (payload && payload.error)) {
    throw new Error((payload && payload.error) || ('Сервер ответил ' + res.status));
  }
  // Часть форм отдаёт готовую страницу целиком (карточка упаковки, документы),
  // остальные — JSON с разметкой листа.
  return payload && payload.html ? payload.html : text;
}

async function printForm(formId, button) {
  const form = forms.find((f) => f.id === formId);
  if (!form) return;
  const card = button && button.closest ? button.closest('.pr-card') : null;
  let params;
  try {
    params = readParams(card, form);
  } catch (error) {
    toast('Не хватает данных', error.message, 'warn');
    return;
  }
  try {
    if (button) button.disabled = true;
    if (button) {
      button.classList.add('is-busy');
      button.setAttribute('aria-busy', 'true');
      button.textContent = 'Готовим лист…';
    }
    const markup = await fetchSheet(form.api, params);
    printWindow(markup, form.title);
    toast('Лист подготовлен', 'Проверьте масштаб 100% в окне печати');
  } catch (error) {
    fail(error);
  } finally {
    if (button) {
      button.disabled = false;
      button.classList.remove('is-busy');
      button.removeAttribute('aria-busy');
      button.textContent = 'Печать листа';
    }
  }
}

/* ============================================================ штрихкод */
async function buildBarcode() {
  const input = $('pr_bc_text');
  const out = $('pr_bc_out');
  if (!input || !out) return;
  const text = String(input.value || '').trim();
  if (!text) { out.textContent = 'Введите текст — построим штрихкод.'; return; }
  try {
    const data = await get('/api/labels/code128', { text });
    out.innerHTML = `${data.svg}<small class="muted">Code 128 ${esc(data.mode)} · `
      + `${nfmt(data.symbols)} символов · контрольная сумма проверена</small>`;
  } catch (error) {
    out.innerHTML = `<span class="pr-bad">${esc(error && error.message ? error.message : error)}</span>`;
  }
}

/* ================================================================ bind */
let bound = false;

function bind() {
  if (bound) return;
  bound = true;

  const refresh = $('pr_refresh');
  const reload = () => loadCatalog().then(() => toast('Каталог обновлён', 'Формы взяты из реестра сервера'));
  if (refresh) refresh.addEventListener('click', reload);
  const retry = $('pr_error_retry');
  if (retry) retry.addEventListener('click', reload);

  const formsHost = $('pr_forms');
  if (formsHost) {
    formsHost.addEventListener('click', (event) => {
      const target = event.target;
      if (!target || !target.closest) return;
      const retryButton = target.closest('[data-print-retry]');
      if (retryButton) reload();
      const button = target.closest('[data-print]');
      if (button) printForm(button.dataset.print, button);
    });
  }

  const gen = $('pr_bc_gen');
  if (gen) gen.addEventListener('click', buildBarcode);
  const text = $('pr_bc_text');
  if (text) {
    text.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); buildBarcode(); }
    });
  }

  // Слайсер (18.8): модель → план → нарезка → в очередь.
  const sel = $('pr_sl_model');
  if (sel) sel.addEventListener('change', updateSlicerButtons);
  const uploadBtn = $('pr_sl_upload_btn');
  if (uploadBtn) uploadBtn.addEventListener('click', () => {
    const input = $('pr_sl_file');
    if (input) input.click();
  });
  const fileInput = $('pr_sl_file');
  if (fileInput) fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) uploadSlicerModel(file);
  });
  const modelsRefresh = $('pr_sl_models_refresh');
  if (modelsRefresh) modelsRefresh.addEventListener('click', () => {
    loadSlicerModels().then(() => toast('Список моделей обновлён', 'Взяли библиотеку заново'));
  });
  const planBtn = $('pr_sl_plan');
  if (planBtn) planBtn.addEventListener('click', runSlicerPlan);
  const sliceBtn = $('pr_sl_slice');
  if (sliceBtn) sliceBtn.addEventListener('click', runSlicerSlice);
}

PF.module('print', () => {
  bind();
  loadCatalog({ quiet: true }).catch(fail);
  loadFarmLoopStatus();
  loadSlicerStatus();
  loadSlicerModels();
  loadSlicerSpools();
});

PF.on('data', () => {
  if (!loaded) return;
  if (PF.viewOn('print')) fillSources();
});
PF.on('view', (detail) => {
  if (detail.view !== 'print') return;
  bind();
  if (!loaded) loadCatalog({ quiet: true }).catch(fail);
  loadFarmLoopStatus();
  loadSlicerStatus();
  loadSlicerModels();
  loadSlicerSpools();
});
})();
