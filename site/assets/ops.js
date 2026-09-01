/* PrintFlow 2.0 — операционный контур: заказы (канбан/таблица),
   клиенты, ниши и настройка статусов. Все данные — с сервера. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, clamp, money, nfmt, hoursText, minutesText, dateText, dateTimeText,
  todayISO, initials, debounce, toast, fail, openModal, closeModal, confirmDanger } = U;
const { get, post, api } = PF.api;

let editingOrder = null, editingOrderUpdatedAt = '', editingNiche = null, statusDraft = [];
let fulfillmentDraft = null;
let aftercareItems = [], aftercareCurrent = null;
let filters = { q: '', status: '', niche: '', chan: '' };
let orderView = 'kanban';
let orderDensity = false;
let customerSegment = 'all';

const PRIORITY = { low: 'Низкий', normal: 'Обычный', high: 'Высокий', urgent: 'Срочный' };

const overdue = (o) => o.due && o.due < todayISO() && !PF.isFinal(o);
const dueSoon = (o) => o.due && o.due === todayISO() && !PF.isFinal(o);

/* 13.1 (29): «через 5 ч 20 мин» / «завтра» — живой отсчёт до дедлайна. */
function liveDueText(due) {
  const ms = new Date(due + 'T23:59:59').getTime() - Date.now();
  if (ms <= 0) return 'сегодня';
  const h = Math.floor(ms / 36e5);
  if (h < 1) return `через ${Math.max(1, Math.round(ms / 6e4))} мин`;
  if (h < 24) {
    const m = Math.round((ms % 36e5) / 6e4);
    return `через ${h} ч${m ? ' ' + m + ' мин' : ''}`;
  }
  return `через ${Math.round(h / 24)} дн`;
}
function tickLiveDues() {
  if (!document.querySelector('#view-orders.on')) return;
  $$('.due-live').forEach((el) => {
    const until = el.dataset.until;
    const txt = el.querySelector('.due-live-txt');
    if (until && txt) txt.textContent = liveDueText(until.slice(0, 10));
  });
}
setInterval(tickLiveDues, 60000);

function filtered() {
  const q = filters.q.trim().toLowerCase();
  return PF.state.orders.filter((o) => {
    if (filters.status && o.status !== filters.status) return false;
    if (filters.niche && o.niche_id !== filters.niche) return false;
    if (filters.chan === 'telegram' && !isTgOrder(o)) return false;
    if (filters.chan === 'no-tg' && isTgOrder(o)) return false;
    if (!q) return true;
    return [o.number, o.product, o.customer_name, o.phone, o.file, o.notes]
      .some((v) => String(v || '').toLowerCase().includes(q));
  });
}

/* ============================================================== канбан */
/* ЗА2: заказ из Telegram (канал или источник клиентского бота) — узнаваем по
   иконке и уточнению источника; бейдж виден и в карточке, и в таблице. */
const TG_SOURCES = { telegram: 'заявка из чата', catalog: 'из витрины', custom: 'свой заказ', individual: 'индивидуальная' };
const isTgOrder = (o) => o.channel === 'telegram' || /^(telegram|catalog|custom|individual)$/.test(String(o.client_source || ''));
function tgChipOf(o) {
  const src = TG_SOURCES[o.client_source];
  // 13.1 (40): бейдж кликабелен — одним кликом в клиент-бот
  return `<button type="button" class="channel-chip tg" data-tg-open="${esc(o.id)}" title="Заказ из Telegram${src ? ' · ' + src : ''} — открыть клиент-бот">`
    + `<i data-icon="telegram">✈</i>Telegram${src ? ` · ${esc(src)}` : ''}</button>`;
}
/* Цвет аватара — детерминированный от имени: один клиент всегда одним тоном. */
function avColor(name) {
  const s = String(name || '');
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return `hsl(${h} 52% 46%)`;
}

function orderCard(o) {
  const n = PF.niche(o.niche_id);
  const st = PF.status(o.status);
  const econ = o.economics || {};
  const paid = Math.max(num(o.paid), num(o.prepaid));
  const price = num(o.price);
  const left = Math.max(0, price - paid);
  const cls = ['ocard'];
  if (o.priority === 'urgent') cls.push('urgent');
  if (overdue(o)) cls.push('late');
  if (o.cancel_requested_at) cls.push('cancel-req');

  // Приоритет
  let prioBadge = '';
  if (o.priority === 'urgent') {
    prioBadge = '<span class="prio-tag urgent" title="Срочный заказ"><i data-icon="bolt">⚡</i>Срочно</span>';
  } else if (o.priority === 'high') {
    prioBadge = '<span class="prio-tag high" title="Высокий приоритет"><i data-icon="bolt">🔥</i>Высокий</span>';
  } else if (o.priority === 'low') {
    prioBadge = '<span class="prio-tag low" title="Низкий приоритет">💤 Низкий</span>';
  }
  // ЗА3: запрос отмены из Telegram виден прямо на карточке
  if (o.cancel_requested_at) {
    prioBadge += '<span class="prio-tag cancel" title="Покупатель просит отменить — решите в карточке"><i data-icon="cancel">✕</i>Просит отмену</span>';
  }

  // Канал продаж
  const channelAliases = {
    direct: 'Прямой', shop: 'Витрина', telegram: 'Telegram',
    avito: 'Авито', ozon: 'Ozon', b2b: 'B2B',
  };
  const ch = (PF.state.channels || []).find((c) => c.id === o.channel);
  const channelName = ch ? ch.name : (channelAliases[o.channel] || (o.channel && o.channel !== 'direct' ? o.channel : ''));
  const channelChip = isTgOrder(o) ? tgChipOf(o)
    : (channelName ? `<span class="channel-chip">${esc(channelName)}</span>` : '');

  // Срок сдачи + 13.1 (29): живой отсчёт «до дедлайна» на ближайших заказах
  let dueBadge = '';
  if (o.due) {
    if (overdue(o)) {
      dueBadge = `<span class="due-badge bad" title="Срок сдачи просрочен"><i data-icon="timer">⚠</i>${esc(dateText(o.due))}</span>`;
    } else {
      const today = new Date().toISOString().slice(0, 10);
      const dueMs = new Date(o.due + 'T23:59:59').getTime();
      const hoursLeft = (dueMs - Date.now()) / 36e5;
      if (o.due === today) {
        dueBadge = '<span class="due-badge warn" title="Срок сегодня"><i data-icon="timer">⏳</i>Сегодня</span>';
      } else if (hoursLeft <= 72) {
        dueBadge = `<span class="due-badge warn due-live" data-until="${esc(o.due)}T23:59:59" title="До срока сдачи">`
          + `<i data-icon="timer">⏳</i><span class="due-live-txt">${liveDueText(o.due)}</span></span>`;
      } else {
        dueBadge = `<span class="due-badge" title="Срок сдачи"><i data-icon="timer">📅</i>${esc(dateText(o.due))}</span>`;
      }
    }
  }

  // Оплата
  let payChip = '';
  if (price > 0) {
    if (paid >= price) {
      payChip = '<span class="pay-chip ok" title="Заказ полностью оплачен">✓ Оплачен</span>';
    } else if (paid > 0) {
      payChip = `<span class="pay-chip warn" title="Оплачено ${money(paid)}, остаток ${money(left)}">Оплата ${money(paid)} · ост. ${money(left)}</span>`;
    } else {
      payChip = '<span class="pay-chip dim" title="Не оплачен">Не оплачен</span>';
    }
  }

  // Файл печати
  const fileChip = o.file ? `<div class="file-chip" title="Файл: ${esc(o.file)}"><i data-icon="cube">🧊</i>${esc(String(o.file).split('/').pop())}</div>` : '';

  // Параметры производства
  const specs = [];
  if (o.material) {
    specs.push(`<span class="spec-pill mat" title="Материал и цвет"><i data-icon="spool">🧵</i>${esc(o.material)}${o.color ? ' · ' + esc(o.color) : ''}</span>`);
  }
  if (num(o.grams)) {
    specs.push(`<span class="spec-pill" title="Вес пластика">⚖️ ${nfmt(o.grams)} г</span>`);
  }
  if (num(o.hours)) {
    specs.push(`<span class="spec-pill" title="Время печати">⏱ ${hoursText(o.hours)}</span>`);
  }
  if (n) {
    specs.push(`<span class="chip" style="background:${esc(n.color)}22;color:${esc(n.color)}">${esc(n.icon || '◆')} ${esc(n.name)}</span>`);
  }
  if (dueBadge) {
    specs.push(dueBadge);
  }

  // Прибыль
  let profitChip = '';
  if (econ.profit != null && price > 0) {
    const profitNum = num(econ.profit);
    const pos = profitNum >= 0;
    const marginStr = econ.margin != null ? ` (${nfmt(econ.margin, 0)}%)` : '';
    profitChip = `<span class="profit-chip ${pos ? 'pos' : 'neg'}" title="Расчётная прибыль">${pos ? '+' : ''}${money(profitNum)}${marginStr}</span>`;
  }

  // Количество / состав
  let qtyBadge = '';
  if (o.items_count > 1) {
    qtyBadge = `<span class="cnt-badge">${nfmt(o.items_count)} поз. · ${nfmt(o.qty)} шт</span>`;
  } else if (o.qty > 1) {
    qtyBadge = `<span class="cnt-badge">${nfmt(o.qty)} шт</span>`;
  }

  // ЗА1 + 13.1 (39): инициалы, а у безымянных — детерминированный эмодзи-аватар
  const who = o.customer_name || '';
  const avGlyph = who ? esc(initials(who)) : esc(U.avatarEmoji('', o.id) || '👤');
  const av = `<span class="who-av" style="--av:${avColor(o.customer_name)}" title="${esc(who || 'Без клиента')}">${avGlyph}</span>`;

  // 13.1 (32): прогресс печати прямо на карточке — «а оно уже печатается?»
  const job = (PF.state.jobs.queue || []).find((j) => j.state === 'running' && j.order && j.order.id === o.id);
  const printProg = job
    ? `<div class="ocard-print" title="Задание связано: ${esc(job.name || job.file || 'печать')}">`
      + `<div class="bar thin"><i style="width:${clamp(num(job.progress), 0, 100)}%"></i></div>`
      + `<small><i data-icon="printer">◉</i> Печатается · ${Math.round(clamp(num(job.progress), 0, 100))}%</small></div>`
    : '';

  return `<article class="${cls.join(' ')}" draggable="true" data-order="${esc(o.id)}">`
    + `<div class="strip" style="background:${esc(st.color)}"></div>`
    + `<div class="ocard-head">`
    + `<span class="num">№${esc(o.number)}</span>`
    + qtyBadge
    + prioBadge
    + channelChip
    + chatBadgeHtml(o.id)
    + `</div>`
    + `<h4>${esc(o.product || 'Без названия')}</h4>`
    + fileChip
    + printProg
    + `<div class="who-row">`
    + `<span class="who">${av}${who ? esc(who) : '<span class="muted">Без клиента</span>'}</span>`
    + (o.phone ? `<span class="phone-chip" title="Телефон"><i data-icon="phone">📞</i>${esc(o.phone)}</span>` : '')
    + `</div>`
    + (specs.length ? `<div class="ocard-specs">${specs.join('')}</div>` : '')
    + `<div class="ocard-foot">`
    + `<span class="price">${money(o.price)}</span>`
    + payChip
    + profitChip
    + `</div>`
    + `<div class="ocard-actions">`
    + `<button class="btn xs ghost" type="button" data-order-action="open" data-order="${esc(o.id)}" title="Открыть карточку заказа"><i data-icon="pen">✎</i> Открыть</button>`
    + (!st.is_final ? `<button class="btn xs ghost" type="button" data-order-action="queue" data-order="${esc(o.id)}" title="Добавить в очередь печати"><i data-icon="queue">⎙</i> В очередь</button>` : '')
    + `</div>`
    + `</article>`;
}

/* ЗА7: маркеры колонок + «докрут» сумм между обновлениями. */
const kanSums = new Map();

/* В40: по каким заказам покупатель ждёт ответа — пузырь переписки.
   Один лёгкий GET на обновление данных, а не разбор всей ленты. */
