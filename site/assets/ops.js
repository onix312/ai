/* PrintFlow 2.0 — операционный контур: заказы (канбан/таблица),
   клиенты, ниши и настройка статусов. Все данные — с сервера. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, money, nfmt, hoursText, dateText, dateTimeText,
  todayISO, initials, debounce, toast, fail, openModal, closeModal, confirmDanger } = U;
const { get, post } = PF.api;

let editingOrder = null, editingNiche = null, statusDraft = [];
let fulfillmentDraft = null;
let aftercareItems = [], aftercareCurrent = null;
let filters = { q: '', status: '', niche: '' };
let orderView = 'kanban';

const PRIORITY = { low: 'Низкий', normal: 'Обычный', high: 'Высокий', urgent: 'Срочный' };

const overdue = (o) => o.due && o.due < todayISO() && !PF.isFinal(o);
const dueSoon = (o) => o.due && o.due === todayISO() && !PF.isFinal(o);

function filtered() {
  const q = filters.q.trim().toLowerCase();
  return PF.state.orders.filter((o) => {
    if (filters.status && o.status !== filters.status) return false;
    if (filters.niche && o.niche_id !== filters.niche) return false;
    if (!q) return true;
    return [o.number, o.product, o.customer_name, o.phone, o.file, o.notes]
      .some((v) => String(v || '').toLowerCase().includes(q));
  });
}

/* ============================================================== канбан */
function orderCard(o) {
  const n = PF.niche(o.niche_id);
  const st = PF.status(o.status);
  const econ = o.economics || {};
  const left = Math.max(0, num(o.price) - Math.max(num(o.paid), num(o.prepaid)));
  const cls = ['ocard'];
  if (o.priority === 'urgent') cls.push('urgent');
  if (overdue(o)) cls.push('late');
  return `<article class="${cls.join(' ')}" draggable="true" data-order="${esc(o.id)}">`
    + `<div class="strip" style="background:${esc(st.color)}"></div>`
    + `<div class="num">№${esc(o.number)}${o.qty > 1 ? ` · ${nfmt(o.qty)} шт` : ''}${o.items_count ? ` · ${nfmt(o.items_count)} поз.` : ''}</div>`
    + `<h4>${esc(o.product || 'Без названия')}</h4>`
    + `<div class="who">${esc(o.customer_name || 'Без клиента')}</div>`
    + '<div class="meta">'
    + `<span class="prio ${esc(o.priority || 'normal')}" title="${esc(PRIORITY[o.priority] || '')}"></span>`
    + (n ? `<span class="chip" style="background:${esc(n.color)}22;color:${esc(n.color)}">${esc(n.icon || '◆')} ${esc(n.name)}</span>` : '')
    + (o.hours ? `<span class="muted">${hoursText(o.hours)}</span>` : '')
    + (o.due ? `<span class="due muted">${esc(dateText(o.due))}</span>` : '')
    + `<span class="price">${money(o.price)}</span>`
    + '</div>'
    + (left > 0 && Math.max(num(o.paid), num(o.prepaid)) > 0 ? `<div class="muted" style="font-size:11.4px;margin-top:5px">осталось получить ${money(left)}</div>` : '')
    + (econ.profit != null && num(o.price) ? `<div class="muted" style="font-size:11.4px;margin-top:3px">прибыль ${money(econ.profit)}${econ.profit_per_hour ? ` · ${money(econ.profit_per_hour)}/ч` : ''}</div>` : '')
    + '</article>';
}

function renderKanban(list) {
  const host = $('orders_kanban');
  host.innerHTML = PF.state.statuses.map((st) => {
    const items = list.filter((o) => o.status === st.id);
    const sum = items.reduce((a, o) => a + num(o.price), 0);
    return `<div class="kan-col${items.length ? '' : ' empty-col'}" data-status="${esc(st.id)}">`
      + `<div class="kan-head"><i style="background:${esc(st.color)}"></i><b>${esc(st.name)}</b><span class="n">${items.length}</span></div>`
      + (sum ? `<div class="kan-sum">${money(sum)}</div>` : '')
      + (items.length ? items.map(orderCard).join('') : '')
      + '</div>';
  }).join('');
  bindDrag();
}

let bulkSelected = new Set();
function updateBulkBar() {
  const bar = $('bulk_bar');
  bar.hidden = orderView !== 'table' || !bulkSelected.size;
  $('bulk_count').textContent = `Выбрано ${bulkSelected.size}`;
  if (!bulkSelected.size) $('orders_tbody').querySelectorAll('[data-bulk]').forEach((c) => { c.checked = false; });
}
function renderTable(list) {
  $('orders_tbody').innerHTML = list.length ? list.map((o) => {
    const st = PF.status(o.status), n = PF.niche(o.niche_id), econ = o.economics || {};
    const checked = bulkSelected.has(o.id) ? ' checked' : '';
    return `<tr class="clickable" data-order="${esc(o.id)}">`
      + `<td class="w-check" onclick="event.stopPropagation()"><input type="checkbox" data-bulk="${esc(o.id)}"${checked}></td>`
      + `<td class="strong">№${esc(o.number)}</td>`
      + `<td><b>${esc(o.product)}</b>${o.file ? `<br><small class="muted">${esc(o.file)}</small>` : ''}</td>`
      + `<td>${esc(o.customer_name || '—')}${o.phone ? `<br><small class="muted">${esc(o.phone)}</small>` : ''}</td>`
      + `<td>${n ? `${esc(n.icon || '')} ${esc(n.name)}` : '—'}</td>`
      + `<td><span class="chip" style="background:${esc(st.color)}22;color:${esc(st.color)}">${esc(st.name)}</span></td>`
      + `<td class="right tnum">${o.hours ? nfmt(o.hours, 1) : '—'}</td>`
      + `<td class="right tnum">${o.grams ? nfmt(o.grams) : '—'}</td>`
      + `<td class="right tnum">${money(o.price)}</td>`
      + `<td class="right tnum ${num(econ.profit) >= 0 ? 'pos' : 'neg'}">${money(econ.profit)}</td>`
      + `<td class="${overdue(o) ? 'neg' : ''}">${o.due ? esc(dateText(o.due)) : '—'}</td></tr>`;
  }).join('') : '<tr><td colspan="10"><div class="empty compact"><span>Заказов не найдено.</span></div></td></tr>';
}

function renderOrders() {
  const list = filtered();
  text('orders_sub', `${list.length} из ${PF.state.orders.length} заказов · перетаскивайте карточки между статусами`);
  const tag = $('nav_orders_tag');
  const activeCount = PF.state.orders.filter((o) => !PF.isFinal(o)).length;
  tag.hidden = !activeCount;
  tag.textContent = String(activeCount);
  const late = PF.state.orders.filter(overdue).length;
  tag.className = 'tag' + (late ? ' warn' : '');

  const bs = $('bulk_status');
  if (bs && bs.options.length !== PF.state.statuses.length + 1) {
    bs.innerHTML = '<option value="">Статус…</option>' + PF.state.statuses
      .map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
  }

  $('orders_kanban').hidden = orderView !== 'kanban';
  $('orders_table').hidden = orderView !== 'table';
  if (orderView === 'kanban') renderKanban(list); else renderTable(list);
  updateBulkBar();
}
function text(id, v) { const el = $(id); if (el) el.textContent = v; }

/* =============================================================== drag */
let dragId = null;
function bindDrag() {
  $$('.ocard').forEach((card) => {
    card.addEventListener('dragstart', () => { dragId = card.dataset.order; card.classList.add('dragging'); });
    card.addEventListener('dragend', () => { card.classList.remove('dragging'); dragId = null; });
  });
  $$('.kan-col').forEach((col) => {
    col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('over'); });
    col.addEventListener('dragleave', () => col.classList.remove('over'));
    col.addEventListener('drop', async (e) => {
      e.preventDefault();
      col.classList.remove('over');
      if (!dragId) return;
      const status = col.dataset.status;
      const order = PF.state.orders.find((o) => o.id === dragId);
      if (!order || order.status === status) return;
      const prev = order.status;
      order.status = status;
      renderOrders();
      try {
        const res = await post('/api/order/status', { id: order.id, status });
        Object.assign(order, res.order);
        toast('Статус обновлён', `№${order.number} → ${PF.status(status).name}`);
        PF.refreshCore();
        PF.refreshFinance();
      } catch (err) { order.status = prev; renderOrders(); fail(err); }
    });
  });
}

/* ========================================================= карточка заказа */
function fillSelectors() {
  const statuses = PF.state.statuses.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
  $('of_status').innerHTML = statuses;
  $('orders_filter_status').innerHTML = '<option value="">Все статусы</option>' + statuses;
  const niches = PF.state.niches.map((n) => `<option value="${esc(n.id)}">${esc(n.icon || '◆')} ${esc(n.name)}</option>`).join('');
  $('of_niche_id').innerHTML = '<option value="">Без ниши</option>' + niches;
  $('orders_filter_niche').innerHTML = '<option value="">Все ниши</option>' + niches;
  const cf = $('cf_niche_id');
  if (cf) cf.innerHTML = '<option value="">Без ниши</option>' + niches;
  $('customers_datalist').innerHTML = PF.state.customers
    .map((c) => `<option value="${esc(c.name)}">`).join('');
  const pd = $('products_datalist');
  if (pd) pd.innerHTML = [...(PF.state.nomenclature || []), ...(PF.state.catalog || [])]
    .map((c) => `<option value="${esc(c.name)}">`).join('');
  const nom = $('of_nom_id');
  if (nom) nom.innerHTML = '<option value="">Не выбран — заказ на печать / услугу</option>'
    + (PF.state.nomenclature || []).map((i) => `<option value="${esc(i.id)}">`
      + `${esc(i.name)} · готово ${nfmt(i.free)} шт${num(i.price) ? ' · ' + money(i.price) : ''}</option>`).join('');
  const wh = $('of_warehouse_id');
  if (wh) wh.innerHTML = '<option value="">Автоматически</option>'
    + (PF.state.warehouses || []).map((w) => `<option value="${esc(w.id)}">${esc(w.name)}</option>`).join('');
  $('orders_filter_status').value = filters.status;
  $('orders_filter_niche').value = filters.niche;
}

const OF = ['product', 'status', 'priority', 'niche_id', 'channel', 'qty', 'due',
  'customer_name', 'phone', 'messenger', 'material', 'color', 'grams', 'hours',
  'manual_minutes', 'file', 'price', 'cost', 'prepaid', 'auto_cost', 'quality',
  'quality_note', 'notes', 'colors', 'nom_id', 'warehouse_id'];

/* Многоцветный расход: JSON в базе <-> строка «Белый:40, Чёрный:15» в форме */
function colorsToStr(json) {
  const raw = String(json || '').trim();
  if (!raw) return '';
  try {
    const list = JSON.parse(raw);
    if (!Array.isArray(list)) return raw;   // старый текстовый формат — как есть
    return list.map((c) => `${c.color || c.material || ''}:${num(c.grams)}`).filter(Boolean).join(', ');
  } catch (e) { return raw; }               // не JSON — показываем как есть
}
function colorsToJson(str) {
  const out = [];
  String(str || '').split(',').forEach((part) => {
    const [name, grams] = part.split(':').map((x) => (x || '').trim());
    if (name && num(grams) > 0) out.push({ color: name, material: '', grams: num(grams) });
  });
  return JSON.stringify(out);
}

