/* PrintFlow 2.0 — операционный контур: заказы (канбан/таблица),
   клиенты, ниши и настройка статусов. Все данные — с сервера. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, money, nfmt, hoursText, dateText, dateTimeText,
  todayISO, initials, debounce, toast, fail, openModal, closeModal, confirmDanger } = U;
const { get, post } = PF.api;

let editingOrder = null, editingNiche = null, statusDraft = [];
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
    + `<div class="num">№${esc(o.number)}${o.qty > 1 ? ` · ${nfmt(o.qty)} шт` : ''}</div>`
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
  return rows.filter((r) => r && typeof r === 'object' && (r.spool_id || num(r.grams) > 0));
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
    if (id && grams > 0) out.push({ spool_id: id, grams });
  });
  return JSON.stringify(out);
}

function distributeSpoolGrams() {
  const rows = $$('#of_spool_rows .of-spool-row');
  if (!rows.length) return;
  const total = num($('of_grams').value) * Math.max(1, num($('of_qty').value, 1));
  if (!total) return;
  const colors = colorsToStr($('of_colors').value).split(',').map((part) => num(part.split(':')[1])).filter((g) => g > 0);
  const colorTotal = colors.reduce((a, b) => a + b, 0);
  rows.forEach((row, index) => {
    const input = row.querySelector('[data-spool-grams]');
    if (!input) return;
    if (colors[index] && colorTotal) input.value = Math.round(total * colors[index] / colorTotal * 10) / 10;
    else if (rows.length === 1) input.value = Math.round(total * 10) / 10;
    else if (!num(input.value)) input.value = Math.round(total / rows.length * 10) / 10;
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

async function fillFromFileEstimate() {
  const file = $('of_file').value.trim();
  if (!file) return;
  try {
    const res = await get('/api/estimate', { file });
    const est = res.estimate || {};
    const grams = num(est.grams) || num(est.total_grams);
    const minutes = num(est.minutes) || num(est.total_minutes);
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

async function openOrder(id) {
  editingOrder = id || null;
  fillSelectors();
  let order = id ? PF.state.orders.find((o) => o.id === id) : null;
  if (id) { try { order = await get('/api/order', { id }); } catch (e) { /* используем локальную копию */ } }
  const blank = {
    product: '', status: PF.state.statuses[0] ? PF.state.statuses[0].id : 'new', priority: 'normal',
    niche_id: '', channel: 'Полка магазина', qty: 1, due: '', customer_name: '', phone: '',
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
  const data = Object.assign({}, blank, order || {});
  OF.forEach((k) => {
    const el = $('of_' + k);
    if (!el) return;
    if (k === 'colors') { if (el) el.value = colorsToStr(data.colors); return; }
    el.value = data[k] == null ? '' : String(data[k]);
  });
  renderSpoolRows(data.spools);
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
  $('order_delete').hidden = !id;
  $('order_queue').hidden = !id;
  $('order_fulfill').hidden = !id;
  $('order_duplicate').hidden = !id;
  $('order_b2b').hidden = !id;

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
}