const chatBadges = new Map();
let chatBadgesAt = 0;
async function refreshChatBadges(force) {
  if (!force && Date.now() - chatBadgesAt < 120000) return;
  try {
    const data = await get('/api/conversations/by-order');
    const counts = (data && data.counts) || {};
    chatBadges.clear();
    Object.entries(counts).forEach(([orderId, cnt]) => {
      if (num(cnt) > 0) chatBadges.set(orderId, num(cnt));
    });
    chatBadgesAt = Date.now();
  } catch (e) { /* тихо: пузырь — подсказка, а не критичные данные */ }
}
function chatBadgeHtml(orderId) {
  const cnt = chatBadges.get(orderId) || 0;
  if (!cnt) return '';
  return `<button class="chat-badge" type="button" data-chat-open="${esc(orderId)}"`
    + ` title="Клиент ждёт ответа: ${cnt} в диалоге"><i data-icon="message">💬</i>${cnt}</button>`;
}
document.addEventListener('click', (e) => {
  const chip = e.target.closest('[data-chat-open]');
  if (!chip) return;
  location.hash = '#clientbot';
});

function renderKanban(list) {
  const host = $('orders_kanban');
  refreshChatBadges();
  if (!PF.state.orders.length) {
    host.innerHTML = '<div class="empty"><span class="big">▦</span><b>Заказов нет</b>'
      + '<span>Нет заказов — создайте из сообщения или с нуля.</span>'
      + '<button class="btn sm primary" type="button" data-empty-click="orders_new">+ Новый заказ</button></div>';
    return;
  }
  host.innerHTML = PF.state.statuses.map((st) => {
    const items = list.filter((o) => o.status === st.id);
    const sum = items.reduce((a, o) => a + num(o.price), 0);
    const lateN = items.filter(overdue).length;
    const tgN = items.filter(isTgOrder).length;
    const cancelN = items.filter((o) => o.cancel_requested_at).length;
    const marks = [
      lateN ? `<i class="km late" title="Просроченных: ${lateN}">●</i>` : '',
      tgN ? `<i class="km tg" title="Из Telegram: ${tgN}"><i data-icon="telegram">✈</i>${tgN}</i>` : '',
      cancelN ? `<i class="km cancel" title="Запросы на отмену: ${cancelN}"><i data-icon="cancel">✕</i></i>` : '',
    ].join('');
    return `<div class="kan-col${items.length ? '' : ' empty-col'}" data-status="${esc(st.id)}">`
      + `<div class="kan-head"><i style="background:${esc(st.color)}"></i><b>${esc(st.name)}</b>`
      + `<span class="kan-marks">${marks}</span><span class="n">${items.length}</span></div>`
      + (sum ? `<div class="kan-sum" data-sum="${esc(st.id)}">${money(sum)}</div>` : '')
      + (items.length ? items.map(orderCard).join('') : '')
      + '</div>';
  }).join('');
  if (window.PFIcons) window.PFIcons.apply(host);
  // Н3-приём: сумма колонки мягко докручивается, а не подменяется рывком
  host.querySelectorAll('.kan-sum').forEach((el) => {
    const key = el.dataset.sum;
    const prev = kanSums.get(key);
    if (prev && prev !== el.textContent) U.countUp(el, prev, el.textContent);
    kanSums.set(key, el.textContent);
  });
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
    return `<tr class="clickable${o.cancel_requested_at ? ' cancel-req' : ''}" data-order="${esc(o.id)}">`
      + `<td class="w-check" onclick="event.stopPropagation()"><input type="checkbox" data-bulk="${esc(o.id)}"${checked}></td>`
      + `<td class="strong">№${esc(o.number)}</td>`
      + `<td><b>${esc(o.product)}</b>${o.file ? `<br><small class="muted">${esc(o.file)}</small>` : ''}</td>`
      + `<td>${esc(o.customer_name || '—')}${o.phone ? `<br><small class="muted">${esc(o.phone)}</small>` : ''}`
      + (isTgOrder(o) ? `<br><span class="channel-chip tg mini" title="Заказ из Telegram"><i data-icon="telegram">✈</i>TG</span>` : '')
      + (o.cancel_requested_at ? `<br><small class="neg">✕ ${'просит отмену'}</small>` : '') + `</td>`
      + `<td>${n ? `${esc(n.icon || '')} ${esc(n.name)}` : '—'}</td>`
      + `<td><span class="chip" style="background:${esc(st.color)}22;color:${esc(st.color)}">${esc(st.name)}</span></td>`
      + `<td class="right tnum">${o.hours ? nfmt(o.hours, 1) : '—'}</td>`
      + `<td class="right tnum">${o.grams ? nfmt(o.grams) : '—'}</td>`
      + `<td class="right tnum">${money(o.price)}</td>`
      + `<td class="right tnum ${num(econ.profit) >= 0 ? 'pos' : 'neg'}">${money(econ.profit)}</td>`
      + `<td class="${overdue(o) ? 'neg' : ''}">${o.due ? esc(dateText(o.due)) : '—'}</td></tr>`;
  }).join('') : `<tr><td colspan="10">${!PF.state.orders.length
    ? '<div class="empty"><span class="big">▦</span><b>Заказов нет</b><span>Нет заказов — создайте из сообщения или с нуля.</span>'
      + '<button class="btn sm primary" type="button" data-empty-click="orders_new">+ Новый заказ</button></div>'
    : '<div class="empty compact"><span>Заказов не найдено.</span></div>'}</td></tr>`;
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
  $('orders_kanban').classList.toggle('compact', orderDensity);
  const odens = $('orders_density');
  if (odens) odens.classList.toggle('on', orderDensity);
  $('orders_table').hidden = orderView !== 'table';
  if (orderView === 'kanban') renderKanban(list); else {
    renderTable(list);
    if (window.PFIcons) window.PFIcons.apply($('orders_tbody'));
  }
  updateBulkBar();
}
function text(id, v) { const el = $(id); if (el) el.textContent = v; }

/* =============================================================== drag */
let dragId = null;
function bindDrag() {
  const board = $('orders_kanban');
  $$('.ocard').forEach((card) => {
    card.addEventListener('dragstart', () => {
      dragId = card.dataset.order;
      card.classList.add('dragging');
      if (board) board.classList.add('dragging-any');   // ЗА7: колонки подсказывают «брось сюда»
    });
    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      dragId = null;
      if (board) board.classList.remove('dragging-any');
    });
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
      } catch (err) {
        order.status = prev;
        renderOrders();
        // ЗА8: мягкий откат — колонка-получатель вспыхивает, если сервер возразил
        const host = $('orders_kanban');
        const flashCol = host && host.querySelector(`.kan-col[data-status="${status}"]`);
        if (flashCol) { flashCol.classList.add('flash'); setTimeout(() => flashCol.classList.remove('flash'), 650); }
        fail(err);
      }
    });
  });
}

function buildNomenclatureGroupedOptions(items, selectedId = '', filterGroupId = '', formatOption) {
  const groups = PF.state.groups || [];
  let list = items || [];
  if (filterGroupId) {
    list = list.filter((i) => String(i.group_id || '') === String(filterGroupId));
  }

  const defaultFmt = (i) => `${esc(i.name)} · готово ${nfmt(i.free)} шт${num(i.price) ? ' · ' + money(i.price) : ''}`;
  const fmt = formatOption || defaultFmt;

  if (filterGroupId || groups.length === 0) {
    return list.map((i) => {
      const sel = String(i.id) === String(selectedId) ? ' selected' : '';
      return `<option value="${esc(i.id)}"${sel}>${fmt(i)}</option>`;
    }).join('');
  }

  const byGroup = new Map();
  groups.forEach((g) => byGroup.set(g.id, []));
  byGroup.set('', []);

  list.forEach((item) => {
    const gid = item.group_id && byGroup.has(item.group_id) ? item.group_id : '';
    byGroup.get(gid).push(item);
  });

  let html = '';
  groups.forEach((g) => {
    const gItems = byGroup.get(g.id) || [];
    if (gItems.length) {
      html += `<optgroup label="📂 ${esc(g.name)}">`;
      gItems.forEach((i) => {
        const sel = String(i.id) === String(selectedId) ? ' selected' : '';
        html += `<option value="${esc(i.id)}"${sel}>${fmt(i)}</option>`;
      });
      html += `</optgroup>`;
    }
  });

  const noGroup = byGroup.get('') || [];
  if (noGroup.length) {
    if (groups.length > 0) html += `<optgroup label="📁 Без категории">`;
    noGroup.forEach((i) => {
      const sel = String(i.id) === String(selectedId) ? ' selected' : '';
      html += `<option value="${esc(i.id)}"${sel}>${fmt(i)}</option>`;
    });
    if (groups.length > 0) html += `</optgroup>`;
  }
  return html;
}

/* ========================================================= карточка заказа */
function fillSelectors() {
  const keepSel = (id) => ($(id) || {}).value || '';
  const keep = {
    of_status: keepSel('of_status'), of_niche_id: keepSel('of_niche_id'),
    of_channel: keepSel('of_channel'), of_nom_id: keepSel('of_nom_id'),
    of_category_filter: keepSel('of_category_filter'),
    of_warehouse_id: keepSel('of_warehouse_id'),
  };
  const statuses = PF.state.statuses.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
  $('of_status').innerHTML = statuses;
  $('orders_filter_status').innerHTML = '<option value="">Все статусы</option>' + statuses;
  const niches = PF.state.niches.map((n) => `<option value="${esc(n.id)}">${esc(n.icon || '◆')} ${esc(n.name)}</option>`).join('');
  $('of_niche_id').innerHTML = '<option value="">Без ниши</option>' + niches;
  $('orders_filter_niche').innerHTML = '<option value="">Все ниши</option>' + niches;
  const cf = $('cf_niche_id');
  if (cf) cf.innerHTML = '<option value="">Без ниши</option>' + niches;
  const channels = (PF.state.channels || []).filter((c) => num(c.active));
  const ch = $('of_channel');
  if (ch && channels.length) {
    const keepChannel = ch.value;
    ch.innerHTML = channels.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
    if (keepChannel && channels.some((c) => c.id === keepChannel)) ch.value = keepChannel;
  }
  $('customers_datalist').innerHTML = PF.state.customers
    .map((c) => `<option value="${esc(c.name)}">`).join('');
  const pd = $('products_datalist');
  if (pd) pd.innerHTML = [...(PF.state.nomenclature || []), ...(PF.state.catalog || [])]
    .map((c) => `<option value="${esc(c.name)}">`).join('');

  const catFilter = $('of_category_filter');
  if (catFilter) {
    catFilter.innerHTML = '<option value="">Все категории</option>'
      + (PF.state.groups || []).map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('');
    if (keep.of_category_filter && (PF.state.groups || []).some((g) => g.id === keep.of_category_filter)) {
      catFilter.value = keep.of_category_filter;
    }
  }

  const nom = $('of_nom_id');
  if (nom) {
    const catVal = (catFilter && catFilter.value) || '';
    nom.innerHTML = '<option value="">Не выбран — заказ на печать / услугу</option>'
      + buildNomenclatureGroupedOptions(PF.state.nomenclature || [], keep.of_nom_id, catVal);
    if (keep.of_nom_id && [...nom.options].some((o) => o.value === keep.of_nom_id)) {
      nom.value = keep.of_nom_id;
    }
  }

  const wh = $('of_warehouse_id');
  if (wh) wh.innerHTML = '<option value="">Автоматически</option>'
    + (PF.state.warehouses || []).map((w) => `<option value="${esc(w.id)}">${esc(w.name)}</option>`).join('');
  $('orders_filter_status').value = filters.status;
  $('orders_filter_niche').value = filters.niche;
  Object.keys(keep).forEach((id) => {
    const el = $(id);
    if (!el || !keep[id]) return;
    if ([...el.options].some((o) => o.value === keep[id])) el.value = keep[id];
  });
  toggleWarehouseField();
}

function toggleWarehouseField() {
  const nom = ($('of_nom_id') || {}).value || '';
  const wrap = $('of_warehouse_id') && $('of_warehouse_id').closest('label');
  if (wrap) wrap.hidden = !nom;
}