/* ===== катушки заказа: чем печатаем и с чего спишется пластик ===== */
function spoolRowsFromJson(json) {
  let rows = [];
  try { rows = JSON.parse(json || '[]'); } catch (e) { rows = []; }
  if (!Array.isArray(rows)) rows = [];
  // Пустые строки и катушка без граммов тоже нужны: иначе «+ Катушка»
  // и сохранение выбранного слота теряются до заполнения веса.
  return rows.filter((r) => r && typeof r === 'object');
}
function spoolLabel(s) {
  const slot = s.ams_slot !== '' && s.ams_slot != null ? ` · слот ${s.ams_slot}` : '';
  return `${s.material} ${s.color_name}${slot}`;
}
function renderSpoolRows(json) {
  const host = $('of_spool_rows');
  if (!host) return;
  const rows = spoolRowsFromJson(json);
  const spools = (PF.state.spools || []).filter((s) => !num(s.archived));
  if (!spools.length) {
    host.innerHTML = '<small class="muted">Склад пуст — добавьте катушки в разделе «Склад пластика».</small>';
    return;
  }
  const options = (selected) => '<option value="">— выбрать катушку —</option>'
    + spools.map((s) => `<option value="${esc(s.id)}"${s.id === selected ? ' selected' : ''}>`
      + `${esc(spoolLabel(s))} · ${nfmt(s.remaining_grams)} г</option>`).join('');
  const rowHtml = (r) => '<div class="of-spool-row">'
    + `<select data-spool-sel>${options(r.spool_id || '')}</select>`
    + `<input type="number" min="0" step="any" placeholder="граммы" data-spool-grams value="${r.grams != null ? esc(String(r.grams)) : ''}" title="Сколько граммов спишется с этой катушки">`
    + '<button class="icon-btn sm danger" type="button" data-spool-del title="Убрать катушку">×</button></div>';
  host.innerHTML = (rows.length ? rows : [{}]).map(rowHtml).join('');
}
function collectSpoolRows() {
  const out = [];
  $$('#of_spool_rows .of-spool-row').forEach((row) => {
    const id = (row.querySelector('[data-spool-sel]') || {}).value || '';
    const grams = num((row.querySelector('[data-spool-grams]') || {}).value);
    if (id) out.push({ spool_id: id, grams });
  });
  return JSON.stringify(out);
}

function snapshotSpoolRows() {
  return $$('#of_spool_rows .of-spool-row').map((row) => {
    const gramsEl = row.querySelector('[data-spool-grams]');
    return {
      spool_id: (row.querySelector('[data-spool-sel]') || {}).value || '',
      grams: gramsEl ? gramsEl.value : '',
    };
  });
}

function distributeSpoolGrams(force) {
  const rows = $$('#of_spool_rows .of-spool-row');
  if (!rows.length) return;
  // У мультизаказа «Пластик, г» — уже вся плита, на количество не умножаем.
  const k = orderIsMulti() ? 1 : Math.max(1, num($('of_qty').value, 1));
  const total = num($('of_grams').value) * k;
  if (!total) return;
  const colors = colorsToStr($('of_colors').value).split(',').map((part) => num(part.split(':')[1])).filter((g) => g > 0);
  const colorTotal = colors.reduce((a, b) => a + b, 0);
  rows.forEach((row, index) => {
    const input = row.querySelector('[data-spool-grams]');
    if (!input) return;
    const filled = num(input.value) > 0;
    if (!force && filled) return;
    if (colors[index] && colorTotal) input.value = Math.round(total * colors[index] / colorTotal * 10) / 10;
    else if (rows.length === 1) input.value = Math.round(total * 10) / 10;
    else input.value = Math.round(total / rows.length * 10) / 10;
  });
}

function autoSpoolsFromAms() {
  const live = PF.livePrinter();
  const trays = live && live.ams && live.ams.trays ? live.ams.trays.filter((t) => t.type || t.uuid) : [];
  if (!trays.length) return fail(new Error('AMS не на связи — слоты определить не удалось'));
  const rows = [];
  trays.forEach((tray) => {
    const spool = (PF.state.spools || []).find((s) => !num(s.archived)
      && String(s.printer_id || '') === String(live.id || '')
      && String(s.ams_slot) === String(tray.slot))
      || (PF.state.spools || []).find((s) => !num(s.archived) && tray.uuid && s.tray_uuid === tray.uuid);
    if (spool && !rows.some((r) => r.spool_id === spool.id)) rows.push({ spool_id: spool.id, grams: 0 });
  });
  if (!rows.length) return fail(new Error('Сначала синхронизируйте AMS со складом пластика'));
  renderSpoolRows(JSON.stringify(rows));
  distributeSpoolGrams();
  toast('Катушки подставлены из AMS', `${rows.length} шт · граммы распределены автоматически`);
}

function updateReadyStockHint() {
  const item = (PF.state.nomenclature || []).find((i) => i.id === ($('of_nom_id') || {}).value);
  const hint = $('of_stock_hint');
  if (!hint) return;
  hint.textContent = item
    ? `На складе ${nfmt(item.qty)} шт, свободно ${nfmt(item.free)} шт. При выдаче остаток спишется автоматически.`
    : 'Выберите готовое изделие: название, цена, вес и файл подставятся автоматически.';
  hint.classList.toggle('neg', Boolean(item && num(item.free) < Math.max(1, num($('of_qty').value, 1))));
}

/* ===== состав заказа: разные товары на одной плите (мультизаказ) =====
   Цена заказа = сумма позиций (цены из базы товаров, можно править).
   Вес/время плиты — поля заказа (с принтера/слайсера), вес позиции — из базы. */
function itemNomOptions(selected) {
  return '<option value="">— из базы товаров —</option>'
    + (PF.state.nomenclature || []).map((i) => `<option value="${esc(i.id)}"${i.id === selected ? ' selected' : ''}>`
      + `${esc(i.name)}${num(i.price) ? ' · ' + money(i.price) : ''}${num(i.grams) ? ' · ' + nfmt(i.grams) + ' г/шт' : ''}</option>`).join('');
}
function renderOrderItems(items) {
  const host = $('of_items');
  if (!host) return;
  const rows = (items && items.length ? items : [{}]);
  host.innerHTML = rows.map((r) => `<div class="of-item-row">`
    + `<select data-item-nom>${itemNomOptions(r.nom_id || '')}</select>`
    + `<input data-item-name value="${esc(r.name || '')}" placeholder="Название">`
    + `<input type="number" min="1" step="1" data-item-qty value="${r.qty != null ? esc(String(r.qty)) : '1'}" title="Количество">`
    + `<input type="number" min="0" step="any" data-item-price value="${num(r.price) ? esc(String(r.price)) : ''}" placeholder="цена, ₽">`
    + `<input type="number" min="0" step="any" data-item-grams value="${num(r.grams) ? esc(String(r.grams)) : ''}" placeholder="г/шт из базы" title="Вес штуки — подставляется из базы товаров">`
    + '<button class="icon-btn sm danger" type="button" data-item-del title="Убрать позицию">×</button></div>').join('');
}
function collectOrderItems() {
  const out = [];
  $$('#of_items .of-item-row').forEach((row) => {
    const name = ((row.querySelector('[data-item-name]') || {}).value || '').trim();
    if (!name) return;
    out.push({
      nom_id: (row.querySelector('[data-item-nom]') || {}).value || '',
      name,
      qty: Math.max(1, num((row.querySelector('[data-item-qty]') || {}).value, 1)),
      price: num((row.querySelector('[data-item-price]') || {}).value),
      grams: num((row.querySelector('[data-item-grams]') || {}).value),
    });
  });
  return out;
}
function orderIsMulti() { return collectOrderItems().length > 0; }
function renderItemsEcon(list) {
  const host = $('of_items_econ');
  if (!host) return;
  host.innerHTML = (list && list.length) ? `<div class="verdict">` + list.map((i) =>
    `<div class="tx-row"><span class="tx-ic accent">▣</span>`
    + `<div class="tx-body"><b>${esc(i.name)} ×${nfmt(i.qty)}</b>`
    + `<small>доля ${nfmt(i.share * 100, 0)}% · себестоимость ${money(i.cost)} · прибыль ${money(i.profit)}</small></div>`
    + `<span class="amt">${money(i.price)}</span></div>`).join('')
    + '</div>' : '';
}
function updateOrderItemsSummary() {
  const items = collectOrderItems();
  if (!items.length) return;
  const total = items.reduce((a, i) => a + i.price * i.qty, 0);
  const units = items.reduce((a, i) => a + i.qty, 0);
  const product = ($('of_product').value || '').trim();
  $('of_price').value = String(Math.round(total * 100) / 100);
  $('of_qty').value = String(units);
  if (!product) {
    $('of_product').value = items.map((i) => `${i.name} ×${i.qty}`).join(', ');
  }
  distributeSpoolGrams();
  updateEconDebounced();
}

async function fillFromFileEstimate() {
  const file = $('of_file').value.trim();
  if (!file) return;
  try {
    const res = await get('/api/estimate', { file });
    const est = res.estimate || {};
    // Многоплитный проект: сумма по всем плитам, а не первая плита.
    const grams = num(est.total_grams) || num(est.grams);
    const minutes = num(est.total_minutes) || num(est.minutes);
    if (grams) $('of_grams').value = grams;
    if (minutes) $('of_hours').value = Math.round(minutes / 60 * 100) / 100;
    if (est.material) $('of_material').value = est.material;
    if (est.color) $('of_color').value = est.color;
    distributeSpoolGrams();
    updateEconDebounced();
    toast('Данные печати подставлены', `${grams ? nfmt(grams) + ' г' : ''}${minutes ? ' · ' + U.minutesText(minutes) : ''}`);
  } catch (e) { /* Файл может ещё не быть известен коннектору — ручной ввод остаётся доступен. */ }
}

function amsHexToName(hex) {
  hex = String(hex || '').trim().replace('#', '');
  if (hex.length < 6) return '';
  const r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  if (mx - mn < 30) {
    if (mx < 60) return 'Чёрный';
    if (mx > 200) return 'Белый';
    return 'Серый';
  }
  if (r >= g && r >= b) return g > 90 ? 'Оранжевый' : 'Красный';
  if (g >= r && g >= b) return 'Зелёный';
  return 'Синий';
}
function currentAmsTray() {
  try {
    const live = PF.livePrinter();
    if (!live || !live.ams || !live.ams.trays.length) return null;
    return live.ams.trays.find((t) => t.active) || live.ams.trays[0] || null;
  } catch (e) { return null; }
}
function fillFromAms() {
  const tray = currentAmsTray();
  if (!tray) return fail(new Error('AMS не на связи — нет данных о слотах'));
  if (tray.type) $('of_material').value = tray.type;
  const name = amsHexToName(tray.color) || tray.color || '';
  if (name) $('of_color').value = name;
  toast('Взято из AMS', `${tray.type || ''} ${name}`.trim() || tray.label || 'слот');
  updateEconDebounced();
}