async function updateEcon() {
  const grams = num($('of_grams').value), hours = num($('of_hours').value);
  const price = num($('of_price').value), prepaid = num($('of_prepaid').value);
  const manual = num($('of_manual_minutes').value), qty = Math.max(1, num($('of_qty').value, 1));
  let cost = num($('of_cost').value);
  let auto = null;
  if (!cost && (grams || hours)) {
    try {
      auto = await post('/api/calc/cost', { grams: grams * qty, hours: hours * qty, manual_minutes: manual });
      cost = num(auto.total);
    } catch (e) { /* офлайн — оставим 0 */ }
  }
  const total = price * qty;
  const profit = total - cost;
  const perHour = hours * qty ? profit / (hours * qty) : 0;
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

async function saveOrder() {
  const payload = { id: editingOrder || '' };
  OF.forEach((k) => { const el = $('of_' + k); if (el) payload[k] = el.value; });
  if (payload.colors !== undefined) payload.colors = colorsToJson(payload.colors);
  payload.spools = collectSpoolRows();
  payload.reserved = $('of_reserved').checked ? 1 : 0;
  if (!payload.product.trim()) return fail(new Error('Укажите изделие или работу'));
  if (payload.reserved && !payload.nom_id) return fail(new Error('Для резерва выберите готовый товар из базы'));
  ['qty', 'grams', 'hours', 'manual_minutes', 'price', 'cost', 'prepaid'].forEach((k) => { payload[k] = num(payload[k]); });
  // поле «Оплачено» ведёт основной счётчик оплаты, prepaid оставляем для совместимости
  payload.paid = payload.prepaid;
  payload.auto_cost = +$('of_auto_cost').value;
  try {
    const res = await post('/api/order/save', payload);
    closeModal('order_modal');
    toast(editingOrder ? 'Заказ обновлён' : 'Заказ создан', `№${res.order.number} · ${res.order.product}`);
    await PF.refreshCore();
    PF.refreshFinance();
  } catch (e) { fail(e); }
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
  runout: 'Закончился пластик', warp: 'Деформация', other: 'Другое',
};
function renderOrderDefects(defects) {
  const wrap = $('of_defects_wrap');
  if (!wrap) return;
  wrap.hidden = !defects.length;
  if (defects.length) {
    $('of_defects').innerHTML = defects.map((d) => `<div class="tx-row">`
      + `<span class="tx-ic expense">✕</span>`
      + `<div class="tx-body"><b>${esc(DEFECT_REASONS[d.reason] || d.reason || 'Брак')}</b>`
      + `<small>${esc(dateTimeText(d.at))}${d.phase ? ' · ' + esc(d.phase) : ''}${d.code ? ' · ' + esc(d.code) : ''}${d.note ? ' · ' + esc(d.note) : ''}</small></div>`
      + `<span class="amt neg">${d.loss ? money(d.loss) : (d.grams ? nfmt(d.grams) + ' г' : '')}</span></div>`).join('');
  }
}
function openDefect(orderId) {
  const jobs = PF.state.jobs.history || [];
  $('df_job').innerHTML = '<option value="">Без задания</option>' + jobs.slice(0, 20).map((j) =>
    `<option value="${esc(j.id)}">${esc(j.name || j.file || 'печать')} · ${esc(dateText(j.finished_at))}</option>`).join('');
  $('defect_reason').value = 'detached';
  $('df_phase').value = 'middle';
  $('df_code').value = '';
  $('df_grams').value = '';
  $('df_loss').value = '';
  $('defect_note').value = '';
  $('df_job').dataset.orderId = orderId || '';
  openModal('defect_modal');
}
async function saveDefect() {
  const job_id = $('df_job').value;
  try {
    const res = await post('/api/defect/save', {
      job_id, order_id: $('df_job').dataset.orderId || null,
      reason: $('defect_reason').value, phase: $('df_phase').value,
      code: $('df_code').value.trim(), grams: num($('df_grams').value),
      loss: num($('df_loss').value), note: $('defect_note').value.trim(),
    });
    closeModal('defect_modal');
    toast('Брак записан', DEFECT_REASONS[res.defect.reason] || '');
    refreshOrderDetail();
    PF.refreshCore();
  } catch (e) { fail(e); }
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
      try { await post('/api/order/save', payload); toast('Чек-лист обновлён'); } catch (e) { fail(e); }
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

/* ============================================================= события */
function bind() {
  $('orders_new').addEventListener('click', () => openOrder());
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
    const current = collectSpoolRows();
    let rows = [];
    try { rows = JSON.parse(current || '[]'); } catch (e) { rows = []; }
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

  const amsBtn = $('of_ams_btn');
  if (amsBtn) amsBtn.addEventListener('click', fillFromAms);
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
    if (num(item.grams)) $('of_grams').value = item.grams;
    if (num(item.hours)) $('of_hours').value = item.hours;
    if (item.material) $('of_material').value = item.material;
    if (num(item.price)) $('of_price').value = item.price;
    if (item.file) $('of_file').value = item.file;
    if (item.niche_id) $('of_niche_id').value = item.niche_id;
    if (ready) $('of_reserved').checked = num(item.free) >= Math.max(1, num($('of_qty').value, 1));
    distributeSpoolGrams();
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
  $('order_save').addEventListener('click', saveOrder);
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
  $('order_fulfill').addEventListener('click', async () => {
    if (!editingOrder) return;
    const order = PF.state.orders.find((o) => o.id === editingOrder);
    const left = order ? Math.max(0, num(order.price) - Math.max(num(order.paid), num(order.prepaid))) : 0;
    const warn = left > 0 ? `\n\nОстаток ${money(left)} будет зачислен как оплата при выдаче.` : '';
    if (!confirmDanger(`Выдать заказ?${warn}`)) return;
    try {
      const res = await post('/api/order/fulfill', { id: editingOrder });
      closeModal('order_modal');
      if (res.message && navigator.clipboard) {
        navigator.clipboard.writeText(res.message).catch(() => {});
      }
      toast('Заказ выдан', res.collected
        ? `зачислено ${money(res.collected)} · текст клиенту скопирован`
        : 'текст клиенту скопирован');
      await PF.refreshCore();
      PF.refreshFinance();
    } catch (e) { fail(e); }
  });
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
  $('defect_save').addEventListener('click', saveDefect);
  bindOrderPhotos();

  $('order_queue').addEventListener('click', () => {
    const file = $('of_file').value.trim();
    if (!file) return fail(new Error('Сначала укажите файл модели на принтере'));
    post('/api/jobs/enqueue', {
      name: $('of_product').value.trim(), file, order_id: editingOrder,
      printer_id: PF.state.activePrinter, source: 'order',
    }).then(() => { toast('Задание добавлено в очередь', file); PF.refreshCore(); }).catch(fail);
  });

  $('customers_search').addEventListener('input', debounce(renderCustomers, 180));

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

PF.modules.ops = { openOrder, openNiche, renderOrders, fillSelectors };
})();