const OF = ['product', 'status', 'priority', 'niche_id', 'channel', 'qty', 'due',
  'customer_name', 'phone', 'messenger', 'material', 'color', 'grams', 'hours',
  'manual_minutes', 'file', 'price', 'cost', 'auto_cost', 'quality',
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
  const fmt = (i) => `${esc(i.name)}${num(i.price) ? ' · ' + money(i.price) : ''}${num(i.grams) ? ' · ' + nfmt(i.grams) + ' г/шт' : ''}`;
  return '<option value="">— из базы товаров —</option>'
    + buildNomenclatureGroupedOptions(PF.state.nomenclature || [], selected, '', fmt);
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
    + `<input type="number" min="0" step="any" data-item-hours value="${num(r.hours) ? esc(String(r.hours)) : ''}" placeholder="ч/шт из базы" title="Время печати штуки — подставляется из базы товаров">`
    + `<button class="btn sm ghost" type="button" data-item-create title="Создать товар в базе">+ товар</button>`
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
      hours: num((row.querySelector('[data-item-hours]') || {}).value),
    });
  });
  return out;
}

/** Создать карточку товара из полей заказа / строки состава. */
async function saveNomFromPrint(fields) {
  const name = String(fields.name || '').trim();
  if (!name) throw new Error('Укажите название изделия');
  const payload = {
    name,
    kind: 'product',
    grams: num(fields.grams),
    hours: num(fields.hours),
    material: String(fields.material || '').trim(),
    file: String(fields.file || '').trim(),
    niche_id: String(fields.niche_id || '').trim(),
    note: String(fields.note || '').trim(),
  };
  const price = num(fields.price);
  if (price > 0) payload.prices = { retail: price };
  const res = await post('/api/nomenclature/save', payload);
  const item = res.item || res;
  if (!item || !item.id) throw new Error('Товар не записался');
  try {
    const nom = await get('/api/nomenclature');
    PF.state.nomenclature = nom.items || [];
    if (nom.groups) PF.state.groups = nom.groups;
    if (nom.warehouses) PF.state.warehouses = nom.warehouses;
  } catch (e) {
    PF.state.nomenclature = (PF.state.nomenclature || []).concat([item]);
  }
  fillSelectors();
  return item;
}

async function createProductFromOrder() {
  const existing = ($('of_nom_id') || {}).value || '';
  if (existing) return toast('Товар уже выбран из базы', '', 'info');
  const qty = Math.max(1, num(($('of_qty') || {}).value, 1));
  const sum = num(($('of_price') || {}).value);
  try {
    const item = await saveNomFromPrint({
      name: ($('of_product') || {}).value,
      grams: ($('of_grams') || {}).value,
      hours: ($('of_hours') || {}).value,
      material: ($('of_material') || {}).value,
      file: ($('of_file') || {}).value,
      niche_id: ($('of_niche_id') || {}).value,
      price: qty ? sum / qty : sum,
      note: 'создано из заказа / печати',
    });
    if ($('of_nom_id')) $('of_nom_id').value = item.id;
    toggleWarehouseField();
    toast('Товар создан', item.name || '');
  } catch (e) { fail(e); }
}