/**
 * Обновить граммы заказа: подтянуть вес плиты и время печати из активного
 * задания принтера, из файла .3mf/.gcode либо из базы товаров. Значение,
 * которое попадает в поле «Пластик, г», сразу распределяется по катушкам
 * (то есть по столбцу «граммы» рядом с каждой выбранной катушкой — это и
 * есть граммы к списанию со склада). Пользователь дёргает эту функцию
 * кнопкой «⟳ Обновить граммы» рядом с полем, но она же вызывается
 * автоматически при открытии заказа, если веса ещё нет.
 *
 * Возвращает { grams, hours, source } или null, если ни один источник не
 * дал данных. При manual=true (нажатие кнопки) — форсим перезапись даже
 * если поле уже заполнено вручную.
 */
async function refreshOrderGrams(manual) {
  const gramsEl = $('of_grams');
  const hoursEl = $('of_hours');
  if (!gramsEl) return null;

  let grams = 0, hours = 0, minutes = 0, source = '';
  const materialEl = $('of_material'), colorEl = $('of_color'), fileEl = $('of_file');

  // 1) Локальная копия / история.
  const live = PF.livePrinter();
  let file = fileEl ? fileEl.value.trim() : '';
  if (!file && live && live.printer && live.printer.task) {
    file = String(live.printer.task).split('/').pop();
    if (fileEl && file) fileEl.value = file;
  }
  if (file) {
    try {
      const res = await get('/api/estimate', { file });
      const est = (res && res.estimate) || {};
      const g = num(est.total_grams) || num(est.grams);
      const m = num(est.total_minutes) || num(est.minutes);
      if (g) { grams = g; source = `файл ${file}`; }
      if (m) minutes = m;
      if (est.material && materialEl && (!materialEl.value || manual)) materialEl.value = est.material;
      if (est.color && colorEl && (!colorEl.value || manual)) colorEl.value = est.color;
    } catch (e) { /* нет локальной копии — скачаем с принтера */ }
  }

  // 2) Скачать 3MF/G-code с SD принтера в uploads и разобрать вес плиты.
  if (!grams && (file || (live && live.id))) {
    try {
      const res = await post('/api/estimate/pull', {
        file, printer_id: (live && live.id) || '',
      });
      const g = num(res.grams) || num((res.estimate || {}).total_grams) || num((res.estimate || {}).grams);
      const m = num(res.minutes) || num((res.estimate || {}).total_minutes) || num((res.estimate || {}).minutes);
      if (res.file && fileEl) fileEl.value = res.file;
      if (g) { grams = g; source = res.source === 'printer' ? `скачан с принтера · ${res.file || file}` : `uploads · ${res.file || file}`; }
      if (m) minutes = m;
      if (res.material && materialEl && (!materialEl.value || manual)) materialEl.value = res.material;
      if (res.color && colorEl && (!colorEl.value || manual)) colorEl.value = res.color;
    } catch (e) {
      if (manual && e && e.message) { /* покажем в конце, если ничего не нашлось */ }
    }
  }

  // 3) Факт принтера — только после FINISH (во время печати print_weight частичный).
  if (!grams) {
    try {
      const p = live && live.printer;
      const w = p ? num(p.weight) : 0;
      if (w > 0 && String(p.state || '') === 'FINISH') {
        grams = w;
        source = `принтер ${live.name || ''}`.trim();
      }
    } catch (e) { /* принтер не на связи — не критично */ }
  }

  // 4) База товаров: если заказ на готовое изделие, берём норматив «грамм на штуку».
  if (!grams) {
    const nomId = ($('of_nom_id') || {}).value || '';
    const item = nomId && (PF.state.nomenclature || []).find((i) => i.id === nomId);
    if (item && num(item.grams)) {
      grams = num(item.grams);
      if (num(item.hours)) hours = num(item.hours);
      source = `база товаров · ${item.name || ''}`.trim();
    }
  }

  if (!grams) {
    if (manual) fail(new Error('Не нашёл граммы: скачайте файл с принтера, выберите 3MF/G-code с компьютера или укажите товар из базы'));
    return null;
  }

  // Форсим перезапись только когда пользователь нажал кнопку; при
  // автоподстановке уважаем ручной ввод и заполняем только пустые поля.
  // Часы — побочный бонус той же оценки. Без граммов их не трогаем:
  // кнопка «Обновить граммы» иначе выглядела так, будто вместо веса
  // подставилось время печати (elapsed+remaining с принтера).
  const shouldWriteGrams = grams > 0 && (manual || !num(gramsEl.value));
  const shouldWriteHours = shouldWriteGrams && (hours > 0 || minutes > 0)
    && hoursEl && (manual || !num(hoursEl.value));
  if (shouldWriteGrams) gramsEl.value = Math.round(grams * 10) / 10;
  if (shouldWriteHours) {
    const h = hours > 0 ? hours : minutes / 60;
    hoursEl.value = Math.round(h * 100) / 100;
  }

  // Раздать граммы по строкам катушек — это и есть «граммы на вычет из катушки».
  // При ручном обновлении перезаполняем все строки, при авто — только пустые.
  distributeSpoolGrams(Boolean(manual));
  updateEconDebounced();

  if (manual) {
    const parts = [];
    if (shouldWriteGrams) parts.push(`${nfmt(gramsEl.value)} г`);
    if (shouldWriteHours) parts.push(U.hoursText(num(hoursEl.value)));
    if (source) parts.push(source);
    toast('Граммы обновлены', parts.join(' · ') || 'из источника печати');
  }
  return { grams, hours: hours || minutes / 60, source };
}

async function openOrder(id, intakeDraft, intakeMeta) {
  editingOrder = id || null;
  fillSelectors();
  let order = id ? PF.state.orders.find((o) => o.id === id) : null;
  if (id) { try { order = await get('/api/order', { id }); } catch (e) { /* используем локальную копию */ } }
  const blank = {
    product: '', status: PF.state.statuses[0] ? PF.state.statuses[0].id : 'new', priority: 'normal',
    niche_id: '', channel: 'direct', qty: 1, due: '', customer_name: '', phone: '',
    messenger: '', material: 'PLA', color: '', grams: '', hours: '', manual_minutes: '',
    file: '', price: '', cost: '', prepaid: '', auto_cost: 1, quality: 'pending',
    quality_note: '', notes: '', nom_id: '', warehouse_id: '', reserved: 0,
  };
  // Умные значения по умолчанию: канал, ниша и материал — как в последнем заказе,
  // а не с нуля. Экономит пару полей на каждом похожем заказе.
  if (!id && PF.state.orders.length) {
    const last = PF.state.orders[0];
    if (last.channel) blank.channel = last.channel;
    if (last.niche_id) blank.niche_id = last.niche_id;
    if (last.material) blank.material = last.material;
  }
  // AMS: если создаём новый заказ и принтер на связи — подставляем материал и цвет из активного слота AMS
  if (!id && !blank.color) {
    const tray = currentAmsTray();
    if (tray) {
      if (tray.type && !blank.material) blank.material = tray.type;
      else if (tray.type) blank.material = tray.type;
      const amsName = amsHexToName(tray.color);
      if (amsName) blank.color = amsName;
      else if (tray.color) blank.color = tray.color;
    }
  }
  const data = Object.assign({}, blank, order || {}, intakeDraft || {});
  // Старые версии сохраняли подпись канала вместо его id. Нормализуем при
  // открытии, чтобы комиссии и аналитика снова находили справочник.
  const channelAliases = {
    'Полка магазина': 'shop', 'Витрина': 'shop', 'Telegram': 'telegram',
    'Авито': 'avito', 'B2B': 'b2b', 'Рекомендация': 'direct', 'Другое': 'direct',
  };
  data.channel = channelAliases[data.channel] || data.channel || 'direct';
  OF.forEach((k) => {
    const el = $('of_' + k);
    if (!el) return;
    if (k === 'colors') { if (el) el.value = colorsToStr(data.colors); return; }
    el.value = data[k] == null ? '' : String(data[k]);
  });
  renderSpoolRows(data.spools);
  distributeSpoolGrams();
  renderOrderItems(data.items || []);
  renderItemsEcon(data.items_economics || []);
  $('of_reserved').checked = Boolean(num(data.reserved));
  updateReadyStockHint();
  $('of_auto_cost').value = String(num(data.auto_cost, 1) ? 1 : 0);
  // в поле показываем фактически полученные деньги: платежи пишутся в paid,
  // prepaid остался от старых заказов
  const paidNow = Math.max(num(data.paid), num(data.prepaid));
  $('of_prepaid').value = paidNow ? String(paidNow) : '';
  $('order_modal_title').textContent = id ? `Заказ №${data.number}` : 'Новый заказ';
  $('order_modal_sub').textContent = id
    ? `Создан ${dateTimeText(data.created_at)}${data.closed_at ? ' · закрыт ' + dateTimeText(data.closed_at) : ''}`
    : 'Клиент создаётся автоматически по имени и телефону.';
  const intakeHint = $('of_intake_hint');
  intakeHint.hidden = !intakeMeta;
  if (intakeMeta) {
    const matches = intakeMeta.matches || {};
    const found = [
      matches.customer ? `клиент: ${matches.customer.name}` : '',
      matches.product ? `товар: ${matches.product.name}` : '',
      matches.previous_order ? `прошлый заказ №${matches.previous_order.number}` : '',
    ].filter(Boolean).join(' · ');
    const warnings = (intakeMeta.warnings || []).map((w) => `⚠ ${esc(w)}`).join('<br>');
    intakeHint.className = `verdict ${num(intakeMeta.confidence) >= 80 ? 'ok' : 'warn'}`;
    intakeHint.innerHTML = `<b>Заполнено из текста · уверенность ${nfmt(intakeMeta.confidence)}%</b>`
      + (found ? `<br>${esc(found)}` : '') + (warnings ? `<br>${warnings}` : '')
      + '<br><small>Проверьте поля и нажмите «Сохранить» — до этого база не меняется.</small>';
  }
  $('order_delete').hidden = !id;
  $('order_queue').hidden = !id;
  $('order_queue').disabled = false;
  $('order_queue').textContent = 'Сохранить и подготовить';
  $('order_save_prepare').hidden = Boolean(id);
  $('order_fulfill').hidden = !id || data.status !== 'ready';
  $('order_duplicate').hidden = !id;
  $('order_b2b').hidden = !id;
  $('of_production_wrap').hidden = !id;
  $('of_completion_wrap').hidden = !id;
  $('of_completion_message_wrap').hidden = true;
  $('order_accept_result').hidden = false;
  $('order_accept_result').disabled = true;

  const jobs = (order && order.jobs) || [];
  $('of_jobs_wrap').hidden = !jobs.length;
  if (jobs.length) {
    $('of_jobs').innerHTML = jobs.map((j) => `<div class="tx-row">`
      + `<span class="tx-ic ${j.state === 'done' ? 'income' : 'expense'}">${j.state === 'done' ? '✓' : '•'}</span>`
      + `<div class="tx-body"><b>${esc(j.name || j.file || 'Печать')}</b>`
      + `<small>${esc(dateTimeText(j.finished_at || j.started_at))} · ${nfmt(j.grams)} г · ${U.minutesText(j.duration_min)}</small></div>`
      + `<span class="amt">${money(j.cost)}</span></div>`).join('');
  }
  renderOrderPhotos((order && order.photos) || []);
  renderOrderDefects((order && order.defects) || []);
  renderQcChecklist((order && order.qc_done) || '');
  updateEcon();
  openModal('order_modal');
  if (id) {
    loadOrderReadiness(id);
    loadOrderCompletion(id);
  }
  // Автоподстановка граммов: если поле пустое, попробуем взять вес плиты
  // с принтера / файла / базы товаров. Ручной ввод не перезаписываем.
  if (!num(($('of_grams') || {}).value)) {
    refreshOrderGrams(false).catch(() => { /* тихо: пользователь всегда может нажать кнопку */ });
  }
}

