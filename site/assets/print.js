/* PrintFlow 17.1 — раздел «Печать» (идея: печатный контур цеха).
   Каталог печатных форм приходит из реестра на сервере (`/api/print/forms`
   вместе с параметрами каждой формы), лист собирает сервер: на бумаге форма
   одинаковая и с телефона, и с ПК. Модуль только рисует каталог, собирает
   параметры и открывает готовый лист в окне печати — своих размеров,
   шаблонов и списков стикеров в браузере нет.

   Грузится лениво (идея 47): раздел открывают перед тиражом, а не в смену. */
(() => {
'use strict';

const { $, $$, esc, nfmt, toast, fail, render, html, raw } = PF.ui;
const { get } = PF.api;

let forms = [];
let groups = [];
let loaded = false;
let loading = false;

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
    render(host, '<div class="empty"><span class="big">▤</span><b>Каталог пуст</b>'
      + '<span>Сервер не отдал ни одной формы — смотрите журнал коннектора.</span></div>');
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
    const markup = await fetchSheet(form.api, params);
    printWindow(markup, form.title);
  } catch (error) {
    fail(error);
  } finally {
    if (button) button.disabled = false;
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
  if (refresh) refresh.addEventListener('click', () => loadCatalog().then(() => toast('Каталог обновлён', 'Формы взяты из реестра сервера')));

  const formsHost = $('pr_forms');
  if (formsHost) {
    formsHost.addEventListener('click', (event) => {
      const target = event.target;
      if (!target || !target.closest) return;
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
}

PF.module('print', () => {
  bind();
  loadCatalog({ quiet: true }).catch(fail);
});

PF.on('data', () => {
  if (!loaded) return;
  if (PF.viewOn('print')) fillSources();
});
PF.on('view', (detail) => {
  if (detail.view !== 'print') return;
  bind();
  if (!loaded) loadCatalog({ quiet: true }).catch(fail);
});
})();