async function createProductFromItemRow(row) {
  if (!row) return;
  const sel = row.querySelector('[data-item-nom]');
  if (sel && sel.value) return toast('В строке уже товар из базы', '', 'info');
  const qty = Math.max(1, num((row.querySelector('[data-item-qty]') || {}).value, 1));
  const sum = num((row.querySelector('[data-item-price]') || {}).value);
  try {
    const name = String((row.querySelector('[data-item-name]') || {}).value || '').trim();
    const item = await saveNomFromPrint({
      name,
      grams: (row.querySelector('[data-item-grams]') || {}).value,
      hours: (row.querySelector('[data-item-hours]') || {}).value,
      material: ($('of_material') || {}).value,
      file: ($('of_file') || {}).value,
      niche_id: ($('of_niche_id') || {}).value,
      price: qty ? sum / qty : sum,
      note: 'создано из состава заказа',
    });
    const current = collectOrderItems();
    const hit = current.find((r) => r.name === name && !r.nom_id) || current.find((r) => r.name === name);
    if (hit) hit.nom_id = item.id;
    renderOrderItems(current.length ? current : [{ nom_id: item.id, name: item.name, qty, price: sum }]);
    updateOrderItemsSummary();
    toast('Товар создан', item.name || '');
  } catch (e) { fail(e); }
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
function syncCompositionFields(active) {
  const override = Boolean($('of_items_override') && $('of_items_override').checked);
  const derivedIds = ['of_product', 'of_qty', 'of_price', 'of_grams', 'of_hours', 'of_cost'];
  derivedIds.forEach((id) => {
    const el = $(id);
    if (el) el.readOnly = active && !override;
  });
  const hint = $('of_items_hint');
  if (hint && active) hint.textContent = override
    ? 'Override включён: итоговые поля можно поправить вручную; состав остаётся сохранённым отдельно.'
    : 'Итоги состава вычисляются автоматически. Для ручной правки включите «Переопределить итоги состава».';
}
function updateOrderItemsSummary() {
  const items = collectOrderItems();
  const active = items.length > 0;
  syncCompositionFields(active);
  if (!active) return;
  const override = Boolean($('of_items_override') && $('of_items_override').checked);
  const total = items.reduce((a, i) => a + i.price * i.qty, 0);
  const units = items.reduce((a, i) => a + i.qty, 0);
  const totalGrams = items.reduce((a, i) => a + num(i.grams) * i.qty, 0);
  const totalHours = items.reduce((a, i) => a + num(i.hours) * i.qty, 0);
  if (!override) {
    $('of_price').value = String(Math.round(total * 100) / 100);
    $('of_qty').value = String(units);
    $('of_product').value = items.map((i) => `${i.name} ×${i.qty}`).join(', ');
    if (totalGrams > 0) $('of_grams').value = String(Math.round(totalGrams * 10) / 10);
    if (totalHours > 0) $('of_hours').value = String(Math.round(totalHours * 100) / 100);
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

/* ================================================= файл печати в заказе */
/** Записать результат чтения файла (upload/pull) в поля заказа. */
function applyFileEstimate(res, fallbackName) {
  const fileEl = $('of_file'), gramsEl = $('of_grams'), hoursEl = $('of_hours');
  const est = res.estimate || {};
  const name = res.file || res.saved || fallbackName || '';
  if (name && fileEl) fileEl.value = String(name).split('/').pop();
  const grams = num(res.grams) || num(est.total_grams) || num(est.grams);
  const minutes = num(res.minutes) || num(est.total_minutes) || num(est.minutes);
  if (grams && gramsEl) gramsEl.value = Math.round(grams * 10) / 10;
  if (minutes && hoursEl && !num(hoursEl.value)) hoursEl.value = Math.round(minutes / 60 * 100) / 100;
  const material = res.material || est.material || '';
  const color = res.color || est.color || '';
  if (material && $('of_material') && !$('of_material').value.trim()) $('of_material').value = material;
  if (color && $('of_color') && !$('of_color').value.trim()) $('of_color').value = color;
  distributeSpoolGrams(true);
  updateEconDebounced();
  // Возвращаем, удалось ли вытащить вес — вызывающий код покажет
  // понятное предупреждение, а не молчаливо «ничего не произошло».
  return { grams, minutes, material, color, name };
}

/** «Выбрать файл»: 3MF/G-code с компьютера → uploads, вес и время — из слайсера. */
async function pickOrderFile(file) {
  if (!file) return;
  if (!/\.(3mf|gcode)$/i.test(file.name)) return fail(new Error('Поддерживаются только 3MF и G-code'));
  const form = new FormData();
  form.append('file', file);
  if (editingOrder) form.append('order_id', editingOrder);
  toast('Читаем файл', file.name, 'info');
  try {
    const res = await api('/api/estimate/upload', { method: 'POST', body: form });
    const applied = applyFileEstimate(res, file.name);
    const bits = [res.file || file.name];
    if (num(applied.grams)) bits.push(`${nfmt(applied.grams)} г`);
    if (num(applied.minutes)) bits.push(U.minutesText ? U.minutesText(applied.minutes) : U.hoursText(num(res.hours)));
    if (num(applied.grams)) {
      toast('Файл сохранён в uploads', bits.join(' · '));
    } else {
      // Файл загрузился, но слайсер не оставил в нём вес — показываем, что
      // делать, вместо молчаливого «ничего не подставилось».
      toast('Файл сохранён, но вес не найден',
        'Впишите граммы вручную или скачайте файл с принтера кнопкой «Скачать с принтера»', 'warn');
    }
  } catch (e) { fail(e); }
}

/** «Скачать с принтера»: файл из поля заказа или выбор из списка SD-карты. */
async function pullOrderFile() {
  const live = PF.livePrinter();
  if (!live) return fail(new Error('Нет принтера на связи — настройте принтер в разделе «Принтеры»'));
  const file = ($('of_file') || {}).value.trim();
  if (!file) return openPullFilePicker(live);
  await pullFileFromPrinter(live, file);
}

async function pullFileFromPrinter(live, file) {
  const name = String(file || '').split('/').pop();
  toast('Скачиваем с принтера', `${live.name} · ${name}`, 'info');
  try {
    const res = await post('/api/estimate/pull', { printer_id: live.id, file: String(file || '') });
    const applied = applyFileEstimate(res, name);
    const title = res.source === 'printer' ? 'Файл скачан с принтера' : 'Использована копия из uploads';
    if (num(applied.grams)) {
      const bits = [res.file, `${nfmt(applied.grams)} г`];
      if (num(applied.minutes)) bits.push(U.minutesText ? U.minutesText(applied.minutes) : '');
      toast(title, bits.filter(Boolean).join(' · '));
    } else {
      toast(title, `${res.file || name} · вес в файле не найден — впишите граммы вручную`, 'warn');
    }
    return true;
  } catch (e) { fail(e); return false; }
}

/** Список файлов SD-карты — выбрать, какой скачать в uploads. */
async function openPullFilePicker(live) {
  const list = $('pull_file_list');
  const sub = $('pull_file_sub');
  if (sub) sub.textContent = `${live.name}: читаем список файлов SD-карты…`;
  list.innerHTML = '<div class="skeleton" style="height:60px"></div>';
  openModal('pull_file_modal');
  let files = [];
  try {
    const data = await get('/api/printer/files', { printer_id: live.id });
    files = (data.files || []).filter((f) => !f.dir && /\.(3mf|gcode)$/i.test(String(f.name || '')));
  } catch (e) {
    list.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
    if (sub) sub.textContent = 'Не удалось прочитать список файлов.';
    return;
  }
  if (sub) sub.textContent = `${live.name} · ${files.length} файл(ов). Выберите, какой скачать в uploads.`;
  if (!files.length) {
    list.innerHTML = '<div class="empty compact"><span>На SD-карте нет 3MF и G-code файлов. Загрузите файл с компьютера кнопкой «Выбрать файл».</span></div>';
    return;
  }
  const icon = (n) => /\.3mf$/i.test(n) ? '🧊' : '⚙️';
  const sizeText = (b) => num(b) >= 1024 * 1024 ? `${(num(b) / 1024 / 1024).toFixed(1)} МБ` : `${Math.round(num(b) / 1024)} КБ`;
  list.innerHTML = files.map((f) => {
    const name = String(f.name || '');
    return `<button class="file-pick" type="button" data-pull-file="${esc(f.path || f.name)}">`
      + `<span class="fic">${icon(name)}</span>`
      + `<span class="fname" title="${esc(f.path || f.name)}">${esc(name)}</span>`
      + (num(f.size) ? `<span class="fsize">${esc(sizeText(num(f.size)))}</span>` : '')
      + '<span class="pull">Скачать →</span></button>';
  }).join('');
}

/** «На диск»: сохранить копию файла из uploads на компьютер. */
async function downloadOrderFile() {
  const file = ($('of_file') || {}).value.trim();
  if (!file) return fail(new Error('Сначала выберите файл с компьютера или скачайте его с принтера'));
  const name = file.split('/').pop();
  toast('Готовим файл', name, 'info');
  try {
    const res = await fetch('/api/uploads?file=' + encodeURIComponent(name));
    if (!res.ok) throw new Error('Файл не найден в uploads — скачайте его с принтера или выберите с компьютера');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast('Файл сохранён на диск', name);
  } catch (e) { fail(e); }
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
  editingOrderUpdatedAt = id ? String(data.updated_at || '') : '';
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
  if ($('of_items_override')) $('of_items_override').checked = Boolean(num(data.items_override));
  renderItemsEcon(data.items_economics || []);
  updateOrderItemsSummary();
  $('of_reserved').checked = Boolean(num(data.reserved));
  if ($('of_gift')) $('of_gift').checked = Boolean(num(data.gift));
  const has85 = Boolean(id);
  ['order_brand_card', 'order_pack_card', 'order_thread'].forEach((bid) => {
    const b = $(bid);
    if (b) b.hidden = !has85;
  });
  updateReadyStockHint();
  toggleWarehouseField();
  $('of_auto_cost').value = String(num(data.auto_cost, 1) ? 1 : 0);
  // Оплата — только через единый журнал платежей. В карточке показываем
  // справочное значение, но оно не входит в payload сохранения заказа.
  const paidNow = Math.max(num(data.paid), num(data.prepaid));
  const paidField = $('of_prepaid');
  if (paidField) {
    paidField.value = paidNow ? String(paidNow) : '';
    paidField.readOnly = true;
  }
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
  const paymentBtn = $('order_payment');
  if (paymentBtn) paymentBtn.hidden = !id;
  $('order_queue').hidden = !id;
  $('order_queue').disabled = false;
  $('order_queue').textContent = 'Сохранить и подготовить';
  $('order_save_prepare').hidden = Boolean(id);
  $('order_fulfill').hidden = !id || data.status !== 'ready';
  $('order_to_warehouse').hidden = !id || data.status !== 'ready';
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
  if (id) loadOrderPhotosFull(id);            // ЗА6: полный список фото и файлов
  loadOrderTimelapse(id, (order && order.jobs) || []);   // №47: таймлапс печати
  renderOrderDefects((order && order.defects) || []);
  renderQcChecklist((order && order.qc_done) || '');
  renderOrderDocuments(null);
  renderCancelBanner(data);                   // ЗА3: плашка «покупатель просит отмену»
  loadTgThread(id ? data : null);             // ЗА4/ЗА5: диалог и быстрые действия
  updateEcon();
  openModal('order_modal');
  if (id) {
    loadOrderReadiness(id);
    loadOrderCompletion(id);
    loadOrderDocuments(id);
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

let stockDraft = null;
async function openOrderStock(orderId) {
  if (!orderId) return;
  try {
    const result = await get('/api/order/stock', { id: orderId });
    if (result.stocked) {
      toast('Заказ уже на складе');
      return;
    }
    if (!result.can_stock) {
      throw new Error((result.blocks || []).map((item) => item.text).join('; ') || 'Заказ нельзя положить на склад');
    }
    stockDraft = result;
    const items = result.items || [];
    const names = items.map((it) => `${esc(it.name || it.nom_id)} ×${nfmt(it.qty)}`).join('<br>');
    $('stock_summary').className = 'verdict ok';
    $('stock_summary').innerHTML = `<b>Заказ №${esc(result.number)}</b><br>`
      + `Готовое изделие: <br>${names || '—'}`
      + `<br>Всего ${nfmt(result.quantity)} шт`;
    $('stk_warehouse_id').innerHTML = (result.warehouses || []).map((w) =>
      `<option value="${esc(w.id)}">${esc(w.name)}</option>`).join('')
      || '<option value="">Не настроен склад</option>';
    $('stk_warehouse_id').value = result.warehouse_id || '';
    $('stk_note').value = '';
    openModal('stock_modal');
  } catch (e) { fail(e); }
}

async function confirmOrderStock() {
  if (!stockDraft) return;
  const button = $('stock_confirm');
  button.disabled = true;
  try {
    const result = await post('/api/order/stock-to-warehouse', {
      id: stockDraft.order_id,
      warehouse_id: $('stk_warehouse_id').value || '',
      note: $('stk_note').value || '',
    });
    closeModal('stock_modal');
    closeModal('order_modal');
    const doc = result.document || {};
    toast('Заказ на складе', `${doc.number || ''} · ${nfmt(result.quantity)} шт`.trim());
    stockDraft = null;
    await PF.refreshCore();
    PF.refreshFinance();
  } catch (e) { fail(e); }
  finally { button.disabled = false; }
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
    U.successFx();
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
      if (orderIsMulti() && !($('of_items_override') && $('of_items_override').checked)) {
        $('of_cost').value = String(cost);
      }
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
  if (editingOrderUpdatedAt) payload.expected_updated_at = editingOrderUpdatedAt;
  OF.forEach((k) => { const el = $('of_' + k); if (el) payload[k] = el.value; });
  if (payload.colors !== undefined) payload.colors = colorsToJson(payload.colors);
  distributeSpoolGrams();
  payload.spools = collectSpoolRows();
  payload.reserved = $('of_reserved').checked ? 1 : 0;
  if ($('of_gift')) payload.gift = $('of_gift').checked ? 1 : 0;
  payload.items = collectOrderItems();
  payload.items_override = $('of_items_override') && $('of_items_override').checked ? 1 : 0;
  if (!payload.product.trim()) {
    if (payload.items.length) payload.product = payload.items.map((i) => `${i.name} ×${i.qty}`).join(', ');
    else return fail(new Error('Укажите изделие или работу'));
  }
  if (payload.reserved && !payload.nom_id) return fail(new Error('Для резерва выберите готовый товар из базы'));
  ['qty', 'grams', 'hours', 'manual_minutes', 'price', 'cost'].forEach((k) => { payload[k] = num(payload[k]); });
  // paid/prepaid намеренно не отправляем: изменение оплаты выполняется
  // отдельной подтверждаемой операцией через /api/payment/save.
  payload.auto_cost = +$('of_auto_cost').value;
  const wasEditing = Boolean(editingOrder);
  try {
    const res = await post('/api/order/save', payload);
    editingOrder = res.order.id;
    editingOrderUpdatedAt = String(res.order.updated_at || '');
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

/* ======================================= Telegram-контур карточки (12.2)
   ЗА3 — баннер «покупатель просит отмену»; ЗА4 — нить диалога с полем
   ответа; ЗА5 — быстрые действия: подтвердить оплату и ответить на отзыв. */
let tgThread = null;
let tgReplyMode = 'reply';

function renderCancelBanner(order) {
  const wrap = $('of_cancel_wrap');
  if (!wrap) return;
  const stamp = String((order && order.cancel_requested_at) || '');
  wrap.hidden = !stamp;
  if (stamp) {
    const sub = $('of_cancel_sub');
    if (sub) sub.textContent = `Просьба от ${dateTimeText(stamp)} · бот ничего не отменял сам — решение за мастером`;
  }
}

async function resolveCancel(action) {
  if (!editingOrder) return;
  const label = action === 'canceled'
    ? 'Перевести заказ в статус отмены и снять отметку?'
    : 'Отметка «просит отмену» будет снята, заказ останется в работе.';
  if (action === 'canceled' && !confirmDanger(label)) return;
  try {
    const res = await post('/api/client-bot/cancel-ack', { order_id: editingOrder, action, actor: 'panel' });
    const fresh = (res && res.order) || {};
    if (action === 'canceled' && fresh.status === (PF.state.orders.find((o) => o.id === editingOrder) || {}).status) {
      toast('Отметка снята', 'Создайте финальный статус со словом «отмен» в «Статусы» — заказ переведётся в него автоматически', 'warn');
    } else {
      toast(action === 'keep' ? 'Оставил в работе' : 'Отмена проведена', `№${fresh.number || ''}`);
    }
    await PF.refreshCore();
    renderCancelBanner({});
    loadTgThread(fresh.channel === 'telegram' || fresh.client_source ? fresh : null);
  } catch (e) { fail(e); }
}

async function loadTgThread(order) {
  const wrap = $('of_tg_wrap');
  if (!wrap) return;
  tgThread = null;
  tgReplyMode = 'reply';
  wrap.hidden = true;
  if (!order || !editingOrder) return;
  const looksTg = order.channel === 'telegram' || order.client_source || order.cancel_requested_at;
  if (!looksTg) return;
  try {
    const d = await get('/api/client-bot/order-thread', { order_id: editingOrder });
    if (!d || !d.order || editingOrder !== d.order.id) return;   // карточку уже переоткрыли
    if (!d.chat_id) return;                                       // чат не привязан — показывать нечего
    tgThread = d;
    renderTgBlock(d);
  } catch (e) { /* коннектор не ответил — лучше скрыть блок, чем показать пустой */ }
}

function renderTgBlock(d) {
  const wrap = $('of_tg_wrap');
  wrap.hidden = false;
  const chat = d.chat || {};
  const src = TG_SOURCES[d.order.client_source] || '';
  const banned = Number(chat.banned) ? ' · чат заблокирован' : '';
  $('of_tg_sub').textContent = [chat.name || chat.chat_id, src].filter(Boolean).join(' · ') + banned;
  const msgs = (d.messages || []).filter((m) => m.direction === 'in' || m.direction === 'out');
  $('of_tg_msgs').innerHTML = msgs.length ? msgs.map((m) => {
    const incoming = m.direction === 'in';
    const who = incoming ? (chat.name || 'Покупатель') : (m.operator ? `Мастер · ${m.operator}` : 'Мастер');
    const body = incoming ? (m.text || '') : (m.answer || m.text || '');
    return `<div class="tg-msg ${incoming ? 'in' : 'out'}"><small>${esc(who)} · ${esc(dateTimeText(m.at))}</small>`
      + `<span>${esc(String(body).slice(0, 420))}</span></div>`;
  }).join('') : '<div class="empty compact"><span>Сообщений нет — напишите первым, бот доставит в Telegram.</span></div>';
  const chips = [];
  if (d.payment_intent) {
    chips.push(`<button class="btn sm ok" type="button" data-tg-pay="${esc(d.payment_intent.id)}" `
      + `title="Записать оплату ${money(d.payment_intent.amount)} в журнал и подтвердить покупателю в чате">`
      + `<i data-icon="check">✓</i> Подтвердить оплату ${money(d.payment_intent.amount)}</button>`);
  }
  if (d.review) {
    chips.push('<button class="btn sm" type="button" data-tg-review="1" '
      + `title="${esc(d.review.rating === 'bad' ? 'Недовольный отзыв' : 'Покупатель оценил работу')}${d.review.comment ? ' — ' + esc(String(d.review.comment).slice(0, 120)) : ''}">`
      + `<i data-icon="star">★</i> Ответить на отзыв</button>`);
  }
  chips.push(`<span class="chip outline" title="Внутренний номер чата · в рабочем боте: «кответ ${esc(String(chat.chat_id || ''))} текст»">✈ ${esc(chat.username ? '@' + chat.username : 'tg:' + chat.chat_id)}</span>`);
  const chipsHost = $('of_tg_chips');
  chipsHost.innerHTML = chips.join(' ');
  const sel = $('of_tg_template');
  sel.innerHTML = '<option value="">Вставить шаблон…</option>' + (d.templates || [])
    .map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');
  sel.disabled = !(d.templates || []).length;
  $('of_tg_summary').hidden = true;             // №61: резюме перезапрашивается по клику
  if (window.PFIcons) window.PFIcons.apply(wrap);
}

/* №61: резюме диалога — локально-экстрактивный разбор на сервере */
async function loadThreadSummary() {
  if (!editingOrder) return;
  const box = $('of_tg_summary');
  box.hidden = false;
  box.className = 'verdict';
  box.innerHTML = '<span class="muted">Собираю резюме переписки…</span>';
  try {
    const d = await get('/api/client-bot/thread-summary', { order_id: editingOrder });
    if (!d) throw new Error('Пустой ответ');
    if (d.empty) {
      box.className = 'verdict warn';
      box.textContent = d.verdict || 'Переписки по заказу ещё нет';
      return;
    }
    const parts = [`<b>${esc(d.verdict || '')}</b>`];
    (d.open_questions || []).forEach((q) => {
      parts.push(`<div style="margin-top:6px">❓ Без ответа: ${esc(q.text)}<br><small class="muted">${esc(dateTimeText(q.at))}</small></div>`);
    });
    if ((d.amounts || []).length) parts.push(`<div>₽ Суммы: ${esc(d.amounts.join(', '))}</div>`);
    if ((d.deadlines || []).length) parts.push(`<div>📅 Сроки: ${esc(d.deadlines.join(', '))}</div>`);
    if ((d.phones || []).length) parts.push(`<div>☎ Телефон: ${esc(d.phones[0])}</div>`);
    if ((d.highlights || []).length) {
      const last = d.highlights[d.highlights.length - 1];
      parts.push(`<div style="margin-top:6px">Последнее по делу: «${esc(last.text)}»<br><small class="muted">${esc(dateTimeText(last.at))}</small></div>`);
    }
    parts.push(`<small class="muted" style="display:block;margin-top:6px">${esc(d.counts.in)} сообщений от покупателя, ${d.counts.out} ваших · резюме собрано локально, без внешних сервисов</small>`);
    box.className = `verdict ${d.last_direction === 'in' || (d.open_questions || []).length ? 'warn' : 'ok'}`;
    box.innerHTML = parts.join('');
  } catch (e) {
    box.className = 'verdict bad';
    box.textContent = 'Резюме не собралось: ' + e.message;
  }
}

function tgSetMode(mode) {
  tgReplyMode = mode;
  const box = $('of_tg_reply');
  const btn = $('of_tg_send');
  if (!box || !btn) return;
  if (mode === 'review') {
    box.placeholder = 'Ответ покупателю на отзыв — уйдёт в чат и пометит отзыв отвеченным';
    btn.textContent = 'Ответить на отзыв';
  } else {
    box.placeholder = 'Ответ покупателю — уйдёт в Telegram через клиентского бота';
    btn.textContent = 'Отправить в чат';
  }
  box.focus();
}

async function sendTgReply() {
  if (!tgThread || !tgThread.chat_id || !editingOrder) return;
  const box = $('of_tg_reply');
  const value = String(box.value || '').trim();
  if (!value) return fail(new Error('Введите текст ответа'));
  const btn = $('of_tg_send');
  btn.disabled = true;
  try {
    if (tgReplyMode === 'review') {
      await post('/api/client-bot/review/reply', { chat_id: tgThread.chat_id, order_id: editingOrder, text: value });
      toast('Ответ на отзыв отправлен', 'Отзыв помечен отвеченным');
    } else {
      const rid = (window.crypto && crypto.randomUUID) ? `panel-order-${crypto.randomUUID()}` : `panel-order-${Date.now()}`;
      await post('/api/client-bot/reply', { chat_id: tgThread.chat_id, text: value, request_id: rid });
      toast('Ответ отправлен', 'Клиентский бот доставит его в чат');
    }
    box.value = '';
    tgSetMode('reply');
    const d = await get('/api/client-bot/order-thread', { order_id: editingOrder });
    tgThread = d;
    renderTgBlock(d);
  } catch (e) { fail(e); } finally { btn.disabled = false; }
}

async function confirmTgPayment(intentId) {
  if (!confirmDanger('Записать оплату и подтвердить её покупателю в чате?')) return;
  try {
    await post('/api/client-bot/payment', { intent_id: intentId, action: 'confirm', actor: 'panel' });
    U.successFx();
    toast('Оплата подтверждена', 'Проводка в журнале · покупателю ушло подтверждение');
    await PF.refreshCore();
    if (editingOrder) {
      const fresh = await get('/api/order', { id: editingOrder });
      const paidField = $('of_prepaid');
      if (paidField) paidField.value = String(Math.max(num(fresh.paid), num(fresh.prepaid)) || '');
      if (fresh.channel === 'telegram' || fresh.client_source) loadTgThread(fresh);
    }
  } catch (e) { fail(e); }
}

/* ====================================================== фото к заказу */
/* ЗА6: снимки производства и файлы заявки живут отдельно. Изображения —
   сеткой с увеличением (О2), документы покупателя — карточками со скачиванием
   и кнопкой «В печать» (перекладываем в uploads и подставляем в поле файла). */
const ORDER_FILE_RE = /\.(stl|3mf|gcode|obj|step|stp|zip|rar|7z|pdf|dwg|txt)$/i;
const orderFileOriginal = (ph) => {
  const m = /^client_.+?_(\d{13})_(.+)$/.exec(String(ph.file || ''));
  return m ? m[2] : String(ph.file || 'файл');
};
const orderFileSize = (n) => n == null ? '' : (num(n) > 1048576
  ? `${nfmt(num(n) / 1048576, 1)} МБ` : `${Math.max(1, Math.round(num(n) / 1024))} КБ`);

function renderOrderPhotos(photos) {
  const wrap = $('of_photos_wrap');
  if (!wrap) return;
  wrap.hidden = !photos.length;
  const isFile = (ph) => ph.kind === 'client_file' || ORDER_FILE_RE.test(orderFileOriginal(ph));
  const fromClient = (ph) => String(ph.kind || '').startsWith('client');
  const imgs = photos.filter((ph) => !isFile(ph));
  const files = photos.filter(isFile);
  const host = $('of_photos');
  host.innerHTML = imgs.length ? imgs.map((ph) => {
    const url = `/api/order/photo.jpg?photo_id=${encodeURIComponent(ph.id)}`;
    const cap = [ph.note, fromClient(ph) ? 'от покупателя' : '', ph.at ? dateText(String(ph.at).slice(0, 10)) : '']
      .filter(Boolean).join(' · ');
    return `<div class="ophoto${fromClient(ph) ? ' client' : ''}">`
      + `<button class="ophoto-zoom" type="button" data-photo-zoom="${esc(ph.id)}" data-src="${esc(url)}" data-cap="${esc(cap)}" title="Клик — крупно"><img src="${esc(url)}" alt="" loading="lazy"></button>`
      + `<small>${esc(cap)}</small>`
      + `<button class="icon-btn sm" type="button" data-photo-del="${esc(ph.id)}" title="Удалить">×</button></div>`;
  }).join('') : (files.length ? '' : '<div class="empty compact"><span>Снимков пока нет.</span></div>');
  const filesHost = $('of_photo_files');
  if (filesHost) {
    filesHost.innerHTML = files.length ? `<div class="ofile-group"><b class="ofile-cap"><i data-icon="cube">▣</i> Материалы заявки</b>`
      + files.map((ph) => {
        const name = orderFileOriginal(ph);
        const printable = /\.(3mf|gcode)$/i.test(name);
        return `<div class="ofile">`
          + `<span class="ofic"><i data-icon="cube">▣</i></span>`
          + `<div class="ofm"><b title="${esc(name)}">${esc(name)}</b>`
          + `<small>${[orderFileSize(ph.size), fromClient(ph) ? 'прислал покупатель' : 'файл'].filter(Boolean).join(' · ')}</small></div>`
          + `<a class="btn sm ghost" href="/api/order/photo.jpg?photo_id=${encodeURIComponent(ph.id)}" download="${esc(name)}" title="Скачать на компьютер">↓ Скачать</a>`
          + (printable ? `<button class="btn sm" type="button" data-photo-use="${esc(ph.id)}" title="Положить в uploads и подставить файлом печати этого заказа">▣ В печать</button>` : '')
          + `<button class="icon-btn sm" type="button" data-photo-del="${esc(ph.id)}" title="Удалить">×</button></div>`;
      }).join('') + '</div>' : '';
    if (window.PFIcons) window.PFIcons.apply(filesHost);
  }
}


/* ---------------------- №47: таймлапс заказа ----------------------
   Кейфреймы снимает коннектор во время печати (keyframe_interval_min,
   PHOTO_DIR/keyframes/<job_id>). Здесь — проигрыватель в карточке заказа:
   /api/job/keyframes отдаёт список кадров, /api/job/keyframe.jpg — сам кадр. */
const tl = { timer: 0, pos: 0, frames: [], jobId: '', speedMs: 400 };

function tlStop() {
  if (tl.timer) { clearInterval(tl.timer); tl.timer = 0; }
  const btn = $('tl_play');
  if (btn) btn.textContent = '▶';
}

function tlShow(pos) {
  if (!tl.frames.length) return;
  tl.pos = clamp(pos, 0, tl.frames.length - 1);
  const name = tl.frames[tl.pos];
  $('tl_img').src = `/api/job/keyframe.jpg?id=${encodeURIComponent(tl.jobId)}&name=${encodeURIComponent(name)}`;
  $('tl_range').value = String(tl.pos);
  $('tl_counter').textContent = `${tl.pos + 1}/${tl.frames.length}`;
}

function tlPlayPause() {
  if (tl.timer) { tlStop(); return; }
  if (!tl.frames.length) return;
  if (tl.pos >= tl.frames.length - 1) tl.pos = 0;
  $('tl_play').textContent = '⏸';
  tl.timer = setInterval(() => {
    if (tl.pos >= tl.frames.length - 1) { tlStop(); return; }
    tlShow(tl.pos + 1);
  }, tl.speedMs);
}

function tlSelectJob(jobId, jobName, frames) {
  tlStop();
  tl.jobId = jobId;
  tl.frames = frames || [];
  tl.pos = 0;
  $('tl_range').max = String(Math.max(0, tl.frames.length - 1));
  $('tl_chip').hidden = false;
  $('tl_chip').textContent = `${tl.frames.length} кадров`;
  $('tl_hint').textContent = frames && frames.length
    ? `Задание «${jobName}» · кадр раз в ${(tl.speedMs / 1000).toFixed(1)} с. Интервал съёмки настраивается: Настройки → Печать → «Кейфрейм-интервал».`
    : '';
  if (tl.frames.length) tlShow(0);
}

async function loadOrderTimelapse(orderId, jobs) {
  const wrap = $('tl_wrap');
  if (!wrap) return;
  tlStop();
  wrap.hidden = true;
  if (!orderId) return;
  const withIds = (jobs || []).filter((j) => j && j.id);
  if (!withIds.length) return;
  let found = [];
  await Promise.all(withIds.map(async (j) => {
    try {
      const d = await get(`/api/job/keyframes?id=${encodeURIComponent(j.id)}`);
      if (d && d.frames && d.frames.length) found.push({ id: j.id, name: j.name || j.file || j.id, frames: d.frames });
    } catch (e) { /* задание без кейфреймов — просто пропускаем */ }
  }));
  found.sort((a, b) => b.frames.length - a.frames.length);
  const picker = $('tl_jobs');
  picker.innerHTML = '';
  if (!found.length) return;
  wrap.hidden = false;
  if (found.length > 1) {
    picker.hidden = false;
    picker.innerHTML = found.map((j, i) =>
      `<button class="btn sm ${i ? 'ghost' : ''}" type="button" data-tl-job="${esc(j.id)}">${esc(j.name)} · ${j.frames.length}</button>`).join('');
    picker.querySelectorAll('[data-tl-job]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const j = found.find((x) => x.id === btn.getAttribute('data-tl-job'));
        if (!j) return;
        picker.querySelectorAll('[data-tl-job]').forEach((b) => b.classList.add('ghost'));
        btn.classList.remove('ghost');
        tlSelectJob(j.id, j.name, j.frames);
      });
    });
  } else {
    picker.hidden = true;
  }
  tlSelectJob(found[0].id, found[0].name, found[0].frames);
}

function bindTimelapseControls() {
  $('tl_play').addEventListener('click', tlPlayPause);
  $('tl_range').addEventListener('input', () => { tlStop(); tlShow(num($('tl_range').value)); });
  $('tl_img').addEventListener('error', () => {
    if (!$('tl_wrap').hidden) $('tl_hint').textContent = 'Кадр не загрузился — возможно, файл удалён архиватором.';
  });
  const modal = $('order_modal');
  if (modal) modal.addEventListener('close', tlStop);   // карточка закрыта — плеер молчит
}

async function loadOrderPhotosFull(orderId) {
  try {
    const d = await get('/api/order/photos', { order_id: orderId });
    if (editingOrder === orderId) renderOrderPhotos(d.photos || []);
  } catch (e) { /* короткая версия уже на экране */ }
}
async function addOrderPhoto(orderId, dataUrl, kind, note) {
  try {
    await post('/api/order/photo', { order_id: orderId, data: dataUrl, kind, note });
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
    const zoom = e.target.closest('[data-photo-zoom]');
    if (zoom) { U.lightbox(zoom.dataset.src, zoom.dataset.cap || ''); return; }
    const btn = e.target.closest('[data-photo-del]');
    if (!btn) return;
    await post('/api/order/photo/delete', { id: btn.dataset.photoDel });
    refreshOrderDetail();
  });
  const filesHost = $('of_photo_files');
  if (filesHost) filesHost.addEventListener('click', async (e) => {
    const use = e.target.closest('[data-photo-use]');
    if (use) {
      try {
        const r = await post('/api/order/photo/to-uploads', { id: use.dataset.photoUse });
        const field = $('of_file');
        if (field) field.value = r.file || '';
        toast('Файл подставлен в заказ', `${r.file || ''} — проверьте граммы и часы`);
      } catch (err) { fail(err); }
      return;
    }
    const del = e.target.closest('[data-photo-del]');
    if (del) {
      await post('/api/order/photo/delete', { id: del.dataset.photoDel });
      refreshOrderDetail();
    }
  });
}
async function refreshOrderDetail() {
  if (!editingOrder) return;
  try {
    const order = await get('/api/order', { id: editingOrder });
    renderOrderPhotos(order.photos || []);
    loadOrderPhotosFull(editingOrder);
    renderOrderDefects(order.defects || []);
  } catch (e) { /* не критично */ }
}

/* ====================================================== документы заказа */
function docFoldEnabled() {
  const box = $('order_doc_fold');
  return box ? box.checked : true; // по умолчанию мелкие товары сворачиваем
}

function openOrderPrint(kind) {
  if (!editingOrder) return fail(new Error('Сначала сохраните заказ'));
  const k = String(kind || 'waybill').trim() || 'waybill';
  window.open('/api/b2b/doc?id=' + encodeURIComponent(editingOrder) + '&kind=' + encodeURIComponent(k)
    + '&group=' + (docFoldEnabled() ? '1' : '0'), '_blank');
}

function renderOrderDocuments(payload) {
  const wrap = $('of_docs_wrap');
  const host = $('of_docs_list');
  if (!wrap || !host) return;
  wrap.hidden = !editingOrder;
  if (!editingOrder) {
    host.innerHTML = '';
    return;
  }
  if (!payload) {
    host.innerHTML = '<div class="empty compact"><span>Загружаем документы…</span></div>';
    return;
  }
  const foldInfo = payload.fold || {};
  const foldHint = $('of_docs_fold_hint');
  if (foldHint) {
    const before = Number(foldInfo.before) || 0;
    const after = Number(foldInfo.after) || 0;
    foldHint.hidden = !foldInfo.enabled;
    if (foldInfo.enabled) {
      foldHint.textContent = `В документе ${before} поз. → ${after} после свёртки`
        + (foldInfo.groups && foldInfo.groups.length ? ` (группы: ${foldInfo.groups.join(', ')})` : '');
    }
  }
  const docs = payload.documents || [];
  if (!docs.length) {
    host.innerHTML = payload.can_create_waybill
      ? '<div class="empty compact"><span>Складских документов ещё нет. Можно создать расходную накладную черновиком.</span></div>'
      : '<div class="empty compact"><span>Накладную со склада можно собрать, когда в заказе есть товар из базы.</span></div>';
    return;
  }
  host.innerHTML = docs.map((d) => {
    const posted = d.state === 'posted';
    return `<div class="tx-row">`
      + `<span class="tx-ic ${posted ? 'income' : 'expense'}">${posted ? '✓' : '•'}</span>`
      + `<div class="tx-body"><b>${esc(d.kind_label || d.kind || 'Документ')} ${esc(d.number || '')}</b>`
      + `<small>${esc(dateTimeText(d.at))}${d.warehouse_name ? ' · ' + esc(d.warehouse_name) : ''}`
      + `${d.note ? ' · ' + esc(d.note) : ''}</small></div>`
      + `<span class="chip ${posted ? 'ok' : 'warn'}">${posted ? 'Проведён' : 'Черновик'}</span>`
      + `<button class="btn sm" type="button" data-order-doc="${esc(d.id)}">Открыть</button></div>`;
  }).join('');
}

async function loadOrderDocuments(orderId) {
  if (!orderId) return null;
  const wrap = $('of_docs_wrap');
  if (wrap) wrap.hidden = false;
  try {
    const res = await get('/api/order/documents', { id: orderId });
    if (editingOrder !== orderId) return res;
    renderOrderDocuments(res);
    return res;
  } catch (e) {
    const host = $('of_docs_list');
    if (host && editingOrder === orderId) {
      host.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message || String(e))}</span></div>`;
    }
    return null;
  }
}

async function createOrderWaybill() {
  if (!editingOrder) return fail(new Error('Сначала сохраните заказ'));
  try {
    const res = await post('/api/order/waybill', {
      id: editingOrder,
      warehouse_id: ($('of_warehouse_id') || {}).value || '',
    });
    const doc = res.document || {};
    toast(res.existing ? 'Накладная уже есть' : 'Накладная создана',
      `${doc.number || ''} · черновик, остатки не менялись`.trim());
    await loadOrderDocuments(editingOrder);
    if (PF.modules.products && PF.modules.products.openDoc && doc.id) {
      PF.modules.products.openDoc(doc.id);
    }
  } catch (e) { fail(e); }
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
function customerHasContact(customer) {
  return Boolean(String(customer.phone || '').trim() || String(customer.messenger || '').trim());
}
function matchesCustomerSegment(customer) {
  if (customerSegment === 'repeat') return num(customer.orders) > 1;
  if (customerSegment === 'new') return num(customer.orders) <= 1;
  if (customerSegment === 'no-contact') return !customerHasContact(customer);
  return true;
}
function renderCustomerInsights(customers, repeat, withoutContact) {
  const host = $('customers_insight');
  if (!host) return;
  const revenue = customers.reduce((sum, customer) => sum + num(customer.revenue), 0);
  const repeatRevenue = customers.filter((customer) => num(customer.orders) > 1)
    .reduce((sum, customer) => sum + num(customer.revenue), 0);
  const shown = customers.filter(matchesCustomerSegment).length;
  host.innerHTML = `<article class="more-insight"><span>Клиентов в базе</span><b>${nfmt(customers.length)}</b><small>${shown === customers.length ? 'вся база' : `в выбранном сегменте ${nfmt(shown)}`}</small></article>`
    + `<article class="more-insight ok"><span>Повторных покупателей</span><b>${nfmt(repeat)}</b><small>${customers.length ? `${nfmt(repeat / customers.length * 100)}% от базы` : 'появятся после второго заказа'}</small></article>`
    + `<article class="more-insight ${withoutContact ? 'warn' : ''}"><span>Выручка по базе</span><b>${money(revenue)}</b><small>${withoutContact ? `без контакта: ${nfmt(withoutContact)} · повторные дали ${money(repeatRevenue)}` : `повторные дали ${money(repeatRevenue)}`}</small></article>`;
}
/* В35: схематичная карта клиентов — зоны по адресам, толщина = выручка.
   Без внешних карт: сетка плиток «зона → клиентов и сумма», как схема города. */
function zoneKeyOf(address) {
  const raw = String(address || '').trim();
  if (!raw) return '';
  return raw.split(/[,;]/)[0].replace(/\s*\d+[\w/\-]*\s*$/, '').trim().slice(0, 42);
}

function ensureCustomerMapHost() {
  const table = document.querySelector('#view-customers .customers-table');
  if (!table) return null;
  let card = document.getElementById('cust_map_card');
  if (!card) {
    card = document.createElement('div');
    card.className = 'card';
    card.id = 'cust_map_card';
    card.style.marginTop = '16px';
    card.innerHTML = '<div class="card-head"><div><h2>Карта клиентов (В35)</h2>'
      + '<p>Схема по адресам из профилей: чем толще полоса — тем больше выручки приносит зона</p></div></div>'
      + '<div class="cust-map" id="cust_map"></div>';
    table.parentElement.insertBefore(card, table);
  }
  return card.querySelector('#cust_map');
}

function renderCustomerMap() {
  const host = ensureCustomerMapHost();
  if (!host) return;
  const finals = PF.finalStatusIds ? PF.finalStatusIds() : [];
  const revenue = new Map();
  (PF.state.orders || []).forEach((o) => {
    if (!finals.includes(o.status)) return;
    const key = o.customer_id || o.customer_name || '';
    revenue.set(key, (revenue.get(key) || 0) + num(o.price));
  });
  const zones = new Map();
  (PF.state.customers || []).forEach((c) => {
    const zone = zoneKeyOf(c.address);
    if (!zone) return;
    const sum = revenue.get(c.id) || revenue.get(c.name) || 0;
    const entry = zones.get(zone) || { count: 0, sum: 0 };
    entry.count += 1;
    entry.sum += sum;
    zones.set(zone, entry);
  });
  if (!zones.size) {
    host.innerHTML = '<div class="empty compact"><span>◌</span><b>Адресов пока нет</b>'
      + '<span>Заполните адрес клиента в карточке — зона появится на схеме.</span></div>';
    return;
  }
  const max = Math.max(1, ...[...zones.values()].map((z) => z.sum));
  host.innerHTML = [...zones.entries()]
    .sort((a, b) => b[1].sum - a[1].sum)
    .map(([zone, z]) => `<div class="cust-zone" style="--heat:${(z.sum / max).toFixed(2)}" title="Клиентов: ${z.count} · закрытых заказов на ${money(z.sum)}">`
      + `<div style="display:flex;align-items:center;gap:6px"><b>${esc(zone)}</b><span class="cz-sum">${money(z.sum)}</span></div>`
      + `<small>${nfmt(z.count)} ${z.count === 1 ? 'клиент' : (z.count < 5 ? 'клиента' : 'клиентов')}</small></div>`)
    .join('');
}

function renderCustomers() {
  const q = ($('customers_search').value || '').trim().toLowerCase();
  const customers = PF.state.customers || [];
  const list = customers.filter((customer) => matchesCustomerSegment(customer) && (!q ||
    [customer.name, customer.phone, customer.messenger, customer.company]
      .some((value) => String(value || '').toLowerCase().includes(q))));
  const repeat = customers.filter((customer) => num(customer.orders) > 1).length;
  const withoutContact = customers.filter((customer) => !customerHasContact(customer)).length;
  const head = $('customers_kpi');
  if (head) {
    head.innerHTML = `<span class="chip">База <b>&nbsp;${nfmt(customers.length)}</b></span>`
      + `<span class="chip ok">Повторных <b>&nbsp;${nfmt(repeat)}</b></span>`;
  }
  renderCustomerInsights(customers, repeat, withoutContact);
  $('customers_tbody').innerHTML = list.length ? list.map((customer) => {
    const segment = num(customer.orders) > 2 ? ['ok', 'Постоянный']
      : num(customer.orders) > 1 ? ['accent', 'Повторный'] : ['outline', 'Новый'];
    const contact = customerHasContact(customer);
    return `<tr><td><div class="cell-user"><span class="avatar" style="--av:${esc(U.avColor(customer.name))}">${esc(initials(customer.name))}</span>`
      + `<span><b>${esc(customer.name || 'Без имени')}</b>${customer.company ? `<small>${esc(customer.company)}</small>` : ''}</span></div></td>`
      + `<td>${esc(customer.phone || '—')}${customer.messenger ? `<br><small class="muted">${esc(customer.messenger)}</small>` : ''}${!contact ? '<br><small class="neg">нет контакта</small>' : ''}</td>`
      + `<td class="right tnum">${nfmt(customer.orders)}</td>`
      + `<td class="right tnum">${money(customer.revenue)}</td>`
      + `<td>${customer.last_order ? esc(dateText(customer.last_order)) : '—'}</td>`
      + `<td><span class="chip ${segment[0]}">${segment[1]}</span></td>`
      + `<td><button class="btn xs" type="button" data-cust-my="${esc(customer.id)}" title="Страница «Мой NOZZA» по коду">🔑 Мой NOZZA</button> `
      + `<button class="btn xs" type="button" data-cust-wish="${esc(customer.id)}" title="Wish-list: хочу, когда будет">💌 Пожелания</button></td></tr>`;
  }).join('') : `<tr><td colspan="7">${customers.length
    ? '<div class="empty compact"><span>В этом сегменте никого не найдено.</span></div>'
    : '<div class="empty"><span class="big">◎</span><b>Клиентов нет</b><span>Появятся после первого заказа.</span>'
      + '<button class="btn sm primary" type="button" data-empty-click="orders_new">+ Новый заказ</button></div>'}</td></tr>`;
  renderCustomerMap();
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
function renderNicheSummary(niches) {
  const host = $('niche_summary');
  if (!host) return;
  const active = niches.filter((niche) => num(niche.active, 1));
  const pool = active.length ? active : niches;
  const orders = pool.reduce((sum, niche) => sum + num(niche.orders), 0);
  const profit = pool.reduce((sum, niche) => sum + num(niche.profit), 0);
  const leads = pool.reduce((sum, niche) => sum + num(niche.leads), 0);
  const views = pool.reduce((sum, niche) => sum + num(niche.views), 0);
  const leader = pool.slice().sort((left, right) => num(right.profit) - num(left.profit))[0];
  host.innerHTML = `<article class="niche-overview"><span>Сейчас тестируется</span><b>${nfmt(active.length || niches.length)} ${active.length === 1 ? 'направление' : active.length < 5 ? 'направления' : 'направлений'}</b><small>${leader && num(leader.profit) > 0 ? `Лидер по прибыли: ${esc(leader.name)}. Сравнивайте его с остальными до расширения ассортимента.` : 'Добавьте показы и обращения — после этого система покажет, где предложение теряет людей.'}</small></article>`
    + `<article class="niche-summary-stat"><span>Заказов</span><b>${nfmt(orders)}</b><small>из ${nfmt(leads)} обращений</small></article>`
    + `<article class="niche-summary-stat"><span>Конверсия</span><b>${leads ? nfmt(orders / leads * 100) + '%' : '—'}</b><small>${views ? `обращений от показов: ${nfmt(leads / views * 100)}%` : 'нужны показы'}</small></article>`
    + `<article class="niche-summary-stat"><span>Прибыль</span><b class="${profit >= 0 ? 'pos' : 'neg'}">${money(profit)}</b><small>${pool.length ? `по ${nfmt(pool.length)} гипотезам` : 'пока нет данных'}</small></article>`;
}
function renderNiches() {
  const host = $('niche_grid');
  const niches = PF.state.niches || [];
  renderNicheSummary(niches);
  host.innerHTML = niches.length ? niches.map((niche) => {
    const [kind, verdict] = nicheVerdict(niche);
    const leadRate = num(niche.views) ? num(niche.leads) / num(niche.views) * 100 : 0;
    const orderRate = num(niche.leads) ? num(niche.orders) / num(niche.leads) * 100 : 0;
    return `<article class="niche-card" data-niche="${esc(niche.id)}">`
      + `<div class="nhead"><span class="nic" style="background:${esc(niche.color)}22;color:${esc(niche.color)}">${esc(niche.icon || '◆')}</span>`
      + `<div style="flex:1"><h3>${esc(niche.name)}</h3><small class="muted">${esc(niche.hypothesis || 'Гипотеза не описана')}</small></div>`
      + `<button class="icon-btn sm" type="button" data-niche-edit="${esc(niche.id)}" title="Настроить нишу">✎</button></div>`
      + '<div class="funnel">'
      + `<div><span>Показы</span><b>${nfmt(niche.views)}</b></div>`
      + `<div><span>Обращения</span><b>${nfmt(niche.leads)}</b></div>`
      + `<div><span>Заказы</span><b>${nfmt(niche.orders)}</b></div>`
      + `<div><span>Повторных</span><b>${nfmt(niche.repeat_buyers)}</b></div>`
      + '</div>'
      + `<div class="niche-conversion"><span>в обращение <b>${niche.views ? nfmt(leadRate) + '%' : '—'}</b></span><span>в заказ <b>${niche.leads ? nfmt(orderRate) + '%' : '—'}</b></span></div>`
      + '<div class="res-row"><span class="lbl">Выручка</span><span class="val">' + money(niche.revenue) + '</span></div>'
      + '<div class="res-row"><span class="lbl">Прибыль</span><span class="val ' + (num(niche.profit) >= 0 ? 'pos' : 'neg') + '">' + money(niche.profit) + '</span></div>'
      + '<div class="res-row"><span class="lbl">Часы печати</span><span class="val">' + hoursText(niche.hours) + '</span></div>'
      + '<div class="res-row"><span class="lbl">Прибыль за час</span><span class="val">' + (num(niche.hours) ? money(niche.profit_per_hour) : '—') + '</span></div>'
      + `<div class="verdict ${kind}" style="margin-top:11px">${esc(verdict)}</div>`
      + `<div class="niche-card-foot"><span class="niche-state">${num(niche.active, 1) ? 'Гипотеза активна' : 'На паузе'}</span><a href="#marketing" data-view="marketing">Сделать контент →</a></div>`
      + '</article>';
  }).join('') : '<div class="empty"><span class="big">◫</span><b>Ниш пока нет</b><span>Добавьте гипотезу, чтобы сравнивать направления по фактической прибыли.</span>'
    + '<button class="btn sm primary" type="button" data-empty-click="niche_add">+ Ниша</button></div>';
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

/* ============================================== 8.5: действия заказа */
function openPackCard() {
  if (!editingOrder) return;
  window.open('/api/order/pack?id=' + encodeURIComponent(editingOrder), '_blank');
}
async function orderBrandCard() {
  if (!editingOrder) return;
  try {
    const r = await post('/api/order/brand-card', { order_id: editingOrder });
    toast('Бренд-карточка в очереди', r.job && r.job.name ? r.job.name : '');
    await PF.refreshCore();
  } catch (e) { fail(e); }
}
async function openOrderThread() {
  if (!editingOrder) return;
  const body = $('thread_body');
  body.innerHTML = '<span class="muted">Загружаем…</span>';
  openModal('thread_modal');
  try {
    const d = await get('/api/order/thread', { id: editingOrder });
    const o = d.order || {};
    const row = (icon, title, detail, tone) =>
      `<div class="mini-row"><span class="dot ${tone || ''}"></span>`
      + `<div class="mbody"><b>${esc(title)}</b><small>${detail}</small></div></div>`;
    let html = row('①', `Заказ №${esc(o.number || '')} · ${esc(o.product || '')}`,
      `${esc(o.customer_name || 'без клиента')} · ${esc(dateTimeText(o.created_at))} · ${money(o.price)}${o.gift ? ' · подарочный' : ''}`, 'on');
    if ((d.print || []).length) {
      (d.print || []).forEach((j) => {
        html += row('②', `Печать: ${esc(j.name || j.id)}`,
          `${esc(j.state)}${j.grams ? ` · ${nfmt(j.grams)} г` : ''}${j.duration_min ? ` · ${minutesText(j.duration_min)}` : ''}`,
          j.state === 'done' ? 'on' : '');
      });
    } else {
      html += row('②', 'Печать ещё не запускалась', 'задание появится после постановки в очередь');
    }
    if (d.shelf && d.shelf.item_id) {
      const last = (d.shelf.recent_sales || [])[0];
      html += row('③', `Полка: ${esc(d.shelf.name)}`,
        `остаток ${nfmt(d.shelf.qty)} шт${last ? ` · последняя продажа ${esc(dateTimeText(last.at))}` : ''}`, 'on');
    } else {
      html += row('③', 'На полке нет позиции с таким именем', 'связка появится, когда изделие совпадёт с карточкой полки');
    }
    if (d.income && d.income.length) {
      const last = d.income[d.income.length - 1];
      html += row('④', `Продажа / оплата: ${money(last.amount)}`, `${d.income.length} проводок · последняя ${esc(dateTimeText(last.at))}`, 'on');
    } else {
      html += row('④', 'Оплаты ещё нет', 'проводка появится после платежа или продажи с полки');
    }
    if (d.feedback) {
      html += row('⑤', `Отзыв: ${nfmt(d.feedback.rating)}/5`, esc(d.feedback.text || 'текст не заполнен'),
        num(d.feedback.rating) >= 4 ? 'on' : 'bad');
    } else {
      html += row('⑤', 'Отзыв ещё не собран', 'после выдачи система подберёт момент для запроса');
    }
    body.innerHTML = html;
  } catch (e) { body.innerHTML = `<span style="color:#ef4444">${esc(e.message)}</span>`; }
}

/* ============================================== 8.5: wish-list (#72) */
let wishCustomerId = '';
let wishCustomerName = '';
let wishList = [];
async function openWishes(id, name) {
  wishCustomerId = id;
  wishCustomerName = name || '';
  $('wish_title').textContent = `Пожелания · ${wishCustomerName || 'клиент'}`;
  $('wish_list').innerHTML = '<span class="muted">Загружаем…</span>';
  openModal('wish_modal');
  try {
    const d = await get('/api/wish/list', { customer_id: id });
    wishList = d.wishes || [];
    renderWishes();
  } catch (e) {
    $('wish_list').innerHTML = `<span style="color:#ef4444">${esc(e.message)}</span>`;
  }
}
function renderWishes() {
  const el = $('wish_list');
  if (!wishList.length) {
    el.innerHTML = '<span class="muted">Пожеланий пока нет — записывайте: «хочу, когда будет».</span>';
    return;
  }
  const WISH_STATE = { pending: ['accent', 'ждёт'], done: ['ok', 'сделано'], declined: ['outline', 'отклонено'] };
  el.innerHTML = wishList.map((w) => {
    const st = WISH_STATE[w.status] || ['outline', w.status];
    return `<div class="mini-row"><span class="dot ${w.status === 'done' ? 'on' : ''}"></span>`
      + `<div class="mbody"><b>${esc(w.text)}</b><small>${dateTimeText(w.created_at)} · ${st[1]}</small></div>`
      + `<span class="chip ${st[0]}">${st[1]}</span>`
      + (w.status === 'pending'
        ? `<button class="btn xs ok" type="button" data-wish-done="${esc(w.id)}">✓ Готово</button>`
        + `<button class="btn xs ghost" type="button" data-wish-del="${esc(w.id)}">×</button>`
        : `<button class="btn xs ghost" type="button" data-wish-del="${esc(w.id)}">×</button>`)
      + '</div>';
  }).join('');
}
async function wishAction(action, id) {
  try {
    if (action === 'done') await post('/api/wish/resolve', { id, status: 'done' });
    if (action === 'del') await post('/api/wish/delete', { id });
    const d = await get('/api/wish/list', { customer_id: wishCustomerId });
    wishList = d.wishes || [];
    renderWishes();
    if (action === 'done') toast('Отмечено готовым', 'Готовьте сообщение клиенту');
  } catch (e) { fail(e); }
}

/* ============================================== 8.5: «Мой NOZZA» (#94) */
async function openMyNozza(id) {
  const box = $('my_code');
  box.textContent = '…';
  $('my_qr').innerHTML = '';
  $('my_link').textContent = '';
  $('my_title').textContent = 'Страница клиента';
  openModal('my_nozza_modal');
  try {
    const r = await post('/api/portal/code', { customer_id: id });
    const code = String(r.code || '').toUpperCase();
    box.textContent = code;
    const base = (PF.state.settings.public_url || '').replace(/\/$/, '') || location.origin;
    const link = `${base}/my.html?code=${code}`;
    $('my_link').textContent = link;
    if (window.QR) $('my_qr').innerHTML = window.QR.svg(link, { size: 160 });
    $('my_copy').onclick = () => copyTextLocal(link, 'Ссылка «Мой NOZZA»');
    $('my_open').onclick = () => window.open(link, '_blank');
  } catch (e) {
    box.textContent = '';
    $('my_link').innerHTML = `<span style="color:#ef4444">${esc(e.message)}</span>`;
  }
}
function copyTextLocal(text, label) {
  const done = () => toast('Скопировано', label || '');
  const bad = () => fail(new Error('Не удалось скопировать'));
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, bad);
  else bad();
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
  // 13.1 (33): компактные карточки канбана
  const odens = $('orders_density');
  if (odens) odens.addEventListener('click', () => {
    orderDensity = !orderDensity;
    const kanban = $('orders_kanban');
    if (kanban) kanban.classList.toggle('compact', orderDensity);
    odens.classList.toggle('on', orderDensity);
    toast(orderDensity ? 'Компактный канбан' : 'Обычный канбан');
  });
  // 13.1 (18): ховер-превью заказа в таблице — мини-карточка у курсора
  const hoverCard = document.createElement('div');
  hoverCard.className = 'hover-card';
  hoverCard.hidden = true;
  document.body.appendChild(hoverCard);
  const tbody = $('orders_tbody');
  if (tbody) {
    tbody.addEventListener('mouseover', (e) => {
      const row = e.target.closest('tr[data-order]');
      if (!row) { hoverCard.hidden = true; return; }
      const order = PF.state.orders.find((x) => x.id === row.dataset.order);
      if (!order) { hoverCard.hidden = true; return; }
      const st = PF.status(order.status), econ = order.economics || {};
      hoverCard.innerHTML = `<b>№${esc(order.number)} · ${esc(order.product || 'Без названия')}</b>`
        + `<span class="chip" style="background:${esc(st.color)}22;color:${esc(st.color)}">${esc(st.name)}</span>`
        + `<small>${esc(order.customer_name || 'Без клиента')}${order.phone ? ' · ' + esc(order.phone) : ''}</small>`
        + `<small>${money(order.price)}${order.due ? ' · срок ' + esc(dateText(order.due)) : ''}${econ.profit != null ? ' · ' + money(econ.profit) : ''}</small>`;
      const rect = row.getBoundingClientRect();
      hoverCard.hidden = false;
      hoverCard.style.left = Math.min(innerWidth - 260, rect.left + rect.width + 10) + 'px';
      hoverCard.style.top = Math.max(8, rect.top - 4) + 'px';
    });
    tbody.addEventListener('mouseout', (e) => {
      if (!e.target.closest('tr[data-order]')) hoverCard.hidden = true;
    });
  }
  $('orders_filter_status').addEventListener('change', (e) => { filters.status = e.target.value; renderOrders(); });
  $('orders_filter_niche').addEventListener('change', (e) => { filters.niche = e.target.value; renderOrders(); });
  $('orders_view').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-mode]');
    if (!btn) return;
    orderView = btn.dataset.mode;
    $$('#orders_view button').forEach((b) => b.classList.toggle('on', b === btn));
    renderOrders();
  });
  // ЗА2: фильтр по каналу (Telegram) с запоминанием выбора
  const chanHost = $('orders_chan');
  if (chanHost) {
    const savedChan = U.store.get('pf_orders_chan', '');
    if (savedChan) {
      filters.chan = savedChan;
      $$('#orders_chan button').forEach((b) => b.classList.toggle('on', b.dataset.chan === savedChan));
    }
    chanHost.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-chan]');
      if (!btn) return;
      filters.chan = btn.dataset.chan;
      $$('#orders_chan button').forEach((b) => b.classList.toggle('on', b === btn));
      U.store.set('pf_orders_chan', filters.chan);
      renderOrders();
    });
  }
  // ЗА3: разбор запроса отмены
  const keepBtn = $('of_cancel_keep');
  if (keepBtn) keepBtn.addEventListener('click', () => resolveCancel('keep'));
  const doCancel = $('of_cancel_do');
  if (doCancel) doCancel.addEventListener('click', () => resolveCancel('canceled'));
  // ЗА4: шаблон → в поле ответа
  const tplSel = $('of_tg_template');
  if (tplSel) tplSel.addEventListener('change', () => {
    const t = ((tgThread && tgThread.templates) || []).find((x) => String(x.id) === tplSel.value);
    if (t) {
      const box = $('of_tg_reply');
      box.value = t.text || '';
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
    }
    tplSel.value = '';
  });
  const tgSend = $('of_tg_send');
  if (tgSend) tgSend.addEventListener('click', sendTgReply);
  const tgBox = $('of_tg_reply');
  if (tgBox) tgBox.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') sendTgReply();
  });
  // ЗА5: быстрые действия по чипам (оплата, отзыв)
  const chipsHost = $('of_tg_chips');
  if (chipsHost) chipsHost.addEventListener('click', (e) => {
    const pay = e.target.closest('[data-tg-pay]');
    if (pay) { confirmTgPayment(pay.dataset.tgPay); return; }
    const review = e.target.closest('[data-tg-review]');
    if (review) tgSetMode('review');
  });
  document.addEventListener('click', (e) => {
    const actionBtn = e.target.closest('[data-order-action]');
    if (actionBtn) {
      e.stopPropagation();
      e.preventDefault();
      const act = actionBtn.dataset.orderAction;
      const orderId = actionBtn.dataset.order;
      if (act === 'open') openOrder(orderId);
      else if (act === 'queue') quickQueueOrder(orderId);
      return;
    }
    const card = e.target.closest('[data-order]');
    if (card && !e.target.closest('button') && !e.target.closest('.w-check') && !e.target.closest('input') && !e.target.closest('select')) {
      openOrder(card.dataset.order);
      return;
    }
    const ne = e.target.closest('[data-niche-edit]');
    if (ne) { openNiche(ne.dataset.nicheEdit); }
    // 13.1 (40): клик по Telegram-бейджу — маршрут в клиент-бот
    const tgOpen = e.target.closest('[data-tg-open]');
    if (tgOpen) {
      e.preventDefault();
      e.stopPropagation();
      PF.go('clientbot');
      toast('Клиент-бот открыт', 'Найдите диалог по имени клиента');
    }
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
    updateOrderItemsSummary();
  });
  const itemsOverride = $('of_items_override');
  if (itemsOverride) itemsOverride.addEventListener('change', updateOrderItemsSummary);
  const itemsHost = $('of_items');
  if (itemsHost) {
    itemsHost.addEventListener('click', (e) => {
      const create = e.target.closest('[data-item-create]');
      if (create) {
        createProductFromItemRow(create.closest('.of-item-row'));
        return;
      }
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
          if (num(item.hours)) row.querySelector('[data-item-hours]').value = item.hours;
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
  /* ---- файл печати: выбрать с компьютера / скачать с принтера / на диск ---- */
  if ($('of_file_pick') && $('of_file_local')) {
    $('of_file_pick').addEventListener('click', () => $('of_file_local').click());
    $('of_file_local').addEventListener('change', () => {
      const file = $('of_file_local').files && $('of_file_local').files[0];
      $('of_file_local').value = '';
      if (file) pickOrderFile(file).catch(fail);
    });
  }
  if ($('of_file_pull')) {
    $('of_file_pull').addEventListener('click', () => pullOrderFile().catch(fail));
  }
  if ($('of_file_download')) {
    $('of_file_download').addEventListener('click', () => downloadOrderFile().catch(fail));
  }
  if ($('pull_file_list')) {
    $('pull_file_list').addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-pull-file]');
      if (!btn) return;
      const live = PF.livePrinter();
      const ok = await pullFileFromPrinter(live, btn.dataset.pullFile);
      if (ok) closeModal('pull_file_modal');
    });
  }
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

  async function quickQueueOrder(orderId) {
    const order = PF.state.orders.find((o) => o.id === orderId);
    if (!order) return;
    try {
      await post('/api/jobs/enqueue', {
        order_id: order.id,
        name: `${order.product || 'Заказ'} (№${order.number})`,
        file: order.file || '',
        est_grams: num(order.grams) || 0,
        est_minutes: Math.round((num(order.hours) || 0) * 60),
        printer_id: '',
        spool_id: '',
        source: 'order-quick',
        allow_auto_start: false,
      });
      toast('Добавлен в очередь печати', `№${order.number} · ${order.product || ''}`);
      await PF.refreshCore();
    } catch (e) {
      openOrder(orderId);
      toast('Проверьте заказ перед отправкой в очередь', e.message, 'warn');
    }
  }

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
  const catFilter = $('of_category_filter');
  if (catFilter) {
    catFilter.addEventListener('change', () => {
      const nom = $('of_nom_id');
      const currentNom = nom ? nom.value : '';
      if (nom) {
        nom.innerHTML = '<option value="">Не выбран — заказ на печать / услугу</option>'
          + buildNomenclatureGroupedOptions(PF.state.nomenclature || [], currentNom, catFilter.value);
        if (currentNom && [...nom.options].some((o) => o.value === currentNom)) {
          nom.value = currentNom;
        } else {
          nom.value = '';
          toggleWarehouseField();
          updateReadyStockHint();
        }
      }
    });
  }
  $('of_nom_id').addEventListener('change', () => {
    const item = (PF.state.nomenclature || []).find((i) => i.id === $('of_nom_id').value);
    if (!item) { $('of_reserved').checked = false; updateReadyStockHint(); toggleWarehouseField(); return; }
    applyProduct(item, true);
    toggleWarehouseField();
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
  const orderPayment = $('order_payment');
  if (orderPayment) orderPayment.addEventListener('click', () => {
    if (!editingOrder) return fail(new Error('Сначала сохраните заказ'));
    if (PF.modules.finance && PF.modules.finance.openPayment) PF.modules.finance.openPayment(editingOrder);
    else fail(new Error('Раздел платежей ещё не загружен'));
  });
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
    const wrap = $('of_docs_wrap');
    if (wrap) {
      wrap.hidden = false;
      wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    openOrderPrint('waybill');
  });
  const docsPrint = $('of_docs_print');
  if (docsPrint) docsPrint.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-b2b-kind]');
    if (!btn) return;
    openOrderPrint(btn.dataset.b2bKind);
  });
  const createWaybill = $('order_create_waybill');
  if (createWaybill) createWaybill.addEventListener('click', createOrderWaybill);
  // Свёртка мелких товаров в печатных формах: выбор запоминается в браузере.
  const docFold = $('order_doc_fold');
  if (docFold) {
    try {
      const saved = localStorage.getItem('pf.docs.fold');
      if (saved !== null) docFold.checked = saved !== '0';
    } catch (e) { /* приватный режим */ }
    docFold.addEventListener('change', () => {
      try { localStorage.setItem('pf.docs.fold', docFold.checked ? '1' : '0'); } catch (e) { /* ок */ }
    });
  }
  const docsList = $('of_docs_list');
  if (docsList) docsList.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-order-doc]');
    if (!btn) return;
    if (PF.modules.products && PF.modules.products.openDoc) PF.modules.products.openDoc(btn.dataset.orderDoc);
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
      $('order_to_warehouse').hidden = false;
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
  $('order_to_warehouse').addEventListener('click', () => openOrderStock(editingOrder));
  $('hf_payment_action').addEventListener('change', updateFulfillmentPaymentFields);
  $('fulfillment_confirm').addEventListener('click', confirmOrderFulfillment);
  $('stock_confirm').addEventListener('click', confirmOrderStock);
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
  bindTimelapseControls();                     // №47: таймлапс в карточке заказа
  $('of_tg_summary_btn').addEventListener('click', loadThreadSummary);   // №61
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
  $('customers_filter').addEventListener('click', (e) => {
    const button = e.target.closest('[data-customer-segment]');
    if (!button) return;
    customerSegment = button.dataset.customerSegment || 'all';
    $$('#customers_filter [data-customer-segment]').forEach((item) => item.classList.toggle('on', item === button));
    renderCustomers();
  });
  $('aftercare_refresh').addEventListener('click', loadAftercare);
  $('aftercare_list').addEventListener('click', (e) => {
    const button = e.target.closest('[data-aftercare]');
    if (button) openAftercare(button.dataset.aftercare);
  });

  /* 8.5: бренд-карточка, упаковка, нить изделия */
  $('order_brand_card').addEventListener('click', orderBrandCard);
  $('order_pack_card').addEventListener('click', openPackCard);
  $('order_thread').addEventListener('click', openOrderThread);

  /* 8.5: клиенты — «Мой NOZZA» и wish-list */
  $('customers_tbody').addEventListener('click', (e) => {
    const my = e.target.closest('[data-cust-my]');
    if (my) {
      openMyNozza(my.dataset.custMy);
      return;
    }
    const wish = e.target.closest('[data-cust-wish]');
    if (wish) {
      const c = PF.state.customers.find((x) => x.id === wish.dataset.custWish);
      openWishes(wish.dataset.custWish, c ? c.name : '');
    }
  });
  $('wish_add').addEventListener('click', async () => {
    const text = ($('wish_new_text').value || '').trim();
    if (!text) return fail(new Error('Опишите пожелание'));
    try {
      await post('/api/wish/save', { customer_id: wishCustomerId, text });
      $('wish_new_text').value = '';
      const d = await get('/api/wish/list', { customer_id: wishCustomerId });
      wishList = d.wishes || [];
      renderWishes();
      toast('Записано', 'Сообщим, когда будет готово');
    } catch (e) { fail(e); }
  });
  $('wish_list').addEventListener('click', (e) => {
    const done = e.target.closest('[data-wish-done]');
    if (done) { wishAction('done', done.dataset.wishDone); return; }
    const del = e.target.closest('[data-wish-del]');
    if (del && confirmDanger('Удалить пожелание?')) wishAction('del', del.dataset.wishDel);
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
PF.on('data', PF.whenView(['orders', 'customers'], () => {
  if (PF.viewOn('orders')) { fillSelectors(); renderOrders(); }
  if (PF.viewOn('customers')) renderCustomers();
}));
PF.on('finance', PF.whenView('niches', () => { renderNiches(); }));
PF.on('view', (detail) => { if (detail.view === 'customers') loadAftercare(); });

PF.modules.ops = { openOrder, openOrderFulfillment, openOrderStock, openNiche, renderOrders, fillSelectors, loadAftercare };
/* 14.0 (идея 57): #orders/<id> открывает карточку заказа. */
PF.deepLink('orders', (id) => openOrder(id));
})();