let completionRequest = 0;
async function loadOrderCompletion(orderId) {
  if (!orderId) return null;
  const request = ++completionRequest;
  const box = $('of_completion_summary');
  box.className = 'verdict';
  box.textContent = 'Сверяем задания, план, факт и чек-лист…';
  try {
    const result = await get('/api/order/completion', { id: orderId });
    if (request !== completionRequest || editingOrder !== orderId) return result;
    const plan = result.plan || {}, actual = result.actual || {}, jobs = result.jobs || {};
    const gramsDiff = (actual.grams_difference || {}).percent;
    const hoursDiff = (actual.hours_difference || {}).percent;
    const lines = [
      `<b>${result.accepted ? 'Результат принят · заказ готов' : result.can_accept ? 'Можно принять результат' : 'Приёмка пока недоступна'}</b>`,
      `План: ${nfmt(plan.grams)} г · ${nfmt(plan.hours)} ч · ${money(plan.cost)}`,
      `Факт: ${nfmt(actual.grams)} г${gramsDiff == null ? '' : ` (${gramsDiff > 0 ? '+' : ''}${nfmt(gramsDiff)}%)`}`
        + ` · ${nfmt(actual.hours)} ч${hoursDiff == null ? '' : ` (${hoursDiff > 0 ? '+' : ''}${nfmt(hoursDiff)}%)`}`
        + ` · ${money(actual.cost)}`,
      `Печати: успешно ${nfmt(jobs.successful)} · неудачно ${nfmt(jobs.failed)} · активно ${nfmt(jobs.active)}`,
      ...(result.blocks || []).map((item) => `✕ ${esc(item.text)}`),
      ...(result.warns || []).map((item) => `⚠ ${esc(item.text)}`),
    ];
    box.className = `verdict ${result.accepted ? 'ok' : result.can_accept ? ((result.warns || []).length ? 'warn' : 'ok') : 'bad'}`;
    box.innerHTML = lines.join('<br>');
    $('order_accept_result').hidden = result.accepted;
    $('order_accept_result').disabled = !result.can_accept;
    $('of_completion_message').value = result.message || '';
    $('of_completion_message_wrap').hidden = !result.accepted;
    return result;
  } catch (e) {
    if (request === completionRequest) {
      box.className = 'verdict bad';
      box.textContent = e.message || String(e);
      $('order_accept_result').disabled = true;
    }
    return null;
  }
}

async function openOrderFulfillment(orderId) {
  if (!orderId) return;
  try {
    const result = await get('/api/order/fulfillment', { id: orderId });
    if (!result.can_fulfill) {
      throw new Error((result.blocks || []).map((item) => item.text).join('; ') || 'Заказ нельзя выдать');
    }
    if (result.fulfilled) {
      toast('Заказ уже выдан');
      return;
    }
    fulfillmentDraft = result;
    const payment = result.payment || {}, eco = result.economics || {};
    const due = num(payment.due);
    $('fulfillment_summary').className = `verdict ${due ? 'warn' : 'ok'}`;
    $('fulfillment_summary').innerHTML = `<b>Заказ №${esc(result.number)} готов к выдаче</b><br>`
      + `Цена ${money(payment.price)} · оплачено ${money(payment.paid)} · остаток ${money(due)}<br>`
      + `Фактическая прибыль ${money(eco.profit)} · маржа ${nfmt(eco.margin)}%`
      + (due ? '<br>Выберите: записать полученную оплату или оставить долг.' : '<br>Заказ уже полностью оплачен.');
    $('hf_payment_action_wrap').hidden = !due;
    $('hf_payment_action').value = due ? '' : 'none';
    $('hf_account').innerHTML = (payment.accounts || []).map((account) =>
      `<option value="${esc(account.id)}">${esc(account.name)}</option>`).join('');
    $('hf_account').value = payment.default_account_id || '';
    $('hf_method').value = 'cash';
    $('hf_handoff_confirmed').checked = false;
    updateFulfillmentPaymentFields();
    openModal('fulfillment_modal');
  } catch (e) { fail(e); }
}

function updateFulfillmentPaymentFields() {
  const received = $('hf_payment_action').value === 'received';
  $('hf_account_wrap').hidden = !received;
  $('hf_method_wrap').hidden = !received;
}

async function confirmOrderFulfillment() {
  if (!fulfillmentDraft) return;
  if (!$('hf_handoff_confirmed').checked) {
    return fail(new Error('Подтвердите, что изделие передано клиенту'));
  }
  const button = $('fulfillment_confirm');
  button.disabled = true;
  try {
    const result = await post('/api/order/fulfill', {
      id: fulfillmentDraft.order_id,
      handoff_confirmed: true,
      payment_action: $('hf_payment_action').value,
      account_id: $('hf_account').value || '',
      payment_method: $('hf_method').value || '',
    });
    closeModal('fulfillment_modal');
    closeModal('order_modal');
    let copied = false;
    if (result.message && navigator.clipboard) {
      try { await navigator.clipboard.writeText(result.message); copied = true; } catch (e) { /* сообщение остаётся в истории */ }
    }
    const moneyResult = num(result.collected) > 0
      ? `получено ${money(result.collected)}`
      : num(result.debt) > 0 ? `оставлен долг ${money(result.debt)}` : 'оплачен ранее';
    toast('Заказ выдан', `${moneyResult}${copied ? ' · текст клиенту скопирован' : ''}`);
    fulfillmentDraft = null;
    await PF.refreshCore();
    PF.refreshFinance();
  } catch (e) { fail(e); }
  finally { button.disabled = false; }
}

let readinessRequest = 0;
async function loadOrderReadiness(orderId, printerId, spoolId) {
  if (!orderId) return null;
  const request = ++readinessRequest;
  const box = $('of_production_ready');
  box.className = 'verdict';
  box.textContent = 'Проверяем файл, принтер и пластик…';
  try {
    const ready = await get('/api/order/readiness', {
      id: orderId, printer_id: printerId || '', spool_id: spoolId || '',
    });
    if (request !== readinessRequest || editingOrder !== orderId) return ready;
    const req = ready.requirements || {};
    const selectedPrinter = (ready.selected_printer || {}).id || '';
    const selectedSpool = (ready.selected_spool || {}).id || '';
    $('of_prepare_printer').innerHTML = (ready.printers || []).map((printer) =>
      `<option value="${esc(printer.id)}">${esc(printer.name)}${printer.connected ? ' · на связи' : ''}</option>`).join('')
      || '<option value="">Нет принтеров</option>';
    $('of_prepare_printer').value = selectedPrinter;
    $('of_prepare_spool').innerHTML = (ready.spools || []).map((spool) =>
      `<option value="${esc(spool.id)}">${esc(spool.material)} ${esc(spool.color)}`
      + ` · свободно ${nfmt(spool.available_grams)} г${spool.in_ams ? ` · AMS ${esc(spool.ams_slot)}` : ''}</option>`).join('')
      || '<option value="">Подходящих катушек нет</option>';
    $('of_prepare_spool').value = selectedSpool;
    const lines = [
      `<b>${ready.already_queued ? 'Уже подготовлено' : ready.ok ? 'Можно ставить в очередь' : 'Нужно исправить'}</b>`,
      `Требуется: ${nfmt(req.grams)} г · ${U.minutesText(req.minutes)} · ${nfmt(req.qty)} шт`,
      ...(ready.blocks || []).map((item) => `✕ ${esc(item.text)}`),
      ...(ready.warns || []).map((item) => `⚠ ${esc(item.text)}`),
      ...(ready.infos || []).map((item) => `• ${esc(item.text)}`),
    ];
    box.className = `verdict ${ready.already_queued || ready.ok ? ((ready.warns || []).length ? 'warn' : 'ok') : 'bad'}`;
    box.innerHTML = lines.join('<br>');
    $('of_production_choices').hidden = ready.already_queued;
    $('order_queue').disabled = ready.already_queued;
    $('order_queue').textContent = ready.already_queued ? 'Уже в очереди' : 'Сохранить и подготовить';
    return ready;
  } catch (e) {
    if (request === readinessRequest) {
      box.className = 'verdict bad';
      box.textContent = e.message || String(e);
      $('order_queue').disabled = true;
    }
    return null;
  }
}

async function updateEcon() {
  const grams = num($('of_grams').value), hours = num($('of_hours').value);
  const price = num($('of_price').value), prepaid = num($('of_prepaid').value);
  const manual = num($('of_manual_minutes').value), qty = Math.max(1, num($('of_qty').value, 1));
  // У простого заказа нормы grams/hours задаются на единицу и умножаются на
  // qty; price всегда хранит сумму всего заказа (как платежи и долг).
  const multi = orderIsMulti();
  const k = multi ? 1 : qty;
  let cost = num($('of_cost').value);
  let auto = null;
  if (!cost && (grams || hours)) {
    try {
      auto = await post('/api/calc/cost', { grams: grams * k, hours: hours * k, manual_minutes: manual });
      cost = num(auto.total);
    } catch (e) { /* офлайн — оставим 0 */ }
  }
  // Цена одинакова во всех режимах: это сумма заказа, не цена за штуку.
  const total = price;
  const profit = total - cost;
  const perHour = hours * k ? profit / (hours * k) : 0;
  const left = Math.max(0, total - prepaid);
  const target = num(PF.state.settings.target_profit_per_hour, 250);
  const kind = !hours ? '' : perHour >= target ? 'ok' : perHour >= target * 0.4 ? 'warn' : 'bad';
  $('of_econ').innerHTML = `<div class="verdict ${kind}">`
    + `<b>Себестоимость:</b> ${money(cost)}${auto ? ' (расчёт по вашим тарифам)' : ''} · `
    + `<b>Прибыль:</b> ${money(profit)} · `
    + `<b>За час печати:</b> ${hours ? money(perHour) : '—'}`
    + (left ? ` · осталось получить ${money(left)}` : '')
    + (kind === 'bad' ? '<br>Ниже нормы: поднимите цену, уменьшите время печати или откажитесь.' : '')
    + (kind === 'ok' ? '<br>Заказ в норме по прибыли за час принтера.' : '')
    + '</div>';
}
const updateEconDebounced = debounce(updateEcon, 350);

