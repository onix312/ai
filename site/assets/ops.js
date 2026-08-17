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

function renderTable(list) {
  $('orders_tbody').innerHTML = list.length ? list.map((o) => {
    const st = PF.status(o.status), n = PF.niche(o.niche_id), econ = o.economics || {};
    return `<tr class="clickable" data-order="${esc(o.id)}">`
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

  $('orders_kanban').hidden = orderView !== 'kanban';
  $('orders_table').hidden = orderView !== 'table';
  if (orderView === 'kanban') renderKanban(list); else renderTable(list);
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
  $('orders_filter_status').value = filters.status;
  $('orders_filter_niche').value = filters.niche;
}

const OF = ['product', 'status', 'priority', 'niche_id', 'channel', 'qty', 'due',
  'customer_name', 'phone', 'messenger', 'material', 'color', 'grams', 'hours',
  'manual_minutes', 'file', 'price', 'cost', 'prepaid', 'auto_cost', 'quality',
  'quality_note', 'notes'];

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
    quality_note: '', notes: '',
  };
  const data = Object.assign({}, blank, order || {});
  OF.forEach((k) => {
    const el = $('of_' + k);
    if (!el) return;
    el.value = data[k] == null ? '' : String(data[k]);
  });
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

  const jobs = (order && order.jobs) || [];
  $('of_jobs_wrap').hidden = !jobs.length;
  if (jobs.length) {
    $('of_jobs').innerHTML = jobs.map((j) => `<div class="tx-row">`
      + `<span class="tx-ic ${j.state === 'done' ? 'income' : 'expense'}">${j.state === 'done' ? '✓' : '•'}</span>`
      + `<div class="tx-body"><b>${esc(j.name || j.file || 'Печать')}</b>`
      + `<small>${esc(dateTimeText(j.finished_at || j.started_at))} · ${nfmt(j.grams)} г · ${U.minutesText(j.duration_min)}</small></div>`
      + `<span class="amt">${money(j.cost)}</span></div>`).join('');
  }
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
  if (!payload.product.trim()) return fail(new Error('Укажите изделие или работу'));
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
  ['name', 'icon', 'color', 'views', 'leads', 'hypothesis', 'target'].forEach((k) => { $('nf_' + k).value = data[k] ?? ''; });
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

  ['grams', 'hours', 'price', 'cost', 'prepaid', 'qty', 'manual_minutes'].forEach((k) =>
    $('of_' + k).addEventListener('input', updateEconDebounced));
  $('order_save').addEventListener('click', saveOrder);
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
      name: $('nf_name').value.trim(),
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