async function saveOrder(prepareAfter) {
  const payload = { id: editingOrder || '' };
  OF.forEach((k) => { const el = $('of_' + k); if (el) payload[k] = el.value; });
  if (payload.colors !== undefined) payload.colors = colorsToJson(payload.colors);
  distributeSpoolGrams();
  payload.spools = collectSpoolRows();
  payload.reserved = $('of_reserved').checked ? 1 : 0;
  payload.items = collectOrderItems();
  if (!payload.product.trim()) {
    if (payload.items.length) payload.product = payload.items.map((i) => `${i.name} ×${i.qty}`).join(', ');
    else return fail(new Error('Укажите изделие или работу'));
  }
  if (payload.reserved && !payload.nom_id) return fail(new Error('Для резерва выберите готовый товар из базы'));
  ['qty', 'grams', 'hours', 'manual_minutes', 'price', 'cost', 'prepaid'].forEach((k) => { payload[k] = num(payload[k]); });
  // поле «Оплачено» ведёт основной счётчик оплаты, prepaid оставляем для совместимости
  payload.paid = payload.prepaid;
  payload.auto_cost = +$('of_auto_cost').value;
  const wasEditing = Boolean(editingOrder);
  try {
    const res = await post('/api/order/save', payload);
    editingOrder = res.order.id;
    if (prepareAfter) {
      try {
        const prepared = await post('/api/order/prepare', {
          id: res.order.id,
          printer_id: $('of_prepare_printer').value || '',
          spool_id: $('of_prepare_spool').value || '',
        });
        closeModal('order_modal');
        toast(prepared.already_queued ? 'Заказ уже в очереди' : 'Производство подготовлено',
          `№${res.order.number} · физический запуск не выполнялся`);
      } catch (prepareError) {
        toast(wasEditing ? 'Заказ обновлён' : 'Заказ создан',
          `№${res.order.number} · проверьте готовность`, 'warn');
        await PF.refreshCore();
        await openOrder(res.order.id);
        fail(prepareError);
        return res;
      }
    } else {
      closeModal('order_modal');
      toast(wasEditing ? 'Заказ обновлён' : 'Заказ создан',
        `№${res.order.number} · ${res.order.product}`);
    }
    await PF.refreshCore();
    PF.refreshFinance();
    return res;
  } catch (e) { fail(e); return null; }
}

/* ====================================================== фото к заказу */
function renderOrderPhotos(photos) {
  const wrap = $('of_photos_wrap');
  if (!wrap) return;
  wrap.hidden = !photos.length;
  if (photos.length) {
    $('of_photos').innerHTML = photos.map((ph) =>
      `<div class="ophoto"><img src="/api/order/photo.jpg?photo_id=${esc(ph.id)}" alt="">`
      + `<small>${esc(ph.note || '')}</small>`
      + `<button class="icon-btn sm" type="button" data-photo-del="${esc(ph.id)}">×</button></div>`).join('');
  }
}
async function addOrderPhoto(orderId, dataUrl, kind, note) {
  try {
    await post('/api/order/photo', { order_id: orderId, data, kind, note });
    toast('Фото добавлено');
  } catch (e) { fail(e); }
}
function bindOrderPhotos() {
  const wrap = $('of_photos_wrap');
  if (!wrap) return;
  const cam = $('of_photo_camera');
  if (cam) cam.addEventListener('click', async () => {
    if (!editingOrder) return;
    const pr = PF.livePrinter();
    if (!pr || !pr.camera || !pr.camera.available) return fail(new Error('Кадр камеры недоступен'));
    try {
      const res = await fetch('/api/printer/camera.jpg?printer_id=' + encodeURIComponent(pr.id));
      const blob = await res.blob();
      const reader = new FileReader();
      reader.onload = async () => { await addOrderPhoto(editingOrder, reader.result, 'camera', 'кадр с камеры'); refreshOrderDetail(); };
      reader.readAsDataURL(blob);
    } catch (e) { fail(e); }
  });
  const upBtn = $('of_photo_upload_btn');
  if (upBtn) upBtn.addEventListener('click', () => { const f = $('of_photo_file'); if (f) f.click(); });
  const up = $('of_photo_file');
  if (up) up.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file || !editingOrder) return;
    const reader = new FileReader();
    reader.onload = async () => { await addOrderPhoto(editingOrder, reader.result, 'upload', 'фото'); refreshOrderDetail(); };
    reader.readAsDataURL(file);
    up.value = '';
  });
  const list = $('of_photos');
  if (list) list.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-photo-del]');
    if (!btn) return;
    await post('/api/order/photo/delete', { id: btn.dataset.photoDel });
    refreshOrderDetail();
  });
}
async function refreshOrderDetail() {
  if (!editingOrder) return;
  try {
    const order = await get('/api/order', { id: editingOrder });
    renderOrderPhotos(order.photos || []);
    renderOrderDefects(order.defects || []);
  } catch (e) { /* не критично */ }
}

/* ====================================================== журнал брака */
const DEFECT_REASONS = {
  detached: 'Деталь отклеилась', clog: 'Засор сопла', shift: 'Смещение слоёв',
  runout: 'Закончился пластик', warp: 'Деформация',
  quality: 'Не прошло контроль качества', support: 'Ошибка поддержек',
  wrong_material: 'Неверный материал', power: 'Сбой питания/связи', other: 'Другое',
};
let defectPreview = null, defectRequestId = '';
function renderOrderDefects(defects) {
  const wrap = $('of_defects_wrap');
  if (!wrap) return;
  wrap.hidden = !defects.length;
  if (defects.length) {
    $('of_defects').innerHTML = defects.map((d) => `<div class="tx-row">`
      + `<span class="tx-ic expense">✕</span>`
      + `<div class="tx-body"><b>${esc(DEFECT_REASONS[d.reason] || d.reason || 'Брак')}</b>`
      + `<small>${esc(dateTimeText(d.at))}${d.phase ? ' · ' + esc(d.phase) : ''}${d.code ? ' · ' + esc(d.code) : ''}${d.note ? ' · ' + esc(d.note) : ''}${d.reprint_job_id ? ' · повтор подготовлен' : ''}</small></div>`
      + `<span class="amt neg">${d.loss ? money(d.loss) : (d.grams ? nfmt(d.grams) + ' г' : '')}</span></div>`).join('');
  }
}
async function refreshDefectPreview(resetGrams) {
  const jobId = $('df_job').value;
  const target = $('df_summary').querySelector('span:last-child');
  if (!jobId) {
    defectPreview = null;
    $('df_loss').value = '';
    target.textContent = 'Выберите задание — PrintFlow подставит фактические потери.';
    return;
  }
  try {
    const params = { id: jobId, reason: $('defect_reason').value };
    if (!resetGrams && num($('df_grams').value) > 0) params.grams = num($('df_grams').value);
    defectPreview = await get('/api/defect/recovery', params);
    if (resetGrams || !num($('df_grams').value)) $('df_grams').value = num(defectPreview.loss.grams);
    $('df_loss').value = num(defectPreview.loss.total).toFixed(2);
    const loss = defectPreview.loss || {};
    const risk = defectPreview.repeat_risk
      ? `<br><b>Причина уже повторялась.</b> Перед повтором подтвердите исправление.` : '';
    const blocked = (defectPreview.blockers || []).length
      ? `<br>⚠ ${esc(defectPreview.blockers.join('; '))}` : '';
    target.innerHTML = `Факт: <b>${nfmt(loss.grams)} г</b> · ${nfmt(loss.minutes)} мин · `
      + `<b>${money(loss.total)}</b>. ${esc(defectPreview.recommendation || '')}${risk}${blocked}`;
    $('df_risk_wrap').hidden = !defectPreview.repeat_risk;
    $('df_risk_confirmed').checked = false;
    $('df_reprint').disabled = !defectPreview.can_reprint;
    if (!defectPreview.can_reprint) $('df_reprint').checked = false;
  } catch (e) {
    defectPreview = null;
    target.textContent = e.message;
    $('df_loss').value = '';
  }
}
function openDefect(orderId) {
  const all = PF.state.jobs.history || [];
  const jobs = all.filter((j) => !orderId || j.order_id === orderId)
    .sort((a, b) => (a.state === 'failed' ? -1 : 1) - (b.state === 'failed' ? -1 : 1));
  $('df_job').innerHTML = '<option value="">Выберите завершённую печать</option>'
    + jobs.slice(0, 30).map((j) => `<option value="${esc(j.id)}">`
      + `${j.state === 'failed' ? '✕ ' : '✓ '}${esc(j.name || j.file || 'печать')} · ${esc(dateText(j.finished_at))}</option>`).join('');
  $('defect_reason').value = 'detached';
  $('df_phase').value = 'middle';
  $('df_code').value = '';
  $('df_grams').value = '';
  $('df_loss').value = '';
  $('defect_note').value = '';
  $('df_confirmed').checked = false;
  $('df_reprint').checked = false;
  $('df_reprint').disabled = true;
  $('df_risk_wrap').hidden = true;
  $('df_risk_confirmed').checked = false;
  defectPreview = null;
  defectRequestId = (window.crypto && window.crypto.randomUUID)
    ? window.crypto.randomUUID()
    : `defect-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  $('df_job').dataset.orderId = orderId || '';
  openModal('defect_modal');
  if (jobs.length) {
    $('df_job').value = jobs[0].id;
    refreshDefectPreview(true);
  }
}
async function saveDefect() {
  const job_id = $('df_job').value;
  if (!job_id) return fail(new Error('Выберите задание печати'));
  if (!$('df_confirmed').checked) return fail(new Error('Подтвердите причину брака'));
  if ($('df_reprint').checked && defectPreview && defectPreview.repeat_risk
      && !$('df_risk_confirmed').checked) {
    return fail(new Error('Подтвердите, что причина повторного брака устранена'));
  }
  const button = $('defect_save');
  button.disabled = true;
  try {
    const res = await post('/api/defect/recover', {
      job_id,
      defect_confirmed: true,
      reason: $('defect_reason').value,
      phase: $('df_phase').value,
      code: $('df_code').value.trim(),
      grams: num($('df_grams').value),
      note: $('defect_note').value.trim(),
      reprint_confirmed: $('df_reprint').checked,
      repeat_risk_confirmed: $('df_risk_confirmed').checked,
      request_id: defectRequestId,
    });
    closeModal('defect_modal');
    const repeat = res.repeat_job
      ? (res.repeat_job.already_prepared ? 'Повтор уже был в очереди.' : 'Повтор подготовлен без автостарта.')
      : 'Повтор не создавался.';
    toast('Разбор брака сохранён', `${money(res.loss.total)} · ${repeat}`);
    refreshOrderDetail();
    PF.refreshCore();
  } catch (e) {
    fail(e);
  } finally {
    button.disabled = false;
  }
}

/* ==================================================== чек-лист качества */
function renderQcChecklist(saved) {
  const host = $('of_qc');
  if (!host) return;
  const steps = (PF.state.settings.qc_checklist || []).map(String);
  if (!steps.length) { host.innerHTML = ''; return; }
  let done = {};
  try { done = JSON.parse(saved || '{}'); } catch (e) { done = {}; }
  host.innerHTML = steps.map((step, i) => `<label class="check qc-check">`
    + `<input type="checkbox" data-qc="${i}"${done[i] ? ' checked' : ''}>${esc(step)}</label>`).join('');
  host.querySelectorAll('[data-qc]').forEach((cb) => {
    cb.addEventListener('change', async () => {
      const payload = { id: editingOrder, qc_done: JSON.stringify(
        Object.fromEntries([...host.querySelectorAll('[data-qc]')].map((x) => [x.dataset.qc, x.checked]))) };
      try {
        await post('/api/order/save', payload);
        toast('Чек-лист обновлён');
        await loadOrderCompletion(editingOrder);
      } catch (e) { fail(e); }
    });
  });
}

/* ============================================================= клиенты */
function renderCustomers() {
  const q = ($('customers_search').value || '').trim().toLowerCase();
  const list = PF.state.customers.filter((c) => !q ||
    [c.name, c.phone, c.messenger, c.company].some((v) => String(v || '').toLowerCase().includes(q)));
  const repeat = PF.state.customers.filter((c) => num(c.orders) > 1).length;
  $('customers_kpi').innerHTML = `<span class="chip">Всего <b>&nbsp;${PF.state.customers.length}</b></span>`
    + `<span class="chip ok">Постоянных <b>&nbsp;${repeat}</b></span>`;
  $('customers_tbody').innerHTML = list.length ? list.map((c) => {
    const seg = num(c.orders) > 2 ? ['ok', 'Постоянный'] : num(c.orders) > 1 ? ['accent', 'Повторный'] : ['outline', 'Новый'];
    return `<tr><td><div class="cell-user"><span class="avatar">${esc(initials(c.name))}</span>`
      + `<span><b>${esc(c.name || 'Без имени')}</b>${c.company ? `<small>${esc(c.company)}</small>` : ''}</span></div></td>`
      + `<td>${esc(c.phone || '—')}${c.messenger ? `<br><small class="muted">${esc(c.messenger)}</small>` : ''}</td>`
      + `<td class="right tnum">${nfmt(c.orders)}</td>`
      + `<td class="right tnum">${money(c.revenue)}</td>`
      + `<td>${c.last_order ? esc(dateText(c.last_order)) : '—'}</td>`
      + `<td><span class="chip ${seg[0]}">${seg[1]}</span></td></tr>`;
  }).join('') : '<tr><td colspan="6"><div class="empty compact"><span>Клиенты появятся после первого заказа.</span></div></td></tr>';
}

/* =============================================== обратная связь после продажи */
const AFTERCARE_STATE = {
  ready: ['ok', 'Можно попросить отзыв'],
  waiting: ['accent', 'Ожидаем ответ'],
  received: ['outline', 'Ответ сохранён'],
  scheduled: ['', 'Ещё рано'],
  no_contact: ['warn', 'Нет контакта'],
};

function requestKey(prefix) {
  if (window.crypto && window.crypto.randomUUID) return `${prefix}-${window.crypto.randomUUID()}`;
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function renderAftercare() {
  const host = $('aftercare_list');
  if (!host) return;
  const ready = aftercareItems.filter((i) => i.state === 'ready').length;
  const waiting = aftercareItems.filter((i) => i.state === 'waiting').length;
  const received = aftercareItems.filter((i) => i.state === 'received').length;
  $('aftercare_kpi').innerHTML = `<span class="chip ok">Готово <b>&nbsp;${ready}</b></span>`
    + `<span class="chip accent">Ждём ответ <b>&nbsp;${waiting}</b></span>`
    + `<span class="chip outline">Получено <b>&nbsp;${received}</b></span>`;
  const visible = aftercareItems.filter((i) => i.state !== 'no_contact').slice(0, 20);
  host.innerHTML = visible.length ? visible.map((item) => {
    const o = item.order || {}, f = item.feedback || {};
    const state = AFTERCARE_STATE[item.state] || ['', item.state];
    const detail = item.state === 'scheduled'
      ? `рекомендуемый срок через ${nfmt(item.wait_days)} дн.`
      : item.state === 'received'
        ? `${nfmt(f.rating)}/5${f.feedback_text ? ` · ${esc(f.feedback_text)}` : ''}`
        : esc(item.contact || 'контакт не указан');
    let action = '';
    if (item.state === 'ready') action = 'Подготовить запрос';
    else if (item.state === 'waiting') action = 'Записать ответ';
    else if (item.state === 'received') action = 'Открыть';
    return `<div class="mini-row"><span class="dot ${item.state === 'ready' ? 'on' : ''}"></span>`
      + `<div class="mbody"><b>№${esc(o.number)} · ${esc(o.product || 'Заказ')} · ${esc(o.customer_name || 'клиент')}</b>`
      + `<small>${detail}</small></div><span class="chip ${state[0]}">${esc(state[1])}</span>`
      + (action ? `<button class="btn sm" type="button" data-aftercare="${esc(o.id)}">${action}</button>` : '')
      + '</div>';
  }).join('') : '<div class="empty compact"><span>Нет заказов, по которым пора запросить обратную связь.</span></div>';
}

async function loadAftercare() {
  const host = $('aftercare_list');
  if (!host) return;
  try {
    const result = await get('/api/aftercare/queue');
    aftercareItems = result.items || [];
    renderAftercare();
  } catch (e) {
    host.innerHTML = '<div class="empty compact"><span>Не удалось проверить очередь обратной связи.</span></div>';
    fail(e);
  }
}

function renderAftercareModal(item) {
  aftercareCurrent = item;
  const o = item.order || {}, f = item.feedback || {};
  $('aftercare_title').textContent = `Заказ №${o.number || ''} · ${o.product || 'Обратная связь'}`;
  $('aftercare_sub').textContent = `${o.customer_name || 'Клиент'}${item.contact ? ` · ${item.contact}` : ''}`;
  ['aftercare_request_block', 'aftercare_response_block', 'aftercare_done_block']
    .forEach((id) => { $(id).hidden = true; });
  $('aftercare_sent_confirmed').checked = false;
  $('aftercare_response_confirmed').checked = false;
  const info = $('aftercare_info');
  if (item.state === 'ready') {
    info.className = 'verdict ok';
    info.innerHTML = '<b>Текст готов.</b> PrintFlow ничего не отправлял.';
    $('aftercare_request_text').value = item.message || '';
    $('aftercare_request_block').hidden = false;
  } else if (item.state === 'waiting') {
    info.className = 'verdict';
    info.innerHTML = `<b>Запрос отмечен отправленным.</b> ${f.request_sent_at ? esc(dateTimeText(f.request_sent_at)) : ''}`;
    $('aftercare_rating').value = '';
    $('aftercare_response_text').value = '';
    $('aftercare_publish').value = 'not_asked';
    $('aftercare_repeat_interest').value = 'not_asked';
    $('aftercare_response_block').hidden = false;
  } else if (item.state === 'received') {
    const permission = { granted: 'публикация разрешена', denied: 'публикация запрещена', not_asked: 'о публикации не спрашивали' };
    const interest = { yes: 'интерес к повтору подтверждён', no: 'повтор не нужен', not_asked: 'повтор не обсуждали' };
    info.className = `verdict ${num(f.rating) <= 3 ? 'warn' : 'ok'}`;
    info.innerHTML = `<b>Оценка ${nfmt(f.rating)}/5.</b> ${num(f.rating) <= 3 ? 'Сначала разберите замечание клиента.' : 'Ответ сохранён отдельно от разрешения.'}`;
    $('aftercare_feedback_view').innerHTML = `<p>${esc(f.feedback_text || 'Текстовый ответ не записан.')}</p>`
      + `<div class="chips"><span class="chip">${esc(permission[f.publish_permission] || permission.not_asked)}</span>`
      + `<span class="chip">${esc(interest[f.repeat_interest] || interest.not_asked)}</span></div>`;
    $('aftercare_reply_text').value = item.message_after_feedback || '';
    $('aftercare_prepare_repeat').hidden = !item.can_prepare_repeat;
    $('aftercare_done_block').hidden = false;
  } else {
    info.className = 'verdict warn';
    info.textContent = item.state === 'scheduled'
      ? `Рекомендуемый срок запроса наступит через ${item.wait_days} дн.`
      : 'У заказа нет контакта для внешней связи.';
  }
}

async function openAftercare(orderId) {
  try {
    const item = await get(`/api/aftercare/summary?id=${encodeURIComponent(orderId)}`);
    renderAftercareModal(item);
    openModal('aftercare_modal');
  } catch (e) { fail(e); }
}

async function copyAftercare(id, success) {
  const value = ($(id) || {}).value || '';
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast(success, 'текст скопирован, но не отправлен');
  } catch (e) { fail(new Error('Не удалось скопировать текст')); }
}

/* =============================================================== ниши */
function nicheVerdict(n) {
  const orders = num(n.orders), leads = num(n.leads), views = num(n.views);
  const pph = num(n.profit_per_hour), target = num(PF.state.settings.target_profit_per_hour, 250);
  if (!orders && !leads) return ['', 'Данных ещё нет. Покажите предложение и запишите показы и обращения.'];
  if (!orders) return ['warn', `Обращения есть (${nfmt(leads)}), заказов нет. Проверьте цену, сроки и то, как формулируете предложение.`];
  if (pph >= target) return ['ok', `Ниша работает: ${money(pph)} прибыли за час печати при норме ${money(target)}. Масштабируйте ассортимент.`];
  if (pph > 0) return ['warn', `Прибыль есть, но ${money(pph)} за час печати ниже нормы ${money(target)}. Поднимите цену или сократите время печати.`];
  return ['bad', 'Ниша убыточна по факту. Пересчитайте цену или закройте гипотезу.'];
}
function renderNiches() {
  const host = $('niche_grid');
  host.innerHTML = PF.state.niches.length ? PF.state.niches.map((n) => {
    const [kind, verdict] = nicheVerdict(n);
    return `<article class="niche-card" data-niche="${esc(n.id)}">`
      + `<div class="nhead"><span class="nic" style="background:${esc(n.color)}22;color:${esc(n.color)}">${esc(n.icon || '◆')}</span>`
      + `<div style="flex:1"><h3>${esc(n.name)}</h3><small class="muted">${esc(n.hypothesis || 'Гипотеза не описана')}</small></div>`
      + `<button class="icon-btn sm" type="button" data-niche-edit="${esc(n.id)}">✎</button></div>`
      + '<div class="funnel">'
      + `<div><span>Показы</span><b>${nfmt(n.views)}</b></div>`
      + `<div><span>Обращения</span><b>${nfmt(n.leads)}</b></div>`
      + `<div><span>Заказы</span><b>${nfmt(n.orders)}</b></div>`
      + `<div><span>Повторных</span><b>${nfmt(n.repeat_buyers)}</b></div>`
      + '</div>'
      + '<div class="res-row"><span class="lbl">Выручка</span><span class="val">' + money(n.revenue) + '</span></div>'
      + '<div class="res-row"><span class="lbl">Прибыль</span><span class="val ' + (num(n.profit) >= 0 ? 'pos' : 'neg') + '">' + money(n.profit) + '</span></div>'
      + '<div class="res-row"><span class="lbl">Часы печати</span><span class="val">' + hoursText(n.hours) + '</span></div>'
      + '<div class="res-row"><span class="lbl">Прибыль за час</span><span class="val">' + (num(n.hours) ? money(n.profit_per_hour) : '—') + '</span></div>'
      + `<div class="verdict ${kind}" style="margin-top:11px">${esc(verdict)}</div>`
      + '</article>';
  }).join('') : '<div class="empty"><span class="big">◫</span><b>Ниш пока нет</b><span>Добавьте гипотезу, чтобы сравнивать направления по фактической прибыли.</span></div>';
}

function openNiche(id) {
  editingNiche = id || null;
  const n = id ? PF.niche(id) : null;
  const data = n || { name: '', icon: '◆', color: '#4f46e5', views: 0, leads: 0, hypothesis: '', target: '', active: 1 };
  ['name', 'icon', 'color', 'views', 'leads', 'hypothesis', 'target'].forEach((k) => { $((k === 'name' ? 'niche_name' : 'nf_' + k)).value = data[k] ?? ''; });
  $('nf_active').value = String(num(data.active, 1) ? 1 : 0);
  $('niche_modal_title').textContent = id ? 'Настройка ниши' : 'Новая ниша';
  $('niche_delete').hidden = !id;
  openModal('niche_modal');
}

/* ============================================================ статусы */
function renderStatusEditor() {
  $('status_editor').innerHTML = statusDraft.map((s, i) => `<div class="status-row">`
    + `<input type="color" value="${esc(s.color || '#64748b')}" data-st-color="${i}">`
    + `<input value="${esc(s.name)}" data-st-name="${i}" placeholder="Название">`
    + `<label class="check" title="Финальный статус закрывает заказ"><input type="checkbox" data-st-final="${i}"${num(s.is_final) ? ' checked' : ''}>финал</label>`
    + `<button class="icon-btn sm" type="button" data-st-up="${i}"${i === 0 ? ' disabled' : ''}>↑</button>`
    + `<button class="icon-btn sm" type="button" data-st-down="${i}"${i === statusDraft.length - 1 ? ' disabled' : ''}>↓</button>`
    + `<button class="icon-btn sm danger" type="button" data-st-del="${i}"${statusDraft.length < 2 ? ' disabled' : ''}>×</button>`
    + '</div>').join('');
}
function readStatusEditor() {
  $$('[data-st-name]').forEach((el) => { statusDraft[+el.dataset.stName].name = el.value.trim() || statusDraft[+el.dataset.stName].name; });
  $$('[data-st-color]').forEach((el) => { statusDraft[+el.dataset.stColor].color = el.value; });
  $$('[data-st-final]').forEach((el) => { statusDraft[+el.dataset.stFinal].is_final = el.checked ? 1 : 0; });
}

/* ============================================================== экспорт */
function exportCsv() {
  const rows = [['Номер', 'Изделие', 'Клиент', 'Телефон', 'Ниша', 'Статус', 'Кол-во',
    'Граммы', 'Часы', 'Цена', 'Себестоимость', 'Прибыль', 'Предоплата', 'Срок', 'Создан']];
  filtered().forEach((o) => {
    const n = PF.niche(o.niche_id), e = o.economics || {};
    rows.push([o.number, o.product, o.customer_name, o.phone, n ? n.name : '',
      PF.status(o.status).name, o.qty, o.grams, o.hours, o.price, e.cost, e.profit,
      o.prepaid, o.due, (o.created_at || '').slice(0, 10)]);
  });
  const csv = '\uFEFF' + rows.map((r) => r.map((c) => {
    const v = String(c ?? '');
    return /[",;\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }).join(';')).join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `printflow-заказы-${todayISO()}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  toast('CSV выгружен', `${rows.length - 1} заказов`);
}

/* ================================================ входящий заказ из текста */
function openOrderIntake() {
  $('intake_text').value = '';
  $('intake_channel').value = 'telegram';
  openModal('order_intake_modal');
  setTimeout(() => $('intake_text').focus(), 50);
}

async function previewOrderIntake() {
  const text = $('intake_text').value.trim();
  if (!text) return fail(new Error('Вставьте сообщение клиента'));
  const button = $('intake_preview');
  button.disabled = true;
  button.textContent = 'Разбираю…';
  try {
    const result = await post('/api/order/intake/preview', {
      text,
      channel: $('intake_channel').value,
    });
    closeModal('order_intake_modal');
    await openOrder(null, result.draft || {}, result);
  } catch (e) { fail(e); }
  finally {
    button.disabled = false;
    button.textContent = 'Разобрать и заполнить';
  }
}

/* ============================================================= события */
function bind() {
  $('orders_new').addEventListener('click', () => openOrder());
  $('orders_intake').addEventListener('click', openOrderIntake);
  $('intake_preview').addEventListener('click', previewOrderIntake);
  $('intake_text').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') previewOrderIntake();
  });
  $('orders_export').addEventListener('click', exportCsv);
  $('orders_tbody').addEventListener('change', (e) => {
    const cb = e.target.closest('[data-bulk]');
    if (!cb) return;
    if (cb.checked) bulkSelected.add(cb.dataset.bulk);
    else bulkSelected.delete(cb.dataset.bulk);
    updateBulkBar();
  });
  $('bulk_clear').addEventListener('click', () => { bulkSelected.clear(); updateBulkBar(); });
  $('bulk_apply').addEventListener('click', async () => {
    const status = $('bulk_status').value;
    const label = ($('bulk_status').selectedOptions[0] || {}).textContent || status;
    if (!status || !bulkSelected.size) return;
    if (!confirmDanger(`Сменить статус у ${bulkSelected.size} заказов на «${label}»?`)) return;
    try {
      const res = await post('/api/orders/bulk-status', { ids: [...bulkSelected], status });
      toast('Готово', `Статус сменён у ${res.updated} заказов`);
      bulkSelected.clear();
      await PF.refreshCore();
      renderOrders();
    } catch (e) { fail(e); }
  });
  $('orders_search').addEventListener('input', debounce((e) => { filters.q = e.target.value; renderOrders(); }, 180));
  $('orders_filter_status').addEventListener('change', (e) => { filters.status = e.target.value; renderOrders(); });
  $('orders_filter_niche').addEventListener('change', (e) => { filters.niche = e.target.value; renderOrders(); });
  $('orders_view').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-mode]');
    if (!btn) return;
    orderView = btn.dataset.mode;
    $$('#orders_view button').forEach((b) => b.classList.toggle('on', b === btn));
    renderOrders();
  });
  document.addEventListener('click', (e) => {
    const card = e.target.closest('[data-order]');
    if (card && !e.target.closest('button')) { openOrder(card.dataset.order); return; }
    const ne = e.target.closest('[data-niche-edit]');
    if (ne) { openNiche(ne.dataset.nicheEdit); }
  });

  const spoolAdd = $('of_spool_add');
  if (spoolAdd) spoolAdd.addEventListener('click', () => {
    const host = $('of_spool_rows');
    if (!host) return;
    const rows = snapshotSpoolRows();
    rows.push({});
    renderSpoolRows(JSON.stringify(rows));
  });
  const spoolHost = $('of_spool_rows');
  if (spoolHost) {
    spoolHost.addEventListener('click', (e) => {
      const del = e.target.closest('[data-spool-del]');
      if (!del) return;
      del.closest('.of-spool-row').remove();
      if (!$$('#of_spool_rows .of-spool-row').length) renderSpoolRows('[]');
      distributeSpoolGrams();
    });
    spoolHost.addEventListener('change', (e) => {
      if (e.target.matches('[data-spool-sel]')) distributeSpoolGrams();
    });
  }
  const spoolAuto = $('of_spool_auto');
  if (spoolAuto) spoolAuto.addEventListener('click', autoSpoolsFromAms);

  /* ---- состав заказа (мультизаказ) ---- */
  const itemAdd = $('of_item_add');
  if (itemAdd) itemAdd.addEventListener('click', () => {
    const current = collectOrderItems();
    current.push({});
    renderOrderItems(current);
  });
  const itemsHost = $('of_items');
  if (itemsHost) {
    itemsHost.addEventListener('click', (e) => {
      const del = e.target.closest('[data-item-del]');
      if (!del) return;
      del.closest('.of-item-row').remove();
      if (!$$('#of_items .of-item-row').length) renderOrderItems([{}]);
      updateOrderItemsSummary();
    });
    itemsHost.addEventListener('change', (e) => {
      const sel = e.target.closest('[data-item-nom]');
      if (sel) {
        const item = (PF.state.nomenclature || []).find((i) => i.id === sel.value);
        const row = sel.closest('.of-item-row');
        if (item && row) {
          if (item.name) row.querySelector('[data-item-name]').value = item.name;
          if (num(item.price)) row.querySelector('[data-item-price]').value = item.price;
          if (num(item.grams)) row.querySelector('[data-item-grams]').value = item.grams;
        }
      }
      updateOrderItemsSummary();
    });
    itemsHost.addEventListener('input', debounce(updateOrderItemsSummary, 200));
  }

  const amsBtn = $('of_ams_btn');
  if (amsBtn) amsBtn.addEventListener('click', fillFromAms);
  const gramsRefreshBtn = $('of_grams_refresh');
  if (gramsRefreshBtn) gramsRefreshBtn.addEventListener('click', (e) => {
    e.preventDefault();
    refreshOrderGrams(true).catch(fail);
  });
  // hint: показать текущий AMS цвет
  const hintEl = $('of_ams_hint');
  if (hintEl) {
    const t = currentAmsTray();
    if (t) hintEl.textContent = `AMS: ${t.type || ''} ${amsHexToName(t.color) || ''}`.trim() || t.label || '';
  }
  ['grams', 'hours', 'price', 'cost', 'prepaid', 'qty', 'manual_minutes'].forEach((k) =>
    $('of_' + k).addEventListener('input', () => {
      updateEconDebounced();
      if (k === 'grams' || k === 'qty') distributeSpoolGrams();
      if (k === 'qty') updateReadyStockHint();
    }));
  $('of_file').addEventListener('change', fillFromFileEstimate);

  function applyProduct(item, ready) {
    if (!item) return;
    $('of_product').value = item.name || $('of_product').value;
    const qty = Math.max(1, num(($('of_qty') || {}).value, 1));
    // Нормативы простого заказа хранятся на единицу; сервер сам применяет qty.
    if (num(item.grams)) $('of_grams').value = Math.round(num(item.grams) * 10) / 10;
    if (num(item.hours)) $('of_hours').value = Math.round(num(item.hours) * 100) / 100;
    if (item.material) $('of_material').value = item.material;
    if (num(item.price)) $('of_price').value = Math.round(num(item.price) * qty * 100) / 100;
    if (item.file) $('of_file').value = item.file;
    if (item.niche_id) $('of_niche_id').value = item.niche_id;
    if (ready) $('of_reserved').checked = num(item.free) >= Math.max(1, num($('of_qty').value, 1));
    distributeSpoolGrams(true);
    updateReadyStockHint();
    updateEconDebounced();
    toast(ready ? 'Готовый товар добавлен' : 'Подставлено из базы', item.name);
  }
  $('of_nom_id').addEventListener('change', () => {
    const item = (PF.state.nomenclature || []).find((i) => i.id === $('of_nom_id').value);
    if (!item) { $('of_reserved').checked = false; updateReadyStockHint(); return; }
    applyProduct(item, true);
  });
  // Автоподстановка поддерживает и новый справочник товаров, и старую базу изделий.
  $('of_product').addEventListener('change', () => {
    const name = $('of_product').value.trim().toLowerCase();
    if (!name) return;
    const item = [...(PF.state.nomenclature || []), ...(PF.state.catalog || [])]
      .find((c) => String(c.name || '').toLowerCase() === name);
    if (!item) return;
    if (item.id && (PF.state.nomenclature || []).some((i) => i.id === item.id)) $('of_nom_id').value = item.id;
    applyProduct(item, Boolean(item.free));
  });
  $('order_save').addEventListener('click', () => saveOrder(false));
  $('order_save_prepare').addEventListener('click', () => saveOrder(true));
  $('order_duplicate').addEventListener('click', async () => {
    if (!editingOrder) return;
    try {
      const res = await post('/api/order/duplicate', { id: editingOrder });
      closeModal('order_modal');
      toast('Заказ повторён', `№${res.order.number} · ${res.order.product}`);
      await PF.refreshCore();
      PF.refreshFinance();
      PF.modules.ops.openOrder(res.order.id);
    } catch (e) { fail(e); }
  });
  $('order_b2b').addEventListener('click', () => {
    if (!editingOrder) return;
    const kind = window.prompt('Документ: счёт (invoice), КП (cp) или товарный чек (receipt)?', 'invoice');
    if (!kind) return;
    window.open(`/api/b2b/doc?id=${encodeURIComponent(editingOrder)}&kind=${encodeURIComponent(kind)}`, '_blank');
  });
  $('order_accept_result').addEventListener('click', async () => {
    if (!editingOrder) return;
    const warning = 'Подтвердите, что вы осмотрели изделие и результат можно отдавать клиенту. Заказ перейдёт в статус «Готов». Сообщение будет только подготовлено — отправки не будет.';
    if (!confirmDanger(warning)) return;
    try {
      const result = await post('/api/order/accept', {
        id: editingOrder, quality_confirmed: true,
      });
      $('of_status').value = 'ready';
      $('of_quality').value = 'passed';
      $('of_completion_message').value = result.message || '';
      $('of_completion_message_wrap').hidden = false;
      $('order_accept_result').hidden = true;
      $('order_fulfill').hidden = false;
      let copied = false;
      if (result.message && navigator.clipboard) {
        try { await navigator.clipboard.writeText(result.message); copied = true; } catch (e) { /* текст остаётся в поле */ }
      }
      toast('Результат принят', copied
        ? 'заказ готов · текст клиенту скопирован, но не отправлен'
        : 'заказ готов · текст клиенту подготовлен, но не отправлен');
      await PF.refreshCore();
      await loadOrderCompletion(editingOrder);
    } catch (e) { fail(e); }
  });
  $('order_copy_ready_message').addEventListener('click', async () => {
    const text = $('of_completion_message').value || '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast('Текст скопирован', 'сообщение не отправлено автоматически');
    } catch (e) { fail(new Error('Не удалось скопировать текст')); }
  });
  $('order_fulfill').addEventListener('click', () => openOrderFulfillment(editingOrder));
  $('hf_payment_action').addEventListener('change', updateFulfillmentPaymentFields);
  $('fulfillment_confirm').addEventListener('click', confirmOrderFulfillment);
  $('order_delete').addEventListener('click', async () => {
    if (!editingOrder || !confirmDanger('Удалить заказ? Действие необратимо.')) return;
    try {
      await post('/api/order/delete', { id: editingOrder });
      closeModal('order_modal');
      toast('Заказ удалён');
      await PF.refreshCore();
      PF.refreshFinance();
    } catch (e) { fail(e); }
  });
  $('order_defect_btn').addEventListener('click', () => openDefect(editingOrder));
  $('df_job').addEventListener('change', () => refreshDefectPreview(true));
  $('defect_reason').addEventListener('change', () => refreshDefectPreview(false));
  $('df_grams').addEventListener('input', debounce(() => refreshDefectPreview(false), 180));
  $('defect_save').addEventListener('click', saveDefect);
  bindOrderPhotos();

  $('order_queue').addEventListener('click', () => saveOrder(true));
  $('of_prepare_printer').addEventListener('change', () => loadOrderReadiness(
    editingOrder, $('of_prepare_printer').value, $('of_prepare_spool').value));
  $('of_prepare_spool').addEventListener('change', () => loadOrderReadiness(
    editingOrder, $('of_prepare_printer').value, $('of_prepare_spool').value));

  $('customers_search').addEventListener('input', debounce(renderCustomers, 180));
  $('aftercare_refresh').addEventListener('click', loadAftercare);
  $('aftercare_list').addEventListener('click', (e) => {
    const button = e.target.closest('[data-aftercare]');
    if (button) openAftercare(button.dataset.aftercare);
  });
  $('aftercare_copy_request').addEventListener('click', () =>
    copyAftercare('aftercare_request_text', 'Запрос скопирован'));
  $('aftercare_copy_reply').addEventListener('click', () =>
    copyAftercare('aftercare_reply_text', 'Ответ скопирован'));
  $('aftercare_confirm_sent').addEventListener('click', async () => {
    if (!aftercareCurrent) return;
    if (!$('aftercare_sent_confirmed').checked) {
      return fail(new Error('Подтвердите фактическую отправку запроса клиенту'));
    }
    const button = $('aftercare_confirm_sent');
    button.disabled = true;
    try {
      const result = await post('/api/aftercare/request/confirm', {
        id: aftercareCurrent.order.id,
        sent_confirmed: true,
        request_id: requestKey('feedback-send'),
      });
      renderAftercareModal(result);
      toast('Отправка зафиксирована', 'повторный запрос не будет создан');
      await loadAftercare();
    } catch (e) { fail(e); } finally { button.disabled = false; }
  });
  $('aftercare_save_response').addEventListener('click', async () => {
    if (!aftercareCurrent) return;
    if (!$('aftercare_response_confirmed').checked) {
      return fail(new Error('Подтвердите, что ответ действительно получен'));
    }
    if (!$('aftercare_rating').value) return fail(new Error('Выберите оценку клиента'));
    const button = $('aftercare_save_response');
    button.disabled = true;
    try {
      const result = await post('/api/aftercare/response', {
        id: aftercareCurrent.order.id,
        response_received: true,
        rating: $('aftercare_rating').value,
        text: $('aftercare_response_text').value.trim(),
        publish_permission: $('aftercare_publish').value,
        repeat_interest: $('aftercare_repeat_interest').value,
        request_id: requestKey('feedback-response'),
      });
      renderAftercareModal(result);
      toast(num(result.feedback.rating) <= 3 ? 'Замечание сохранено' : 'Отзыв сохранён',
        result.feedback.publish_permission === 'granted'
          ? 'разрешение зафиксировано, автопубликации не было'
          : 'публикации не было');
      await loadAftercare();
    } catch (e) { fail(e); } finally { button.disabled = false; }
  });
  $('aftercare_prepare_repeat').addEventListener('click', async () => {
    if (!aftercareCurrent) return;
    const warning = 'Создать новый черновик на основе завершённого заказа? Это не запускает печать и не отправляет клиенту подтверждение.';
    if (!confirmDanger(warning)) return;
    const button = $('aftercare_prepare_repeat');
    button.disabled = true;
    try {
      const result = await post('/api/aftercare/repeat', {
        id: aftercareCurrent.order.id,
        repeat_confirmed: true,
        request_id: requestKey('feedback-repeat'),
      });
      closeModal('aftercare_modal');
      toast('Черновик повтора создан', `заказ №${result.order.number} требует проверки`);
      await Promise.all([PF.refreshCore(), loadAftercare()]);
      openOrder(result.order.id);
    } catch (e) { fail(e); } finally { button.disabled = false; }
  });

  $('niche_add').addEventListener('click', () => openNiche());
  $('niche_save').addEventListener('click', async () => {
    const payload = {
      id: editingNiche || '',
      name: $('niche_name').value.trim(),
      icon: $('nf_icon').value.trim() || '◆',
      color: $('nf_color').value,
      views: num($('nf_views').value),
      leads: num($('nf_leads').value),
      hypothesis: $('nf_hypothesis').value.trim(),
      target: $('nf_target').value.trim(),
      active: +$('nf_active').value,
    };
    if (!payload.name) return fail(new Error('Укажите название ниши'));
    try {
      await post('/api/niche/save', payload);
      closeModal('niche_modal');
      await PF.refreshLists();
      fillSelectors();
      renderNiches();
      toast('Ниша сохранена', payload.name);
    } catch (e) { fail(e); }
  });
  $('niche_delete').addEventListener('click', async () => {
    if (!editingNiche || !confirmDanger('Удалить нишу? Заказы останутся, но потеряют привязку.')) return;
    try {
      await post('/api/niche/delete', { id: editingNiche });
      closeModal('niche_modal');
      await PF.refreshLists();
      fillSelectors();
      renderNiches();
      toast('Ниша удалена');
    } catch (e) { fail(e); }
  });

  $('orders_statuses').addEventListener('click', () => {
    statusDraft = PF.state.statuses.map((s) => Object.assign({}, s));
    renderStatusEditor();
    openModal('status_modal');
  });
  $('status_add').addEventListener('click', () => {
    readStatusEditor();
    statusDraft.push({ id: 'st_' + Date.now().toString(36), name: 'Новый статус', color: '#64748b', is_final: 0 });
    renderStatusEditor();
  });
  $('status_editor').addEventListener('click', (e) => {
    const up = e.target.closest('[data-st-up]'), down = e.target.closest('[data-st-down]'), del = e.target.closest('[data-st-del]');
    if (!up && !down && !del) return;
    readStatusEditor();
    if (up) { const i = +up.dataset.stUp; [statusDraft[i - 1], statusDraft[i]] = [statusDraft[i], statusDraft[i - 1]]; }
    if (down) { const i = +down.dataset.stDown; [statusDraft[i + 1], statusDraft[i]] = [statusDraft[i], statusDraft[i + 1]]; }
    if (del) {
      const i = +del.dataset.stDel;
      const used = PF.state.orders.filter((o) => o.status === statusDraft[i].id).length;
      if (used) return fail(new Error(`Статус используют ${used} заказ(ов). Сначала переведите их.`));
      statusDraft.splice(i, 1);
    }
    renderStatusEditor();
  });
  $('status_save').addEventListener('click', async () => {
    readStatusEditor();
    try {
      for (let i = 0; i < statusDraft.length; i++) {
        await post('/api/status/save', Object.assign({}, statusDraft[i], { position: i }));
      }
      const removed = PF.state.statuses.filter((s) => !statusDraft.some((d) => d.id === s.id));
      for (const s of removed) await post('/api/status/delete', { id: s.id });
      closeModal('status_modal');
      await PF.refreshLists();
      fillSelectors();
      renderOrders();
      toast('Статусы сохранены');
    } catch (e) { fail(e); }
  });
}

/* =============================================================== старт */
PF.on('ready', () => { bind(); fillSelectors(); renderNiches(); });
PF.on('data', () => { fillSelectors(); renderOrders(); renderCustomers(); });
PF.on('finance', () => { renderNiches(); });
PF.on('view', (detail) => { if (detail.view === 'customers') loadAftercare(); });

PF.modules.ops = { openOrder, openNiche, renderOrders, fillSelectors, loadAftercare };
})();
