/* PrintFlow 2.0 — дашборд, настройки и библиотека. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, clamp, money, nfmt, pct, hoursText, minutesText,
  dateText, dateTimeText, agoText, toast, fail, confirmDanger, ask, drawChart, legend, store,
  openModal, closeModal } = U;
const { get, post } = PF.api;

/* Отказоустойчивая привязка и отрисовка (18.6.3).
   Раньше один отсутствующий в разметке элемент ронял bind() или
   renderSettings() целиком: обработчики после него не навешивались,
   карточки настроек оставались пустыми — пользователь видел это как
   «вкладки «Принтеры и Bambu» и «Склад и пластик» не открываются».
   Теперь отсутствующий элемент просто пропускается, а сбой одной
   карточки не мешает соседним. */
const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
const put = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };

let dashMode = 'money';

/* ============================================================ дашборд */
function kpi(label, value, sub, kind, extra) {
  return `<div class="kpi ${kind || ''}" data-kpi="${esc(label)}"><span class="label">${esc(label)}</span>`
    + `<b class="value">${value}</b><span class="sub">${sub || ''}</span>${extra || ''}</div>`;
}

/* Н3: прошлые значения KPI — чтобы живое обновление «докручивало» цифру
   от прежнего значения к новому, а не подменяло её рывком. Ключ — подпись
   плитки: она уникальна внутри Обзора и стабильна между рендерами. */
const kpiPrev = new Map();
function animateKpis() {
  $$('#dash_kpis .kpi').forEach((tile) => {
    const valEl = tile.querySelector('.value');
    const key = tile.dataset.kpi || '';
    if (!key || !valEl) return;
    const text = valEl.textContent;
    const prev = kpiPrev.get(key);
    if (prev && prev !== text) U.countUp(valEl, prev, text);
    kpiPrev.set(key, text);
  });
}

function renderDashboard() {
  const live = PF.state.live;
  const farm = (live && live.farm) || {};
  const s = (PF.state.finance && PF.state.finance.summary) || PF.state.summary || {};
  const orders = PF.state.orders || [];
  const finals = PF.finalStatusIds();
  const activeOrders = orders.filter((o) => !finals.includes(o.status));
  const today = U.todayISO();
  const late = activeOrders.filter((o) => o.due && o.due <= today);
  const paidOf = (o) => Math.max(num(o.paid), num(o.prepaid));
  const pipeline = activeOrders.reduce((a, o) => a + Math.max(0, num(o.price) - paidOf(o)), 0);
  // В KPI «Очередь» должны попадать задания, а не весь объём незавершённых заказов:
  // иначе оператор видит часы заказа, который ещё даже не прошёл подготовку.
  const queue = (PF.state.jobs && PF.state.jobs.queue) || [];
  const queuedJobs = queue.filter((j) => !['done', 'failed', 'cancelled'].includes(j.state));
  const jobOrder = (j) => j.order || orders.find((o) => o.id === j.order_id) || {};
  const jobScale = (j) => {
    const o = jobOrder(j);
    return num(o.items_count) ? 1 : Math.max(1, num(o.qty, 1));
  };
  const needGrams = queuedJobs.reduce((a, j) => {
    const o = jobOrder(j);
    return a + (num(j.est_grams || j.grams) || num(o.grams) * jobScale(j));
  }, 0);
  const needHours = queuedJobs.reduce((a, j) => {
    const o = jobOrder(j);
    return a + (num(j.est_minutes || j.minutes) || num(o.hours) * jobScale(j) * 60) / 60;
  }, 0);
  const capacity = num(PF.state.settings.weekly_capacity_hours, 110);
  const load = capacity ? clamp(needHours / capacity * 100, 0, 999) : 0;
  const stock = num(s.stock_grams);

  put('dash_kpis', [
    kpi('Печатает сейчас', `${nfmt(farm.printing)} / ${nfmt(farm.total)}`,
      `${nfmt(farm.online)} на связи · загрузка ${nfmt(farm.utilization)}%`,
      num(farm.printing) ? 'ok' : ''),
    kpi('Очередь печати', hoursText(needHours), `${nfmt(queuedJobs.length)} заданий · ${pct(load)} от ${nfmt(capacity)} ч в неделю`,
      load > 100 ? 'bad' : load > 85 ? 'warn' : '',
      `<div class="bar ${load > 100 ? 'bad' : load > 85 ? 'warn' : ''}"><i style="width:${clamp(load, 0, 100)}%"></i></div>`),
    kpi('Активные заказы', String(activeOrders.length), `${nfmt(farm.queued)} заданий в производстве`),
    kpi('Прибыль за период', money(s.profit), `маржа ${pct(s.margin)}` + (farm.today_hours ? ` · сегодня ${nfmt(farm.today_hours, 1)} ч` : ''), num(s.profit) >= 0 ? 'ok' : 'bad'),
  ].join(''));
  animateKpis();

  const series = (PF.state.finance && PF.state.finance.series) || [];
  const cut = series.slice(-PF.state.dashDays);
  const MODES = {
    money: {
      keys: [
        { key: 'income', label: 'Доход', color: 'var(--ok)', type: 'bar', fmt: (v) => money(v) },
        { key: 'expense', label: 'Расход', color: 'var(--bad)', type: 'bar', opacity: .55, fmt: (v) => money(v) },
        { key: 'profit', label: 'Прибыль', color: 'var(--accent)', type: 'line', area: true, fmt: (v) => money(v) },
      ], sub: 'Доход, расход и прибыль по дням',
    },
    hours: {
      keys: [{ key: 'hours', label: 'Часы печати', color: 'var(--accent)', type: 'bar', fmt: (v) => hoursText(v) }],
      sub: 'Часы печати по дням',
    },
    grams: {
      keys: [{ key: 'grams', label: 'Пластик, г', color: 'var(--accent-2)', type: 'bar', fmt: (v) => nfmt(v) + ' г' }],
      sub: 'Расход пластика по дням',
    },
  };
  const mode = MODES[dashMode] || MODES.money;
  $('dash_chart_sub').textContent = mode.sub;
  drawChart($('dash_chart'), $('dash_tip'), cut, mode.keys, { height: 220 });
  legend($('dash_legend'), mode.keys);

  const printers = (live && live.printers) || [];
  // ПР9: компактные карточки парка в том же языке, что и лента на «Принтерах».
  const DPC = 2 * Math.PI * 15.5;
  put('dash_printers', printers.length ? `<div class="dp-grid">` + printers.map((p) => {
    const st = p.printer.state;
    const running = st === 'RUNNING' || st === 'PREPARE';
    const progress = clamp(num(p.printer.progress), 0, 100);
    const problems = (p.printer.problems || []).length;
    const alerts = (((p.guard || {}).alerts) || []).length;
    const trays = ((p.ams || {}).trays) || [];
    const sub = running
      ? `${esc(String(p.printer.task || 'Печать').slice(0, 30))} · осталось ${minutesText(p.printer.remaining_min)}`
      : esc(p.connection.connected ? (p.printer.state_label || 'Готов к печати') : (p.connection.last_error || 'Нет связи'));
    return `<button class="dp-card${running ? ' run' : ''}${p.connection.connected ? '' : ' off'}" type="button" data-dp-printer="${esc(p.id)}"`
      + ` title="${esc(p.name)} — открыть на вкладке «Принтеры»">`
      + `<span class="dp-ring"><svg viewBox="0 0 40 40" aria-hidden="true">`
      + `<circle class="tr" cx="20" cy="20" r="15.5"/>`
      + `<circle class="fl" cx="20" cy="20" r="15.5" stroke-dasharray="${DPC.toFixed(1)}"`
      + ` style="stroke-dashoffset:${p.connection.connected ? (DPC * (1 - progress / 100)).toFixed(1) : DPC.toFixed(1)}"/>`
      + `</svg><b>${p.connection.connected ? (running ? Math.round(progress) + '%' : '✓') : '◌'}</b></span>`
      + `<span class="dp-main"><b>${esc(p.name)}</b>`
      + `<small>${sub}</small>`
      + `<span class="dp-sw">${trays.slice(0, 4).map((t) => {
        const has = t.present !== false && (t.present || t.generic || t.type || t.uuid);
        return `<i class="${has ? '' : 'ghost'}" style="background:${esc(t.color || '#38445c')}"></i>`;
      }).join('')}</span></span>`
      + `<span class="dp-flags">${alerts ? '<i class="flag alarm" title="Тревога сторожа">!</i>' : ''}${problems ? `<i class="flag hms" title="HMS: ${problems}">▲</i>` : ''}</span>`
      + `</button>`;
  }).join('') + '</div>' : '<div class="empty compact"><span>Принтер ещё не добавлен.</span></div>');

  const due = activeOrders
    .filter((o) => o.due)
    .sort((a, b) => String(a.due).localeCompare(String(b.due)))
    .slice(0, 8);
  put('dash_due', due.length ? due.map((o) => {
    const st = PF.status(o.status);
    const isLate = o.due < today, isToday = o.due === today;
    return `<div class="tx-row clickable" data-order="${esc(o.id)}" style="cursor:pointer">`
      + `<span class="tx-ic ${isLate ? 'expense' : 'income'}">${isLate ? '!' : '◷'}</span>`
      + `<div class="tx-body"><b>№${esc(o.number)} · ${esc(o.product)}</b>`
      + `<small>${esc(o.customer_name || 'без клиента')} · ${esc(st.name)}</small></div>`
      + `<span class="amt ${isLate ? 'neg' : ''}">${isLate ? 'просрочен' : isToday ? 'сегодня' : esc(dateText(o.due))}</span></div>`;
  }).join('') : '<div class="empty compact"><span>Заказов со сроками нет.</span></div>');

  renderOperatorFocus();
  renderOwnerCash();
  renderPlan();
  renderHealth();
  renderActivePrint();
  renderGauge();
  renderAmsPanel();
  renderFilamentForecast();
  renderTimeline();
  renderRecords();
  applyWidgets();
  applyDashOrder();
}

/* ============================================ 13.1 (41): рекорды цеха
   «Самая долгая печать 14 ч 22 м», «лучший день 12 400 ₽» — гордость
   цифрами, без отдельного отчёта. Считается из уже загруженных данных. */
function renderRecords() {
  const host = $('dash_records');
  if (!host) return;
  const history = (PF.state.jobs && PF.state.jobs.history) || [];
  const series = (PF.state.finance && PF.state.finance.series) || [];
  const records = [];
  const finished = history.filter((j) => j.state === 'done' || j.state === 'complete');
  const longest = finished.reduce((best, j) => {
    const min = Math.max(num(j.minutes), num(j.est_minutes));
    return min > best.min ? { min, j } : best;
  }, { min: 0, j: null });
  if (longest.j) {
    records.push({ icon: '⏱', label: 'Самая долгая печать', value: minutesText(longest.min),
      sub: longest.j.name || longest.j.file || 'задание' });
  }
  const bestDay = series.reduce((best, row) => (num(row.income) > best.income ? { income: num(row.income), row } : best), { income: 0, row: null });
  if (bestDay.row) {
    records.push({ icon: '🏆', label: 'Лучший день по доходам', value: money(bestDay.income),
      sub: dateText(bestDay.row.day) });
  }
  const heavy = finished.reduce((best, j) => {
    const g = num(j.grams) || num(j.est_grams);
    return g > best.g ? { g, j } : best;
  }, { g: 0, j: null });
  if (heavy.j && heavy.g > 0) {
    records.push({ icon: '◍', label: 'Самая тяжёлая деталь', value: nfmt(heavy.g) + ' г',
      sub: heavy.j.name || heavy.j.file || 'задание' });
  }
  if (!records.length) { host.hidden = true; host.innerHTML = ''; return; }
  host.hidden = false;
  host.innerHTML = records.map((r) => `<span class="dash-record" title="${esc(r.sub)}">`
    + `<i>${r.icon}</i><b>${esc(r.value)}</b><small>${esc(r.label)}</small></span>`).join('');
}

function renderOperatorFocus() {
  const host = $('operator_focus');
  if (!host) return;
  const live = PF.state.live || {};
  const queue = (PF.state.jobs && PF.state.jobs.queue) || [];
  const history = (PF.state.jobs && PF.state.jobs.history) || [];
  const orders = PF.state.orders || [];
  const finals = PF.finalStatusIds();
  const activeOrders = orders.filter((o) => !finals.includes(o.status));
  const today = U.todayISO();
  const items = [];
  const add = (priority, icon, title, detail, button, attrs) => items.push({ priority, icon, title, detail, button, attrs: attrs || '' });
  const route = (name, id) => `data-focus-route="${name}"${id ? ` data-focus-id="${esc(id)}"` : ''}`;

  const failed = history.filter((j) => {
    if (j.state !== 'failed') return false;
    const at = Date.parse(j.finished_at || j.created_at || '');
    return !Number.isFinite(at) || Date.now() - at <= 48 * 60 * 60 * 1000;
  }).slice(0, 3);
  if (failed.length) {
    add(1, '✕', `${failed.length} печать${failed.length === 1 ? '' : 'и'} завершилась браком`,
      failed.map((j) => j.name || j.file || 'Без имени').join(' · '), 'Открыть журнал', route('queue'));
  }

  const broken = (live.printers || []).filter((p) => !p.connection || !p.connection.connected
    || (p.printer && p.printer.problems && p.printer.problems.length));
  broken.slice(0, 3).forEach((p) => {
    const reason = p.connection && p.connection.connected
      ? ((p.printer.problems || [])[0] || {}).title || 'Проверьте состояние'
      : (p.connection && p.connection.last_error) || 'Нет связи';
    add(1, '⚠', `${p.name}: требуется проверка`, reason, 'Открыть принтер', route('printers', p.id));
  });

  const stale = queue.filter((j) => ['uploading', 'starting'].includes(j.state)
    && j.created_at && Date.now() - Date.parse(j.created_at) > 10 * 60 * 1000);
  if (stale.length) {
    add(1, '⏳', `${stale.length} задание${stale.length === 1 ? ' зависло' : ' зависли'} на подготовке`,
      'Файл не запускается автоматически — проверьте связь и повторите действие.', 'Открыть очередь', route('queue'));
  }

  const ready = activeOrders.filter((o) => {
    const name = String(PF.status(o.status).name || '').toLowerCase();
    return o.status === 'ready' || name.includes('готов');
  });
  if (ready.length) {
    const first = ready[0];
    add(2, '✓', `${ready.length} заказ${ready.length === 1 ? '' : 'а'} готовы к выдаче`,
      ready.slice(0, 2).map((o) => `№${o.number}`).join(' · '), 'Открыть выдачу',
      `data-focus-fulfill="${esc(first.id)}"`);
  }

  const overdue = activeOrders.filter((o) => o.due && o.due <= today);
  if (overdue.length) {
    add(2, '!', `${overdue.length} заказ${overdue.length === 1 ? '' : 'а'} требуют срока`,
      overdue.slice(0, 2).map((o) => `№${o.number} · ${dateText(o.due)}`).join(' · '), 'Открыть заказы', route('orders'));
  }

  const unassigned = queue.filter((j) => j.state === 'queued' && !j.printer_id);
  if (unassigned.length) {
    add(2, '↗', `${unassigned.length} заданию не назначен принтер`,
      'Назначьте принтер перед стартом, чтобы не искать ошибку в момент запуска.', 'Открыть очередь', route('queue'));
  }

  const low = (PF.state.spools || []).filter((s) => !num(s.archived)
    && num(s.percent) < num(PF.state.settings.filament_low_threshold, 15));
  if (low.length) {
    add(3, '◒', `${low.length} катуш${low.length === 1 ? 'ка' : 'ки'} заканчивается`,
      low.slice(0, 2).map((s) => `${s.material || 'пластик'} ${nfmt(s.percent, 0)}%`).join(' · '), 'Открыть склад', route('stock'));
  }

  const next = queue.find((j) => j.state === 'queued');
  if (next && !unassigned.length) {
    add(3, '▶', 'Можно запускать следующее задание',
      `${next.name || next.file || 'Без имени'}${next.printer_id ? '' : ' · принтер определится при запуске'}`,
      'Открыть очередь', route('queue'));
  }

  items.sort((a, b) => a.priority - b.priority);
  $('operator_focus_sub').textContent = items.length
    ? `${items.length} ${items.length === 1 ? 'действие' : 'действий'} · сначала критичное`
    : 'Очередь, принтеры и выдача без срочных проблем';
  host.innerHTML = items.length
    ? items.slice(0, 7).map((item) => `<div class="focus-row priority-${item.priority}">`
      + `<span class="focus-icon">${item.icon}</span><div class="focus-body"><b>${esc(item.title)}</b><small>${esc(item.detail)}</small></div>`
      + `<button class="btn sm ${item.priority === 1 ? 'danger' : ''}" type="button" ${item.attrs}>${item.button}</button></div>`).join('')
    : '<div class="focus-empty"><span class="focus-ok">✓</span><div><b>Срочных действий нет</b><small>Можно продолжать плановую печать или открыть любой раздел.</small></div><button class="btn sm" type="button" data-focus-route="queue">Открыть очередь</button></div>';
}

function renderOwnerCash() {
  const host = $('owner_cash');
  if (!host) return;
  const moneyState = PF.state.money || {};
  const acc = moneyState.accounts || {};
  const debts = moneyState.debts || {};
  const have = moneyState.accounts || moneyState.debts;
  const cash = have ? num(acc.total) : null;
  const owe = have ? num(debts.total) : (PF.state.orders || []).reduce((a, o) => {
    const paid = Math.max(num(o.paid), num(o.prepaid));
    return a + Math.max(0, num(o.price) - paid);
  }, 0);
  const overdue = have ? num(debts.overdue) : 0;
  const nacc = (acc.accounts || []).length;
  const ndebt = have ? nfmt(debts.count) : '—';
  host.hidden = false;
  host.innerHTML = `<div class="oc ${cash != null && cash < 0 ? 'bad' : 'ok'}"><span>В кассе</span><b>${cash == null ? '…' : money(cash)}</b>`
    + `<span>${nacc ? nfmt(nacc) + ' касс(ы)' : 'откроется Финансы'}</span></div>`
    + `<div class="oc ${num(owe) ? 'warn' : 'ok'}"><span>Должны нам</span><b>${money(owe)}</b><span>${have ? ndebt + ' заказ(ов)' : 'по активным заказам'}</span></div>`
    + `<div class="oc ${num(overdue) ? 'bad' : 'ok'}"><span>Просрочка</span><b>${have ? money(overdue) : '—'}</b>`
    + `<span>дольше ${nfmt(PF.state.settings.debt_alert_days, 0)} дн.</span></div>`;
}

function renderEvents() {
  const list = PF.state.events || [];
  put('dash_events', list.length ? list.slice(0, 18).map((e) => `<div class="event ${esc(e.kind)}">`
    + '<span class="edot"></span><span class="etext">'
    + `<b>${esc(e.title)}</b><small>${esc(e.detail || '')}</small></span>`
    + `<time title="${esc(dateTimeText(e.at))}">${esc(agoText(e.at))}</time></div>`).join('')
    : '<div class="empty compact"><span>Событий пока нет.</span></div>');
}

/* ================================================= виджеты панели */
const DASH_WIDGETS = [
  ['kpis', 'Показатели смены (4 KPI)'],
  ['operator_focus', 'Сейчас нужно сделать'],
  ['due', 'Ближайшие сроки'],
  ['plan', 'План на сегодня'],
  ['health', 'Здоровье бизнеса'],
  ['gauge', 'Прибыль за час печати'],
  ['filament', 'Пластик на очередь'],
  ['ams', 'AMS на главной'],
  ['chart', 'Деньги и печать по дням'],
  ['printers', 'Парк принтеров'],
  ['events', 'Лента событий'],
  ['timeline', 'Таймлайн печати за день'],
  ['achievements', 'Достижения цеха'],
  ['heartbeat', 'Здоровье системы'],
];
const WIDGET_KEY = 'pf_dash_widgets';
function widgetPrefs() {
  try {
    const v = JSON.parse(store.get(WIDGET_KEY, 'null'));
    if (Array.isArray(v) && v.length) return v.filter((id) => DASH_WIDGETS.some(([w]) => w === id));
  } catch (e) { /* повреждённые настройки — вернём всё */ }
  return DASH_WIDGETS.map(([id]) => id);
}
function saveWidgetPrefs(list) { store.set(WIDGET_KEY, JSON.stringify(list)); }
function applyWidgets() {
  const prefs = widgetPrefs();
  $$('[data-widget]').forEach((el) => el.classList.toggle('hidden', !prefs.includes(el.dataset.widget)));
}

/* ============================================ 13.1 (66): перетаскивание
   виджетов дашборда. Каждый строит свой утренний экран: за ручку «⠿»
   карточка перетаскивается выше/ниже, порядок сохраняется в localStorage. */
const DASH_ORDER_KEY = 'pf_dash_order';
function dashRowId(row) {
  if (row.dataset.widget) return row.dataset.widget;
  const first = row.querySelector('[data-widget]');
  return first ? first.dataset.widget : '';
}
function dashOrder() {
  try {
    const v = JSON.parse(store.get(DASH_ORDER_KEY, 'null'));
    if (Array.isArray(v) && v.length) return v;
  } catch (e) { /* повреждено — начнём с текущего */ }
  return [];
}
function applyDashOrder() {
  const saved = dashOrder();
  if (!saved.length) return;
  const view = $('view-dashboard');
  if (!view) return;
  const rows = $$(':scope > *', view).filter((el) => !el.classList.contains('view-head') && dashRowId(el));
  const current = rows.map(dashRowId);
  const wanted = saved.filter((id) => current.includes(id)).concat(current.filter((id) => !saved.includes(id)));
  if (current.join() === wanted.join()) return;
  const byId = new Map(rows.map((r) => [dashRowId(r), r]));
  wanted.forEach((id) => { const row = byId.get(id); if (row) view.appendChild(row); });
}
function initDashDrag() {
  const view = $('view-dashboard');
  if (!view || view.dataset.drag) return;
  view.dataset.drag = '1';
  let dragRow = null;
  const rows = $$(':scope > *', view).filter((el) => !el.classList.contains('view-head'));
  rows.forEach((row) => {
    const grip = document.createElement('button');
    grip.type = 'button';
    grip.className = 'dash-grip';
    grip.title = 'Перетащите, чтобы изменить порядок на утреннем экране';
    grip.setAttribute('aria-label', 'Изменить порядок виджета');
    grip.innerHTML = '<span aria-hidden="true">⠿</span>';
    row.appendChild(grip);
    row.draggable = true;
    row.addEventListener('dragstart', (e) => {
      if (!e.target.closest('.dash-grip')) { e.preventDefault(); return; }
      dragRow = row;
      row.classList.add('dragging');
      if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', dashRowId(row)); }
    });
    row.addEventListener('dragover', (e) => {
      if (!dragRow || dragRow === row) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      row.classList.add('drop-target');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
    row.addEventListener('drop', (e) => {
      e.preventDefault();
      row.classList.remove('drop-target');
      if (!dragRow || dragRow === row) return;
      const all = $$(':scope > *', view).filter((el) => !el.classList.contains('view-head'));
      const from = all.indexOf(dragRow);
      const to = all.indexOf(row);
      view.insertBefore(dragRow, to < from ? row : row.nextSibling);
      const order = $$(':scope > *', view).filter((el) => !el.classList.contains('view-head'))
        .map(dashRowId).filter(Boolean);
      store.set(DASH_ORDER_KEY, JSON.stringify(order));
      dragRow.classList.remove('dragging');
      dragRow = null;
      toast('Порядок сохранён', 'Утренний экран теперь такой');
    });
    row.addEventListener('dragend', () => {
      dragRow = null;
      rows.forEach((r) => r.classList.remove('dragging', 'drop-target'));
    });
  });
}
function renderWidgetsList() {
  const prefs = widgetPrefs();
  put('dash_widgets_list', DASH_WIDGETS.map(([id, label]) =>
    `<label class="widget-check${prefs.includes(id) ? ' on' : ''}"><input type="checkbox" data-widget-check="${id}"${prefs.includes(id) ? ' checked' : ''}><span>${esc(label)}</span></label>`).join(''));
}
function dashEmpty(msg) { return `<div class="empty compact"><span>${esc(msg)}</span></div>`; }

/* ================================================ 18.10.0: HERO-ПУЛЬТ СТАНКА НА ГЛАВНОМ ЭКРАНЕ */
async function execHeroCommand(printerId, cmd, btn) {
  if (!printerId || !cmd) return;
  const DANGER_MSGS = {
    pause: 'Поставить печать на паузу? Оператор должен контролировать состояние принтера.',
    resume: 'Продолжить печать? Принтер снова нагреется и продолжит движение.',
    stop: 'Остановить печать? Задание будет прервано, деталь придётся печатать заново.',
  };
  const ask = DANGER_MSGS[cmd];
  if (ask && !confirmDanger(ask)) return;
  try {
    await U.withBusy(btn, async () => {
      await post('/api/printer/command', {
        printer_id: printerId,
        command: cmd,
        confirmed: Boolean(ask),
      });
      toast('Команда отправлена', cmd === 'light_toggle' ? 'Свет' : cmd);
      setTimeout(PF.poll, 500);
    });
  } catch (err) {
    fail(err);
  }
}

async function execHeroSpeed(printerId, level, btn) {
  if (!printerId || !level) return false;
  try {
    await U.withBusy(btn, async () => {
      await post('/api/printer/command', {
        printer_id: printerId,
        command: 'speed',
        value: level,
      });
      const labels = { 1: 'Тихо (50%)', 2: 'Стандарт (100%)', 3: 'Спорт (124%)', 4: 'Ludicrous (166%)' };
      toast('Скорость отправлена', `${labels[level] || level} — станок подтвердит через пару секунд`);
      setTimeout(PF.poll, 500);
    });
    return true;
  } catch (err) {
    fail(err);
    return false;
  }
}

async function execHeroStartJob(jobId, printerId, btn) {
  if (!jobId || !printerId) return;
  try {
    await U.withBusy(btn, async () => {
      const check = await post('/api/printer/preflight', {
        job_id: jobId,
        printer_id: printerId,
      });
      const blocks = (check && check.blocks) || [];
      if (blocks.length) {
        throw new Error('Preflight блокирует старт: ' + blocks.map((x) => x.title || x.detail).join('; '));
      }
      const warns = (check && check.warnings) || [];
      if (warns.length) {
        const msg = 'Preflight предупреждения:\n' + warns.map((x) => '• ' + (x.title || x.detail)).join('\n')
          + '\n\nПодтвердите запуск задания.';
        if (!confirmDanger(msg)) return;
      }
      await post('/api/jobs/start', {
        id: jobId,
        printer_id: printerId,
        confirmed: true,
        preflight_acknowledged: true,
      });
      toast('Задание запущено', 'Печать начата');
      setTimeout(PF.poll, 500);
    });
  } catch (err) {
    fail(err);
  }
}

/* Перед полной пересборкой Hero-пульта гасим виджеты со своими таймерами
   (шкала слоёв опрашивает бэкенд, пока идёт разбор файла). */
function heroReleaseWidgets(host) {
  const lay = host.querySelector('[data-hf="layers"]');
  if (lay && lay._layers) lay._layers.destroy();
}

function renderActivePrint() {
  const host = $('dash_active');
  const live = PF.state.live;
  const printers = (live && live.printers) || [];

  // Смарт-фокус: выбранный оператором или приоритетный принтер
  let snap = null;
  if (PF.state.dashHeroPrinterId) {
    snap = printers.find((p) => p.id === PF.state.dashHeroPrinterId);
  }
  if (!snap) {
    snap = printers.find((p) => ['RUNNING', 'PAUSE', 'PREPARE'].includes((p.printer || {}).state));
    if (!snap) snap = printers.find((p) => ((p.printer || {}).problems || []).length > 0);
    if (!snap) snap = printers.find((p) => (p.connection || {}).connected);
    if (!snap) snap = (live && (live.active || printers[0])) || null;
  }

  // Селектор принтеров фермы в шапке
  const prHost = $('dash_hero_printers');
  if (prHost) {
    if (printers.length > 1) {
      prHost.innerHTML = printers.map((p) => {
        const st = (p.printer || {}).state;
        const isRun = st === 'RUNNING' || st === 'PREPARE';
        const isPause = st === 'PAUSE';
        const isOn = snap && p.id === snap.id;
        return `<button class="hero-printer-pill${isOn ? ' on' : ''}${isRun ? ' run' : ''}${isPause ? ' pause' : ''}" type="button" data-hero-printer="${esc(p.id)}" title="${esc(p.name)} · ${esc((p.printer || {}).state_label || '')}">`
          + `<span class="st-dot"></span><span>${esc(p.name)}</span></button>`;
      }).join('');
    } else {
      prHost.innerHTML = '';
    }
  }

  const dot = $('dash_hero_dot');
  if (!snap) {
    if (dot) dot.className = 'hero-live-dot off';
    if ($('dash_active_sub')) $('dash_active_sub').textContent = 'Принтеры не добавлены';
    if (host) { host.innerHTML = dashEmpty('Принтер не добавлен — подключите его в разделе «Принтеры».'); host.dataset.heroKey = ''; }
    return;
  }

  const p = snap;
  const info = p.printer || {};
  // Снимок принтера отдаёт temperature (ед. ч.); старое имя оставлено на случай
  // чужих снимков — до 18.11 из-за него Hero-пульт показывал «—» вместо градусов.
  const t = p.temperature || p.temperatures || {};
  const running = ['RUNNING', 'PAUSE', 'PREPARE'].includes(info.state);

  if (dot) {
    dot.className = 'hero-live-dot' + (running ? (info.state === 'PAUSE' ? ' warn' : ' pulse') : (p.connection && p.connection.connected ? '' : ' off'));
  }

  const trays = (p.ams && p.ams.trays) || [];
  const activeTray = trays.find((tr) => tr.active) || trays.find((tr) => tr.present !== false && (tr.present || tr.type || tr.uuid));
  const telemetryHtml = `
        <span class="hero-chip" title="Сопло: факт / цель">🌡 Сопло: <b>${nfmt(t.nozzle, 0)}°C</b>${t.nozzle_target ? ' / ' + nfmt(t.nozzle_target, 0) + '°' : ''}</span>
        <span class="hero-chip" title="Стол: факт / цель">🛏 Стол: <b>${nfmt(t.bed, 0)}°C</b>${t.bed_target ? ' / ' + nfmt(t.bed_target, 0) + '°' : ''}</span>
        ${t.chamber ? `<span class="hero-chip" title="Температура камеры">□ Камера: <b>${nfmt(t.chamber, 0)}°C</b></span>` : ''}
        ${activeTray ? `<span class="hero-chip" title="${esc(activeTray.label || 'Активный слот AMS')}"><span class="hero-chip-sw" style="background:${esc(activeTray.color || '#38445c')}"></span> <b>${esc(activeTray.type || activeTray.material || 'Пластик')}</b> <small>${esc(activeTray.label || activeTray.slot || '')}</small></span>` : ''}`;

  // 18.11: инкрементальный рендер. Полная перерисовка — только когда меняется
  // структура (принтер, режим, камера, задание). Иначе телеметрия каждые
  // 1–2 с пересоздавала <img> MJPEG (поток стартовал заново), сбрасывала
  // регулятор скорости и анимацию тракта AMS.
  if (!running) {
    const isConn = p.connection && p.connection.connected;
    if ($('dash_active_sub')) {
      $('dash_active_sub').textContent = `${esc(p.name)} · ${esc(isConn ? (info.state_label || 'Готов к печати') : 'Нет связи')}`;
    }
    const queue = (PF.state.jobs && PF.state.jobs.queue) || [];
    const queuedJobs = queue.filter((j) => !['done', 'failed', 'cancelled'].includes(j.state));
    const nextJob = queuedJobs[0];
    const key = ['idle', p.id, isConn ? 1 : 0, info.state_label || '', nextJob ? nextJob.id : '', nextJob ? nextJob.name : ''].join('|');
    if (host && host.dataset.heroKey === key && host.querySelector('[data-hf="telemetry"]')) {
      host.querySelector('[data-hf="telemetry"]').innerHTML = telemetryHtml;
      if (window.PFAmsPath) PFAmsPath.mount(host.querySelector('[data-hf="ams"]'), p.ams, { compact: true });
      return;
    }

    let nextJobHtml = '';
    if (nextJob) {
      const orders = PF.state.orders || [];
      const order = nextJob.order || orders.find((o) => o.id === nextJob.order_id);
      nextJobHtml = `
        <div class="hero-idle-card">
          <div class="hero-idle-card-head">
            <div>
              <b>Следующее задание из очереди: «${esc(nextJob.name || nextJob.file || 'Задание')}»</b>
              ${order ? `<small style="display:block;margin-top:3px;color:var(--text-2)"><a href="#orders" class="order-link" data-order-open="${esc(order.id || '')}">Заказ №${esc(order.number || '')} · ${esc(order.product || '')}</a></small>` : ''}
            </div>
            <button class="btn sm primary hero-btn" type="button" data-hero-start-job="${esc(nextJob.id)}" data-pid="${esc(p.id)}" ${isConn ? '' : 'disabled'} title="Preflight проверит филамент и стол, потом отправит файл на станок">
              <span class="ic">▶</span>Запустить из очереди
            </button>
          </div>
          <div class="hero-idle-job-details">
            ${nextJob.est_minutes ? `<span>Оценка: <b>${minutesText(nextJob.est_minutes)}</b></span>` : ''}
            ${nextJob.est_grams ? `<span>Расход: <b>${nfmt(nextJob.est_grams)} г</b></span>` : ''}
            ${nextJob.material ? `<span>Материал: <b>${esc(nextJob.material)}</b></span>` : ''}
          </div>
        </div>`;
    } else {
      nextJobHtml = `
        <div class="hero-idle-card" style="border-style:dashed">
          <div class="hero-idle-card-head">
            <span style="color:var(--muted)">Очередь печати пуста.</span>
            <a href="#print" class="btn sm ghost" data-view="print">Перейти к нарезке →</a>
          </div>
        </div>`;
    }

    if (host) {
      heroReleaseWidgets(host);
      host.dataset.heroKey = key;
      host.innerHTML = `
        <div class="hero-body">
          <div class="hero-idle-container">
            <div class="hero-idle-status">
              <span class="hero-idle-badge"><i class="dot" style="${isConn ? '' : 'background:var(--bad)'}"></i> ${esc(isConn ? (info.state_label || 'Готов к печати') : 'Нет связи')}</span>
              <div class="hero-telemetry-row" data-hf="telemetry">${telemetryHtml}</div>
            </div>
            ${nextJobHtml}
          </div>
          <div class="hero-ams-block">
            <div class="hero-block-head"><b>Тракт AMS</b><small>что заправлено и из какого слота пойдёт печать</small></div>
            <div data-hf="ams"></div>
          </div>
        </div>`;
      if (window.PFAmsPath) PFAmsPath.mount(host.querySelector('[data-hf="ams"]'), p.ams, { compact: true });
    }
    return;
  }

  // Режим печати (RUNNING / PAUSE / PREPARE)
  const job = p.job || {};
  const orders = PF.state.orders || [];
  const order = job.order || orders.find((o) => o.id === job.order_id) || {};
  const progress = clamp(num(info.progress), 0, 100);
  const remaining = num(info.remaining_min);
  const elapsed = num(info.elapsed_min);
  const eta = info.eta ? new Date(num(info.eta) > 1e12 ? num(info.eta) : num(info.eta) * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '';
  const facts = [];
  if (info.layer) facts.push(`<span>Слой <b>${info.layer} / ${info.total_layers || '—'}</b></span>`);
  if (elapsed) facts.push(`<span>В печати <b>${minutesText(elapsed)}</b></span>`);
  if (eta && remaining) facts.push(`<span>Финиш в <b>${eta}</b></span>`);

  if ($('dash_active_sub')) {
    $('dash_active_sub').textContent = `${esc(p.name)} · ${esc(info.state_label || 'Печать')}`;
  }

  const camAvailable = p.camera && p.camera.available !== false;
  const speedLvl = num(info.speed_level, 2) || 2;
  const remHtml = remaining ? `Осталось <b>${minutesText(remaining)}</b>` : (info.state === 'PAUSE' ? 'На паузе' : '');
  const key = ['run', p.id, camAvailable ? 1 : 0, info.state === 'PAUSE' ? 1 : 0, order.number || '', order.id || '', info.task || '', PF.state.camSession || ''].join('|');

  if (host && host.dataset.heroKey === key && host.querySelector('[data-hf="pct"]')) {
    // Быстрое обновление живых полей без пересборки DOM.
    host.querySelector('[data-hf="pct"]').innerHTML = `${Math.round(progress)}<small>%</small>`;
    host.querySelector('[data-hf="rem"]').innerHTML = remHtml;
    host.querySelector('[data-hf="bar"]').style.width = `${progress}%`;
    host.querySelector('[data-hf="facts"]').innerHTML = facts.join('');
    host.querySelector('[data-hf="telemetry"]').innerHTML = telemetryHtml;
    if (window.PFAmsPath) PFAmsPath.mount(host.querySelector('[data-hf="ams"]'), p.ams, { compact: true });
    const knobEl = host.querySelector('[data-hf="knob"]');
    if (knobEl && knobEl._knob) knobEl._knob.set(speedLvl);
    const layEl = host.querySelector('[data-hf="layers"]');
    if (layEl && layEl._layers) layEl._layers.update({ layer: info.layer, total: info.total_layers });
    return;
  }

  const camUrl = `/api/printer/camera.mjpeg?printer_id=${encodeURIComponent(p.id)}&t=${PF.state.camSession || Date.now()}`;
  const camHtml = camAvailable ? `
    <div class="hero-cam-col">
      <div class="hero-cam-wrap">
        <span class="hero-cam-badge"><i class="live-red"></i> LIVE</span>
        <img class="hero-cam-img" src="${camUrl}" alt="Камера ${esc(p.name)}"
             onerror="this.parentElement.innerHTML='<div class=\\'hero-cam-fallback\\'><span class=\\'ic\\'>📷</span><span>Камера принтера временно недоступна</span></div>'">
      </div>
    </div>` : `
    <div class="hero-cam-col">
      <div class="hero-cam-wrap">
        <div class="hero-cam-fallback">
          <span class="ic">📷</span>
          <span>Камера выключена или недоступна</span>
          <small class="muted">${esc(p.name)}</small>
        </div>
      </div>
    </div>`;

  const orderLink = order.number
    ? `<a href="#orders" class="hero-order-badge" data-order-open="${esc(order.id || '')}"><span>№${esc(order.number)}</span> · <span>${esc(order.product || '')}</span>${order.customer_name ? ' · <small>' + esc(order.customer_name) + '</small>' : ''}</a>`
    : `<button class="btn xs primary" type="button" data-convert-order="${esc(p.id)}" style="margin-top:6px;padding:2px 8px;font-size:11px"><span class="ic">✨</span>Создать заказ из печати</button>`;

  if (host) {
    heroReleaseWidgets(host);
    host.dataset.heroKey = key;
    host.innerHTML = `
      <div class="hero-body">
        <div class="hero-grid">
          ${camHtml}
          <div class="hero-info-col">
            <div class="hero-job-header">
              <div class="hero-job-main">
                <h3 class="hero-job-title">${esc(info.task || 'Печать задания')}</h3>
                ${orderLink}
              </div>
            </div>

            <div class="hero-progress-section">
              <div class="hero-progress-meta">
                <span class="hero-pct-big" data-hf="pct">${Math.round(progress)}<small>%</small></span>
                <span class="hero-time-rem" data-hf="rem">${remHtml}</span>
              </div>
              <div class="hero-progress-bar${info.state === 'PAUSE' ? ' paused' : ''}">
                <i data-hf="bar" style="width:${progress}%"></i>
              </div>
              <div class="hero-facts-row" data-hf="facts">
                ${facts.join('')}
              </div>
            </div>

            <div class="hero-telemetry-row" data-hf="telemetry">${telemetryHtml}</div>

            <div class="hero-control-bar">
              <div class="hero-action-buttons">
                ${info.state === 'PAUSE'
                  ? `<button class="btn sm primary hero-btn" type="button" data-hero-cmd="resume" data-pid="${esc(p.id)}" title="Станок прогреется и продолжит с того же места"><span class="ic">▶</span>Продолжить</button>`
                  : `<button class="btn sm hero-btn" type="button" data-hero-cmd="pause" data-pid="${esc(p.id)}" title="Пауза с подтверждением: сопло отъедет, деталь останется на столе"><span class="ic">⏸</span>Пауза</button>`
                }
                <button class="btn sm ghost hero-btn" type="button" data-hero-cmd="light_toggle" data-pid="${esc(p.id)}" title="Включить или выключить подсветку камеры"><span class="ic">☀</span>Свет</button>
                <button class="btn sm danger hero-btn" type="button" data-hero-cmd="stop" data-pid="${esc(p.id)}" title="Прервать печать (с подтверждением). Деталь придётся печатать заново"><span class="ic">⏹</span>Стоп</button>
              </div>

              <div class="hero-speed-wrap">
                <div class="hero-speed-knob" data-hf="knob" data-pid="${esc(p.id)}"></div>
              </div>
            </div>
          </div>
        </div>

        <div class="hero-ams-block">
          <div class="hero-block-head"><b>Тракт AMS</b><small>подсвечена трубка, из которой станок печатает сейчас</small></div>
          <div data-hf="ams"></div>
        </div>
        <div class="hero-layers-block" data-hf="layers"></div>
      </div>`;
    if (window.PFAmsPath) PFAmsPath.mount(host.querySelector('[data-hf="ams"]'), p.ams, { compact: true });
    const knobEl = host.querySelector('[data-hf="knob"]');
    if (knobEl && window.PFKnob) {
      PFKnob.create(knobEl, {
        value: speedLvl,
        size: 88,
        onChange: (level) => {
          // Подтверждение уровня придёт с телеметрией (info.speed_level);
          // при отказе станка стрелка возвращается на прежнее деление.
          execHeroSpeed(p.id, level, null).then((ok) => { if (!ok && knobEl._knob) knobEl._knob.fail(); });
        },
      });
    }
    const layEl = host.querySelector('[data-hf="layers"]');
    if (layEl && window.PFLayers) {
      PFLayers.mount(layEl, { printerId: p.id, layer: info.layer });
    }
  }
}

/* ============================================== спидометр «прибыль за час» */
function renderGauge() {
  const host = $('dash_gauge');
  const s = (PF.state.finance && PF.state.finance.summary) || PF.state.summary || {};
  const value = num(s.profit_per_print_hour);
  const target = num(PF.state.settings.target_profit_per_hour, 250);
  $('dash_gauge_norm').textContent = money(target);
  const max = Math.max(target * 1.5, value * 1.15, 1);
  const L = Math.PI * 64;
  const valP = clamp(value / max, 0, 1);
  const tgtP = clamp(target / max, 0, 1);
  const kind = value >= target ? 'ok' : value >= target * 0.4 ? 'warn' : 'bad';
  const color = { ok: 'var(--ok)', warn: 'var(--warn)', bad: 'var(--bad)' }[kind];
  const tgtX = 80 + 52 * Math.cos(Math.PI * (1 - tgtP));
  const tgtY = 78 - 52 * Math.sin(Math.PI * (1 - tgtP));
  const note = value >= target ? 'норма выполнена'
    : value > 0 ? `до нормы ${money(target - value)}/ч` : 'пока нет прибыли за час';
  host.innerHTML = `<svg viewBox="0 0 160 96" class="gauge">`
    + `<path d="M 16 78 A 64 64 0 0 1 144 78" fill="none" stroke="var(--line)" stroke-width="13" stroke-linecap="round"/>`
    + `<path d="M 16 78 A 64 64 0 0 1 144 78" fill="none" stroke="${color}" stroke-width="13" stroke-linecap="round" stroke-dasharray="${(L * valP).toFixed(1)} ${L.toFixed(1)}"/>`
    + `<line x1="80" y1="78" x2="${tgtX.toFixed(1)}" y2="${tgtY.toFixed(1)}" stroke="var(--muted)" stroke-width="2" stroke-dasharray="3 3"/>`
    + `<text x="80" y="56" text-anchor="middle" class="gauge-val ${kind}">${money(value)}</text>`
    + `<text x="80" y="70" text-anchor="middle" class="gauge-unit">за час печати · за 30 дней</text>`
    + `<text x="80" y="92" text-anchor="middle" class="gauge-target">норма ${money(target)}/ч · ${esc(note)}</text>`
    + `</svg>`;
}

/* ====================================================== AMS на главной */
function amsHexToName(hex){
  hex=String(hex||'').trim().replace('#','');
  if(hex.length<6) return '';
  const r=parseInt(hex.slice(0,2),16),g=parseInt(hex.slice(2,4),16),b=parseInt(hex.slice(4,6),16);
  const mx=Math.max(r,g,b),mn=Math.min(r,g,b);
  if(mx-mn<30){ if(mx<60) return 'Чёрный'; if(mx>200) return 'Белый'; return 'Серый'; }
  if(r>=g&&r>=b) return g>90?'Оранжевый':'Красный';
  if(g>=r&&g>=b) return 'Зелёный';
  return 'Синий';
}
function renderAmsPanel() {
  const host = $('dash_ams');
  const live = PF.state.live;
  const snap = (live && (live.active || (live.printers || [])[0])) || null;
  const ams = (snap && snap.ams) || {};
  const trays = ams.trays || [];
  $('dash_ams_env').textContent = (ams.temperature != null || ams.humidity != null)
    ? `Температура ${ams.temperature ?? '—'} °C · влажность ${ams.humidity ?? '—'}`
    : 'Температура и влажность —';
  if (!trays.length) { host.innerHTML = dashEmpty('AMS не обнаружен или принтер не на связи.'); return; }
  const threshold = num(PF.state.settings.filament_low_threshold, 15);
  // ПР9: мини-стойка AMS — те же «трубки», что на вкладке «Принтеры», только мельче.
  host.innerHTML = `<div class="dp-rack">` + trays.map((t) => {
    const remain = t.remain == null || t.remain < 0 ? null : num(t.remain);
    const warn = remain != null && remain < threshold;
    const cname = amsHexToName(t.color) || '';
    const present = t.present !== false && (t.present || t.generic || t.type || t.uuid);
    const empty = !present;
    const typeLabel = empty ? 'пусто' : (t.type || '?');
    return `<div class="dp-tube${empty ? ' empty' : ''}${t.active ? ' active' : ''}${warn ? ' low' : ''}"`
      + ` title="${esc((t.label || 'Слот') + ' · ' + typeLabel + (cname && !empty ? ' · ' + cname : '') + (remain != null ? ' · ' + Math.round(remain) + '%' : ''))}">`
      + `<i style="--filament:${esc(t.color || '#cbd5e1')};--lvl:${empty ? 0 : (remain == null ? 100 : clamp(remain, 4, 100))}%"></i>`
      + `<span>${empty ? '' : (remain != null ? Math.round(remain) + '%' : '—')}</span></div>`;
  }).join('') + '</div>';
}

/* ============================================== прогноз пластика на очередь */
function renderFilamentForecast() {
  const host = $('dash_filament');
  const queue = (PF.state.jobs.queue || []).filter((j) => j.state === 'queued');
  const need = {};
  let jobsN = 0, totalNeed = 0;
  queue.forEach((j) => {
    let grams = num(j.grams);
    let mat = String(j.material || '').trim().toUpperCase();
    if (j.order_id) {
      const o = PF.state.orders.find((x) => x.id === j.order_id);
      if (o) {
        // У мультизаказа граммы — вся плита, на количество не умножаем.
        grams = num(o.grams) * (num(o.items_count) ? 1 : Math.max(1, num(o.qty, 1)));
        mat = String(o.material || mat).trim().toUpperCase();
      }
    }
    if (grams > 0) { need[mat || '—'] = (need[mat || '—'] || 0) + grams; jobsN++; totalNeed += grams; }
  });
  if (!Object.keys(need).length) {
    $('dash_filament_sub').textContent = 'Хватит ли катушек на задания';
    host.innerHTML = dashEmpty('Очередь пуста — прогноз не нужен.');
    return;
  }
  const avail = {};
  (PF.state.spools || []).forEach((sp) => {
    if (num(sp.archived)) return;
    const m = String(sp.material || '').trim().toUpperCase() || '—';
    avail[m] = (avail[m] || 0) + num(sp.remaining_grams);
  });
  $('dash_filament_sub').textContent = `${jobsN} заданий в очереди · нужно ~${nfmt(totalNeed)} г`;
  host.innerHTML = Object.keys(need).map((m) => {
    const n = need[m], a = num(avail[m]);
    const covered = a ? clamp(a / n * 100, 0, 100) : 0;
    const ok = a >= n;
    return `<div class="fl-row">`
      + `<div class="fl-info"><b>${esc(m || '—')}</b><small>нужно ${nfmt(n)} г</small></div>`
      + `<div class="fl-right"><div class="bar thin"><i style="width:${covered}%;background:${ok ? 'var(--ok)' : 'var(--bad)'}"></i></div>`
      + `<small class="${ok ? '' : 'neg'}">${a ? 'на складе ' + nfmt(a) + ' г' : 'нет на складе'}</small></div>`
      + '</div>';
  }).join('')
    + `<div class="fl-total">Всего в очереди ${nfmt(totalNeed)} г · на складе ${nfmt(Object.values(avail).reduce((a, b) => a + b, 0))} г</div>`;
}

/* =================================================== мастер-план производства */
async function refreshPlan() {
  try {
    const data = await get('/api/plan/day');
    PF.state.plan = data;
    if (document.querySelector('#view-dashboard.on')) renderPlan();
  } catch (e) { /* офлайн — не критично */ }
}
function renderPlan() {
  const host = $('dash_plan');
  if (!host) return;
  const p = PF.state.plan;
  if (!p) {
    $('dash_plan_sub').textContent = 'Что печатать следующим — заказы и пополнение полки';
    host.innerHTML = dashEmpty('План появится после загрузки данных.');
    return;
  }
  const load = clamp(num(p.load_pct), 0, 999);
  const barKind = p.verdict === 'bad' ? 'bad' : p.verdict === 'warn' ? 'warn' : '';
  $('dash_plan_sub').textContent = p.verdict_text || 'Что печатать следующим';
  const seq = p.sequence || [];
  const suggestedId = p.suggested_next ? p.suggested_next.id : null;
  let html = '<div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">'
    + `<div class="bar" style="flex:1;min-width:120px"><i class="${barKind}" style="width:${clamp(load, 0, 100)}%"></i></div>`
    + `<span class="chip ${barKind || 'ok'}">${nfmt(p.total_hours)} / ${nfmt(p.capacity_weekly)} ч в неделю</span></div>`;
  if (p.suggested_next) {
    const n = p.suggested_next;
    html += `<div class="plan-next-callout">▶ Следующее: ${n.kind === 'order' ? 'заказ' : 'полка'} · ${esc(n.title)}`
      + ` · ${hoursText(n.hours)}` + (n.due ? ` · до ${esc(dateText(n.due))}` : '') + '</div>';
  }
  if (!seq.length) {
    html += dashEmpty('Печатать нечего: очередь пуста, а полка не просит пополнения.');
  } else {
    html += seq.slice(0, 8).map((t) => {
      const isOrder = t.kind === 'order';
      const title = isOrder ? `${esc(String(t.ref || 'заказ'))} · ${esc(t.title)}` : `Полка · ${esc(t.title)}`;
      const parts = [];
      if (isOrder && t.customer) parts.push(esc(t.customer));
      if (isOrder && t.status) parts.push(esc(t.status));
      if (isOrder && t.due) parts.push('до ' + esc(dateText(t.due)));
      if (!isOrder) {
        parts.push(t.days_left != null ? 'запас ' + Math.round(t.days_left) + ' дн' : 'нет продаж');
        if (t.qty) parts.push('план ' + nfmt(t.qty) + ' шт');
      }
      const bad = (t.issues || []).filter((i) => i.level === 'bad').length;
      const warn = (t.issues || []).filter((i) => i.level === 'warn').length;
      const flag = bad ? ' ✕' : warn ? ' ⚠' : '';
      const titleAttr = (t.issues || []).length
        ? ` title="${esc(t.issues.map((i) => i.text).join('; '))}"` : '';
      return `<div class="tx-row${t.id === suggestedId ? ' plan-next' : ''}"${titleAttr}>`
        + `<span class="tx-ic ${isOrder ? 'income' : ''}">${isOrder ? '▦' : '▤'}</span>`
        + `<div class="tx-body"><b>${title}</b>`
        + (parts.length ? '<small>' + parts.join(' · ') + '</small>' : '')
        + '</div>'
        + `<span class="amt">${hoursText(t.hours)}${flag}</span></div>`;
    }).join('');
  }
  host.innerHTML = html;
}

/* ==================================================== здоровье бизнеса */
async function refreshInsights() {
  try {
    const data = await get('/api/insights');
    PF.state.insights = data;
    if (document.querySelector('#view-dashboard.on')) renderHealth();
  } catch (e) { /* офлайн — не критично */ }
}
function renderHealth() {
  const host = $('dash_health');
  if (!host) return;
  const ins = PF.state.insights;
  if (!ins) {
    $('dash_health_sub').textContent = 'Цель месяца, касса вперёд и налоги';
    host.innerHTML = dashEmpty('Показатели появятся после загрузки данных.');
    return;
  }
  const goal = ins.goal || {};
  const cash = ins.cash || {};
  const tax = ins.tax || {};
  $('dash_health_sub').textContent = goal.verdict_text || 'Цель месяца, касса вперёд и налоги';

  const gp = clamp(num(goal.pct), 0, 100);
  const goalKind = goal.verdict === 'bad' ? 'bad' : goal.verdict === 'warn' ? 'warn' : 'ok';
  const cashKind = cash.verdict === 'bad' ? 'bad' : cash.verdict === 'warn' ? 'warn' : 'ok';
  const nxt = (tax.events || [])[0];
  const limitWarn = num(tax.limit_used) >= 80;

  let html = '<div class="health-grid">';

  html += '<div class="health-cell">'
    + '<span class="h-label">Цель месяца</span>'
    + `<div class="bar ${goalKind}" style="margin:6px 0 4px"><i style="width:${clamp(gp, 0, 100)}%"></i></div>`
    + `<b>${money(goal.profit)}</b><span class="muted"> из ${money(goal.goal)} · ${pct(goal.pct)}</span>`
    + `<small class="muted" style="display:block">темп ведёт к ${money(goal.projected)}</small></div>`;

  const pts = (cash.points || []).map((p) => p.cash);
  const minC = Math.min(...pts.map((v) => num(v)));
  const maxC = Math.max(...pts.map((v) => num(v)), 1);
  html += '<div class="health-cell">'
    + '<span class="h-label">Касса вперёд, 90 дней</span>'
    + `<div class="health-cash ${cashKind}">`
    + pts.map((v) => {
      const h = clamp(14 + (num(v) - minC) / Math.max(1, maxC - minC) * 26, 4, 40);
      return `<i style="height:${h}px" title="${money(v)}"></i>`;
    }).join('')
    + `</div><b>${money(cash.now)}</b><span class="muted"> сейчас${cash.runway_days != null ? ' · запас ' + Math.round(cash.runway_days) + ' дн' : ''}</span></div>`;

  html += '<div class="health-cell">'
    + '<span class="h-label">Налоги и лимит</span>'
    + (nxt
      ? `<b>${esc(nxt.title)}</b><small class="muted" style="display:block">${esc(nxt.due ? dateText(nxt.due) : '')} · ${money(nxt.amount)}</small>`
      : '<b>Ближайших платежей нет</b>')
    + (num(tax.limit) ? `<div class="bar ${limitWarn ? 'warn' : ''}" style="margin-top:6px"><i style="width:${clamp(num(tax.limit_used), 0, 100)}%"></i></div>`
      + `<small class="muted" style="display:block">лимит режима ${pct(tax.limit_used)}` +
        (tax.limit_days != null ? ` · хватит на ~${Math.round(tax.limit_days)} дн` : '') + '</small>'
      : '<small class="muted" style="display:block">режим без лимита</small>')
    + '</div>';

  html += '</div>';
  host.innerHTML = html;
}

/* ==================================================== таймлайн печати за день */
async function refreshTimeline() {
  try {
    const data = await get('/api/timeline', { day: U.todayISO() });
    PF.state.timeline = data.jobs || [];
    if (document.querySelector('#view-dashboard.on')) renderTimeline();
  } catch (e) { /* офлайн — не критично */ }
}
function renderTimeline() {
  const host = $('dash_timeline');
  const jobs = PF.state.timeline || [];
  if (!jobs.length) {
    $('dash_timeline_sub').textContent = 'Сегодня печатей ещё не было';
    host.innerHTML = dashEmpty('Сегодня печатей ещё не было.');
    return;
  }
  const runningIds = (PF.state.jobs.queue || []).filter((j) => j.state === 'running').map((j) => j.id);
  const rows = jobs.map((j) => {
    const start = j.started_at || j.queued_at || j.created_at || '';
    const isRunning = j.state === 'running' || j.state === 'starting' || runningIds.includes(j.id);
    const end = j.finished_at || (isRunning ? null : start);
    const dur = num(j.duration_min) || (start && end ? (new Date(end) - new Date(start)) / 60000 : 0);
    return Object.assign({}, j, { start, end, dur, isRunning });
  });
  let idle = 0;
  for (let i = 1; i < rows.length; i++) {
    const a = rows[i - 1], b = rows[i];
    if (a.printer_id && a.printer_id === b.printer_id && a.end && b.start) {
      const gap = (new Date(b.start) - new Date(a.end)) / 60000;
      if (gap > 1 && gap < 24 * 60) idle += gap;
    }
  }
  const doneN = rows.filter((r) => r.state === 'done').length;
  const failN = rows.filter((r) => r.state === 'failed').length;
  $('dash_timeline_sub').textContent = `${rows.length} заданий · ${doneN} готово${failN ? ' · ' + failN + ' брак' : ''} · простой между ними ${minutesText(idle)}`;
  const byPrinter = {};
  rows.forEach((r) => { (byPrinter[r.printer_id || '—'] = byPrinter[r.printer_id || '—'] || []).push(r); });
  const stMap = { done: ['ok', '✓'], failed: ['bad', '✕'], cancelled: ['', '○'], running: ['accent', '▶'], starting: ['accent', '▶'] };
  host.innerHTML = Object.keys(byPrinter).map((pid) => {
    const list = byPrinter[pid];
    const name = PF.printer(pid) ? PF.printer(pid).name : 'Принтер';
    return `<div class="tl-group"><div class="tl-pname">${esc(name)}</div>`
      + list.map((j) => {
        const t0 = j.start ? String(j.start).slice(11, 16) : '—';
        const t1 = j.end ? String(j.end).slice(11, 16) : (j.isRunning ? '…' : '—');
        const [stk, sti] = stMap[j.state] || ['', '•'];
        const ord = j.order ? `№${j.order.number} · ${j.order.product}` : (j.name || j.file || 'Печать');
        return `<div class="tl-row">`
          + `<span class="tx-ic ${stk}">${sti}</span>`
          + `<div class="tx-body"><b>${esc(ord)}</b>`
          + `<small>${t0}—${t1} · ${nfmt(j.grams)} г · ${minutesText(j.duration_min)}</small></div>`
          + `<span class="amt">${esc(String(j.result || j.state || ''))}</span></div>`;
      }).join('')
      + '</div>';
  }).join('');
}

/* ================================================= браузерные уведомления */
const NOTIFY_KINDS = new Set(['complete', 'error', 'guard', 'filament_low',
  'maintenance', 'loss', 'defect', 'pause']);
// События, клик по которым ведёт в раздел принтеров (а не просто фокусирует окно).
const NOTIFY_PRINTER_KINDS = new Set(['complete', 'error', 'guard', 'pause']);
let notifyLastId = 0;
let notifySeeded = false;
function initBrowserNotify() {
  if (!('Notification' in window)) return;
  if (Notification.permission === 'granted' && PF.state.settings.browser_notify_enabled) {
    // Страховочный опрос остаётся: если SSE не работает (прокси), события
    // догоняем по журналу. При живом SSE срабатывает мгновенно через notifyEvent.
    setInterval(checkNewEvents, 30000);
  }
  // Подписка на события: SSE кладёт запись в журнал — показываем сразу.
  PF.on('notify', (row) => notifyEvent(row));
}
function notifyEnabled() {
  return 'Notification' in window && Notification.permission === 'granted'
    && PF.state.settings.browser_notify_enabled;
}
function notifyEvent(e) {
  if (!e || !notifyEnabled()) return;
  const id = Number(e.id) || 0;
  if (!id) return;
  if (!notifySeeded) { notifyLastId = id; notifySeeded = true; return; } // стартовый прогон
  if (id <= notifyLastId || !NOTIFY_KINDS.has(e.kind)) return;
  notifyLastId = id;
  try {
    const icon = NOTIFY_PRINTER_KINDS.has(e.kind) && e.printer_id
      ? `/api/printer/camera.jpg?printer_id=${encodeURIComponent(e.printer_id)}`
      : '/assets/brand/nozza-mark.svg';
    const n = new Notification('PrintFlow · ' + (e.title || 'событие'), {
      body: String(e.detail || ''),
      tag: 'pf-' + e.id,
      icon,
    });
    n.onclick = () => {
      window.focus();
      if (NOTIFY_PRINTER_KINDS.has(e.kind) && typeof PF.go === 'function') PF.go('printers');
      n.close();
    };
  } catch (err) { /* уведомления не критичны */ }
}
function checkNewEvents() {
  if (!notifyEnabled()) return;
  const list = PF.state.events || [];
  if (!list.length) return;
  const maxId = Math.max(...list.map((e) => Number(e.id) || 0));
  if (!notifySeeded) { notifyLastId = maxId; notifySeeded = true; return; }
  // Догоняем только то, что не пришло по SSE.
  list.filter((e) => Number(e.id) > notifyLastId && NOTIFY_KINDS.has(e.kind))
    .forEach(notifyEvent);
  notifyLastId = maxId;
}
function requestBrowserNotify() {
  if (!('Notification' in window)) return fail(new Error('Браузер не поддерживает уведомления'));
  if (Notification.permission === 'granted') return toast('Уведомления уже разрешены');
  Notification.requestPermission().then((p) => {
    if (p === 'granted') toast('Уведомления включены', 'События будут приходить сразу');
    else toast('Уведомления не разрешены', 'Можно включить в настройках браузера', 'warn');
  });
}

/* =========================================================== настройки */
const RATES = [
  ['energy_price', 'Цена электричества, ₽/кВт·ч', 'Тариф за электроэнергию по вашему договору', 'select', [
    [4.5, '4.50 ₽ (льготный тариф)'],
    [5.5, '5.50 ₽'],
    [6.0, '6.00 ₽ (стандартный тариф)'],
    [7.0, '7.00 ₽'],
    [8.5, '8.50 ₽ (коммерческий тариф)'],
  ]],
  ['power_kw', 'Мощность принтера, кВт', 'P1S в активной печати потребляет ~0.15 кВт', 'num', 0.01],
  ['amortization_per_hour', 'Амортизация оборудования, ₽/ч', 'Износ станка за каждый час печати', 'num', 1],
  ['maintenance_per_hour', 'Обслуживание станка, ₽/ч', 'Сопла, ремни, смазка и расходники', 'num', 1],
  ['target_profit_per_hour', 'Норма прибыли за час, ₽', 'Порог окупаемости машино-часа станка', 'select', [
    [200, '200 ₽/ч (базовый)'],
    [300, '300 ₽/ч'],
    [400, '400 ₽/ч (стандарт)'],
    [500, '500 ₽/ч'],
    [800, '800 ₽/ч (высокая маржа)'],
  ]],
  ['labor_rate', 'Стоимость часа оператора, ₽', 'Время на снятие детали, постобработку и заправку', 'num', 50],
  ['failure_rate', 'Резерв на брак, %', 'Закладывается в себестоимость деталей', 'select', [
    [2, '2% (очень стабильный цех)'],
    [5, '5% (стандарт NOZZA)'],
    [8, '8%'],
    [10, '10% (сложная печать / новые материалы)'],
  ]],
];

const STORAGE_RULES = [
  ['filament_low_threshold', 'Порог низкого остатка катушки, %', 'Предупреждать в Telegram и панели, когда остаток ниже порога', 'select', [
    [10, '10% (минимальный резерв)'],
    [15, '15% (по умолчанию)'],
    [20, '20% (рекомендуется для больших деталей)'],
    [25, '25% (раннее оповещение)'],
    [30, '30% (серийное производство)'],
  ]],
  ['auto_consume_filament', 'Автосписание пластика по завершении', 'Списывать фактический вес детали с катушки, стоявшей в AMS', 'bool'],
  ['default_location', 'Место хранения по умолчанию', 'Основное место для прихода новых катушек', 'select', [
    ['shop', 'Магазин / Основной склад'],
    ['home', 'Дом / Мастерская'],
    ['dry', 'Сушильный шкаф'],
    ['other', 'Другое место'],
  ]],
  ['default_spool_weight', 'Вес катушки по умолчанию, г', 'Стандартная масса пластика в новой бобине', 'select', [
    [1000, '1 000 г (стандартная бобина 1 кг)'],
    [750, '750 г'],
    [500, '500 г'],
    [250, '250 г (пробник)'],
  ]],
  ['default_spool_price', 'Цена катушки по умолчанию, ₽', 'Используется для быстрой оценки себестоимости', 'select', [
    [1200, '1 200 ₽'],
    [1400, '1 400 ₽'],
    [1600, '1 600 ₽ (средний PLA / PETG)'],
    [1800, '1 800 ₽'],
    [2200, '2 200 ₽ (инженерный пластик)'],
    [2600, '2 600 ₽ (композиты)'],
  ]],
  ['restock_remind', 'Напоминать о закупке пластика', 'Ежедневная сводка по катушкам ниже порога', 'bool'],
  ['ams_auto_spools', 'Заводить катушки из AMS автоматически', 'Вставили бобину в AMS — она появилась на складе', 'bool'],
  ['ams_sync_remaining', 'Обновлять остаток по датчику AMS', 'Только для катушек с флагом синхронизации с AMS', 'bool'],
  ['dry_humidity_threshold', 'Порог влажности AMS для сушки, %', 'Выше порога — событие сушки и оповещение', 'select', [
    [20, '20% (строгий контроль)'],
    [25, '25%'],
    [30, '30% (стандарт AMS)'],
    [40, '40% (допустимый для PLA)'],
  ]],
];

const GOALS = [
  ['goal_profit_month', 'Цель по прибыли в месяц, ₽', 'От неё считается план продаж', 'num', 1000],
  ['printer_investment', 'Во сколько обошёлся принтер, ₽', 'Для расчёта окупаемости (виджет «Здоровье бизнеса»)', 'num', 10000],
];
const COMPANY = [
  ['company_name', 'Название бренда', 'Подставляется в материалы и документы', 'text'],
  ['legal_name', 'Юридическое имя', 'ИП Иванов И. И. — для счетов и чеков', 'text'],
  ['inn', 'ИНН', 'Нужен для счетов B2B', 'text'],
  ['currency', 'Валюта', 'Символ рядом с суммами', 'text'],
];
const TAX_FIELDS = {
  npd: [
    ['npd_rate_person', 'Ставка с продаж физлицам, %', 'По закону 4%', 'num', 0.5],
    ['npd_rate_company', 'Ставка с продаж юрлицам, %', 'По закону 6%', 'num', 0.5],
    ['npd_limit', 'Годовой лимит дохода, ₽', 'На НПД — 2 400 000 ₽', 'num', 100000],
    ['npd_bonus_left', 'Остаток налогового вычета, ₽', 'Стартовые 10 000 ₽ уменьшают ставки', 'num', 500],
  ],
  usn6: [
    ['usn_income_rate', 'Ставка налога, %', 'Обычно 6%, в регионах бывает меньше', 'num', 0.5],
    ['usn_limit', 'Лимит дохода на УСН, ₽', 'С 2026 года — 490,5 млн ₽', 'num', 1000000],
  ],
  usn15: [
    ['usn_profit_rate', 'Ставка налога, %', 'Обычно 15% с прибыли', 'num', 0.5],
    ['usn_min_tax_rate', 'Минимальный налог, %', '1% с дохода, если обычный налог меньше', 'num', 0.5],
    ['usn_limit', 'Лимит дохода на УСН, ₽', 'С 2026 года — 490,5 млн ₽', 'num', 1000000],
  ],
  patent: [
    ['patent_cost_year', 'Стоимость патента за год, ₽', 'Из уведомления налоговой', 'num', 1000],
  ],
  manual: [
    ['tax_rate', 'Своя ставка, %', 'Просто процент с оборота', 'num', 0.5],
  ],
  none: [],
};
const INSURANCE = [
  ['insurance_fixed', 'Фиксированные взносы за год, ₽', 'Для ИП в 2026 году — 57 390 ₽', 'num', 100],
  ['insurance_extra_rate', 'Дополнительный взнос, %', '1% с дохода свыше порога', 'num', 0.5],
  ['insurance_extra_base', 'Порог для 1%, ₽', 'Обычно 300 000 ₽', 'num', 10000],
  ['insurance_extra_cap', 'Максимум дополнительного взноса, ₽', 'В 2026 году — 321 818 ₽', 'num', 1000],
  ['insurance_reduces_tax', 'Уменьшать налог на взносы', 'ИП без сотрудников — вплоть до нуля', 'bool'],
];
const VAT = [
  ['vat_threshold', 'Порог внимания к НДС, ₽', 'При приближении система предупредит; сам НДС автоматически не рассчитывается', 'num', 1000000],
  ['tax_reserve_enabled', 'Считать резерв под налог', 'Если выключено, рекомендуемый резерв равен нулю', 'bool'],
  ['tax_reserve_extra', 'Запас сверх ставки, %', 'Чтобы точно хватило', 'num', 0.5],
];
const PRICING = [
  ['default_markup', 'Наценка к себестоимости, %', 'Базовый процент маржи для подсказки цены изделия', 'select', [
    [50, '50% (минимальная)'],
    [100, '100% (в 2 раза)'],
    [150, '150% (в 2.5 раза, стандарт NOZZA)'],
    [200, '200% (в 3 раза)'],
    [250, '250%'],
    [300, '300% (в 4 раза)'],
  ]],
  ['min_order_price', 'Минимальный чек, ₽', 'Ниже этой суммы браться за печать невыгодно', 'select', [
    [150, '150 ₽'],
    [200, '200 ₽'],
    [300, '300 ₽ (стандарт)'],
    [500, '500 ₽'],
    [1000, '1 000 ₽'],
  ]],
  ['price_rounding', 'Округление цены, ₽', 'Округлять итоговую цену клиенту вверх до кратной суммы', 'select', [
    [1, '1 ₽ (без округления)'],
    [5, 'до 5 ₽'],
    [10, 'до 10 ₽ (стандарт)'],
    [50, 'до 50 ₽'],
    [100, 'до 100 ₽'],
  ]],
  ['design_rate', 'Моделирование и подготовка, ₽/ч', 'Оплата за подготовку или доработку 3D-модели', 'num', 50],
];
const DISCOUNTS = [
  ['bulk_discount_10', 'Скидка от 10 шт, %', 'Автоматически рассчитывается в оптовой партии', 'select', [
    [0, '0% (без скидки)'],
    [5, '5%'],
    [10, '10% (стандарт)'],
    [15, '15%'],
    [20, '20%'],
  ]],
  ['bulk_discount_50', 'Скидка от 50 шт, %', 'Для крупных серийных тиражей', 'select', [
    [0, '0% (без скидки)'],
    [10, '10%'],
    [15, '15%'],
    [20, '20% (стандарт)'],
    [25, '25%'],
    [30, '30%'],
  ]],
  ['rush_surcharge', 'Надбавка за срочность, %', 'Когда заказ нужен вне очереди («на вчера»)', 'select', [
    [0, '0% (без наценки)'],
    [20, '20%'],
    [30, '30% (стандарт)'],
    [50, '50% (х1.5)'],
    [100, '100% (х2)'],
  ]],
];
const PAYMENTS = [
  ['acquiring_fee', 'Комиссия эквайринга, %', 'Банковская комиссия за приём карт и СБП', 'select', [
    [0, '0% (без комиссии)'],
    [0.7, '0.7% (СБП льготный)'],
    [1.0, '1.0% (СБП стандарт)'],
    [1.5, '1.5%'],
    [2.0, '2.0%'],
    [2.5, '2.5% (интернет-эквайринг)'],
    [3.0, '3.0%'],
  ]],
  ['delivery_cost', 'Доставка на заказ, ₽', 'Средние затраты, если доставку оплачивает ферма', 'num', 10],
  ['packaging_cost', 'Упаковка на заказ, ₽', 'Коробка, пузырчатая плёнка, брендированный стикер', 'num', 5],
];
const MONEY_RULES = [
  ['count_labor_in_cost', 'Считать свою работу расходом', 'По умолчанию выключено: ваш час — это прибыль', 'bool'],
  ['allocate_fixed_costs', 'Разносить постоянные расходы на заказы', 'Добавляет долю аренды и подписок в себестоимость', 'bool'],
  ['fixed_costs_auto', 'Начислять постоянные расходы автоматически', 'Проводки создаются по расписанию', 'bool'],
  ['debt_alert_days', 'Долг считается просроченным через, дней', 'После этого срока подсветим красным', 'num', 1],
  ['debt_reminder_cooldown_days', 'Пауза между напоминаниями, дней', 'Защищает клиента от случайных повторов', 'num', 1],
  ['feedback_delay_days', 'Просить отзыв после выдачи, дней', 'До срока заказ остаётся в плане после продажи', 'num', 1],
  ['low_margin_alert', 'Предупреждать при марже ниже, %', 'Заказ подсветится как невыгодный', 'num', 1],
  ['envelope_auto', 'Откладывать % с дохода в конверты', 'Конверты ниже: налог, пластик, принтер', 'bool'],
];
const AUTOS = [
  ['auto_accounting', 'Автоматический учёт', 'Считать себестоимость по фактам печати'],
  ['auto_link_orders', 'Связывать печать с заказом', 'По имени файла и номеру заказа'],
  ['auto_consume_filament', 'Списывать пластик', 'С катушки, которая стояла в AMS'],
  ['auto_queue', 'Автозапуск очереди', 'Следующее задание стартует само только при включённом safety-gate'],
  ['auto_resume_paused', 'Авто-resume после сбоя питания', 'Продолжает только печать с явным marker восстановления питания от принтера; ручную паузу и сетевой обрыв не трогает', 'bool'],
  ['auto_resume_max_delay_minutes', 'Окно восстановления, мин', 'Не продолжать старую печать после этого срока; 0 — без ограничения', 'num', 1],
  ['unattended_dangerous_actions', 'Разрешить опасные действия без оператора', 'Safety-gate для автозапуска, расписаний, нагрева и подачи филамента. Power-loss recovery имеет отдельную строгую политику.', 'bool'],
];
const NOTIFY = [
  ['notify_complete', 'Завершение печати', 'Сообщение, когда задание дошло до конца: имя файла, время и вес пластика'],
  ['notify_error', 'Ошибки и HMS', 'Коды ошибок принтера и сбои задания — чтобы реагировать, пока брак не разросся'],
  ['notify_pause', 'Пауза', 'Печать встала на паузу (вручную или по сторожу) — уведомление придёт сразу'],
  ['notify_filament_low', 'Пластик заканчивается', 'Остаток катушки упал ниже порога из блока «Склад и пластик»'],
  ['notify_guard', 'Тревоги сторожа печати', 'Срабатывания защиты: перегрев, зависание, спагетти-детектор'],
  ['notify_maintenance', 'Пора обслужить принтер', 'Регламент ТО: смазка, протяжка, чистка — по наработке часов парка'],
  ['notify_photo', 'Прикладывать кадр с камеры', 'К уведомлениям о событиях прикладывается снимок с камеры принтера'],
];
const AUTO_EXTRA = [
  ['weekly_capacity_hours', 'Сколько часов печати в неделю', 'Реальный потолок парка станков', 'select', [
    [40, '40 ч (1 принтер, 1 смена)'],
    [80, '80 ч (2 принтера / удлинённая смена)'],
    [120, '120 ч (полукруглосуточно)'],
    [168, '168 ч (круглосуточно 24/7)'],
    [336, '336 ч (2 станка 24/7)'],
    [500, '500 ч (ферма 3+ станка)'],
  ]],
  ['notify_finish_remind_min', 'Напомнить о финише за, мин', 'Оповещение оператору перед окончанием печати', 'select', [
    [0, '0 — выключено'],
    [5, 'за 5 минут'],
    [10, 'за 10 минут (стандарт)'],
    [15, 'за 15 минут'],
    [30, 'за 30 минут'],
  ]],
  ['digest_time', 'Утренний дайджест, время', 'Ежедневная сводка по задачам и станкам в Telegram', 'select', [
    ['08:00', '08:00'], ['08:30', '08:30'], ['09:00', '09:00 (стандарт)'], ['09:30', '09:30'], ['10:00', '10:00'],
  ]],
  ['weekly_report_day', 'День недельного отчёта', 'День недели для отправки сводки цеха', 'select', [
    [1, '1 — Понедельник'],
    [5, '5 — Пятница (конец недели)'],
    [7, '7 — Воскресенье (вечер)'],
  ]],
  ['weekly_report_time', 'Время недельного отчёта', 'Время отправки недельного отчёта', 'select', [
    ['18:00', '18:00'], ['19:00', '19:00'], ['20:00', '20:00 (стандарт)'], ['21:00', '21:00'],
  ]],
];
const GUARD = [
  ['guard_enabled', 'Сторож печати', 'Следит за ошибками, зависанием и температурой', 'bool'],
  ['guard_pause_on_error', 'Ставить на паузу при ошибке', 'Спасает деталь и пластик, пока вас нет', 'bool'],
  ['guard_snapshot', 'Сохранять кадр при тревоге', 'Видно, что случилось, даже задним числом', 'bool'],
  ['guard_stall_minutes', 'Прогресс не растёт, мин', 'Через сколько считать печать зависшей', 'num', 1],
  ['guard_cold_minutes', 'Сопло не догревается, мин', 'Сколько ждать выхода на температуру', 'num', 1],
  ['guard_count_loss', 'Считать убыток от брака', 'Потраченный пластик и электричество — в расходы', 'bool'],
  ['guard_cost_limit', 'Лимит стоимости печати, ₽', 'Пауза, если живая себестоимость перешла порог (0 — выключено)', 'num', 1],
  ['guard_overrun_pct', 'Перерасход пластика, %', 'Тревога, если расход превысил смету слайсера (0 — выключено)', 'num', 1],
  ['spaghetti_enabled', 'Спагетти-детект по камере', 'Ловит «мешанину» в кадре и ставит печать на паузу (нужен pillow)', 'bool'],
  ['spaghetti_sensitivity', 'Чувствительность детекта, ×', 'Во сколько раз кромки должны превысить норму (2 — строже, 5 — мягче)', 'num', 0.5],
];
const QUEUE_RULES = [
  ['queue_check_filament', 'Проверять остаток пластика', 'Не запускать печать, если на катушке меньше граммов, чем нужно', 'bool'],
  ['queue_group_material', 'Группировать очередь по материалу', 'Меньше перезаправок катушек и промывки сопла подряд', 'bool'],
  ['quiet_hours_enabled', 'Тихие часы (ночной режим)', 'Ночью автозапуск очереди откладывается до утра', 'bool'],
  ['quiet_from', 'Тишина с (время)', 'Начало тихих часов', 'select', [
    ['20:00', '20:00'], ['21:00', '21:00'], ['22:00', '22:00'], ['23:00', '23:00 (стандарт)'], ['00:00', '00:00'],
  ]],
  ['quiet_to', 'Тишина до (время)', 'Окончание тихих часов', 'select', [
    ['06:00', '06:00'], ['07:00', '07:00'], ['08:00', '08:00 (стандарт)'], ['09:00', '09:00'], ['10:00', '10:00'],
  ]],
];
const UPKEEP = [
  ['maintenance_enabled', 'Регламент обслуживания (ТО)', 'Напоминать о ТО по наработке часов печати', 'bool'],
  ['telemetry_enabled', 'История показателей станка', 'Графики температур стола, сопла и обдува', 'bool'],
  ['telemetry_keep_days', 'Хранить историю, дней', 'Глубина хранения точек (старые удаляются сами)', 'select', [
    [7, '7 дней'],
    [14, '14 дней'],
    [30, '30 дней (стандарт)'],
    [60, '60 дней'],
    [90, '90 дней'],
  ]],
  ['night_shift_enabled', 'Ночная смена', 'Планировать длинные задания на ночь, срочные — на день', 'bool'],
  ['auto_backup_days', 'Автобэкап базы, раз в N дней', 'Резервная копия базы данных по расписанию (0 — выключен)', 'select', [
    [0, '0 — выключен'],
    [1, '1 день (каждые сутки)'],
    [3, '3 дня'],
    [7, '7 дней (раз в неделю)'],
    [30, '30 дней'],
  ]],
];
const WATCH = [
  ['watch_folder_enabled', 'Watch Folder — авто-импорт 3MF', 'Следить за локальной папкой и автоматически подхватывать 3MF из слайсера', 'bool'],
  ['watch_auto_action', 'Действие при обнаружении файла', 'Что делать с новым файлом 3MF', 'select', [
    ['notify', 'notify — Только уведомить оператора'],
    ['queue', 'queue — Сразу поставить задание в очередь'],
  ]],
  ['watch_folder_path', 'Путь к локальной папке 3MF', 'Папка, куда слайсер сохраняет файлы для фермы', 'text'],
  ['watch_link_order', 'Связывать с заказом по номеру', 'Искать номер заказа в названии 3MF файла', 'bool'],
  ['watch_create_order', 'Создавать черновик заказа', 'Если заказ не найден — создать новую карточку', 'bool'],
];
/* FarmLoop (18.7): конвейер вернулся в настройки — карточка пропала при
   пересборке настроек в 18.6.3. Подписи safety-gate честно говорят, что это
   допуск на работу без человека: пока галочки не стоят, авто-режимы сами
   не включатся (гейт проверяется на сервере, settings_schema.validate). */
// Группа FarmLoop (18.8) переехала в раздел «Конвейер» (conveyor.js):
// настройки живут там, где конвейер настраивают, а не в свалке «Настройки».
const STUDIO = [
  ['studio_gateway_enabled', 'Шлюз Bambu Studio (Studio Gateway)', 'Studio находит PrintFlow как принтер в LAN. Slice/Print падает в очередь с preflight и AMS-map.', 'bool'],
  ['studio_gateway_mode', 'Режим обработки заданий', 'confirm — подтверждение на пульте/ПК; queue — сразу в очередь; autostart — печать сразу', 'select', [
    ['confirm', 'confirm — Окно подтверждения на пульте/ПК (безопасно)'],
    ['queue', 'queue — Сразу отправлять в очередь печати'],
    ['autostart', 'autostart — Автостарт (печатать немедленно)'],
  ]],
  ['studio_gateway_name', 'Имя виртуального принтера', 'Как PrintFlow называется в списке устройств Bambu Studio', 'text'],
  ['studio_gateway_host', 'Адрес шлюза в сети (пусто — авто)', 'Какой адрес объявлять Studio в SSDP и PASV. VirtualBox/Hyper-V/Docker/VPN подсовывают виртуальный IP — закрепите реальный, например 192.168.1.50. Порты Studio меняет нельзя: опрос :3000/:3002, команды :8883, файл :990', 'text'],
  ['studio_gateway_autostart', 'Автостарт со шлюза', 'Печатать сразу после Slice/Print (требует режим autostart и safety-gate)', 'bool'],
  ['studio_gateway_serial', 'Серийный номер устройства', 'Пусто — сгенерируется автоматически', 'text'],
  // 18.11: ретранслятор SSDP — Studio в другой подсети/VLAN видит реальные станки через PrintFlow.
  ['studio_relay_enabled', 'Ретранслятор станков для Studio', 'PrintFlow объявляет Studio реальные принтеры фермы (адрес + серийник из вкладки «Принтеры»). Включайте, если Studio на другом компьютере не видит станки сама', 'bool'],
  ['studio_relay_targets', 'Компьютеры со Studio (IPv4 через запятую)', 'Получают объявления шлюза и станков напрямую на UDP :2021 — через маршрутизатор, где broadcast не ходит. Пусто — только своя подсеть', 'text'],
];
const FTPS = [
  ['ftps_timeout', 'Таймаут FTPS, сек', 'Время ожидания ответа SD-карты станка (порт 990)', 'select', [
    [15, '15 сек'],
    [30, '30 сек (рекомендуется)'],
    [60, '60 сек (для больших 3MF)'],
    [120, '120 сек'],
  ]],
  ['ftps_retries', 'Повторы загрузки FTPS', 'Количество автоматических повторов при обрыве', 'select', [
    [1, '1 попытка (без повторов)'],
    [3, '3 повтора (стандарт)'],
    [5, '5 повторов'],
  ]],
  ['ftps_block_kb', 'Размер блока загрузки, КБ', 'Размер пакета передачи по FTPS (порт 990)', 'select', [
    [16, '16 КБ (надёжно при слабом Wi-Fi)'],
    [32, '32 КБ (сбалансированно)'],
    [64, '64 КБ (быстро в хорошей сети)'],
    [128, '128 КБ'],
  ]],
];
const MQTT = [
  ['mqtt_keepalive', 'Keepalive MQTT, сек', 'Интервал heartbeat-проверки связи со станком (порт 8883)', 'select', [
    [5, '5 сек (быстрый отклик)'],
    [15, '15 сек (стандарт)'],
    [30, '30 сек'],
    [60, '60 сек (экономия)'],
  ]],
  ['mqtt_backoff', 'Backoff переподключений', 'Увеличивать паузу после повторных сетевых сбоев', 'bool'],
];
const AMS_SETTINGS = [
  ['ams_auto_spools', 'Заводить катушки из AMS автоматически', 'Вставили бобину в AMS — она появилась на складе', 'bool'],
  ['ams_sync_remaining', 'Обновлять остаток по датчику AMS', 'Только для катушек с флагом синхронизации с AMS', 'bool'],
];
const PREFLIGHT = [
  ['preflight_enabled', 'Preflight — проверка перед стартом', 'Блокировать старт при проблемах', 'bool'],
  ['preflight_block_material', 'Блок: не тот материал в AMS', 'PLA вместо PETG — брак', 'bool'],
  ['preflight_block_filament', 'Блок: мало пластика', 'Сверять граммы с остатком катушки', 'bool'],
  ['preflight_block_hms', 'Блок: HMS ошибки', 'Не давать старт при ошибке принтера', 'bool'],
  ['preflight_block_bed', 'Блок: стол не пуст', 'До старта сравнить кадр с эталоном пустого стола. Нет эталона — проверка выключена.', 'bool'],
  ['preflight_warn_nozzle', 'Предупр.: сопло', 'Диаметр сопла в файле vs принтер', 'bool'],
  ['preflight_warn_humidity', 'Предупр.: влажность AMS', 'Выше порога — сушить', 'bool'],
];
/* 8.5: умный цех — камера и виртуальный принтер */
const PHASE11 = [
  ['keyframe_interval_min', 'Кейфреймы, мин', 'Каждые N минут — кадр в архив задания (0 = выключено, мин. 0.5)', 'num', 0.5],
  ['first_layer_watch_min', 'Смотреть первый слой, мин', 'Первые N минут после старта сверяем кадр со столом (0 = выключено)', 'num', 1],
  ['bed_watch_enabled', 'Проверка «деталь на столе»', 'После финиша кадр сравнивается с эталоном пустого стола', 'bool'],
  ['bed_watch_threshold', 'Порог пустого стола, %', 'Разница кадров, выше которой «стол не пуст»', 'num', 1],
  ['demo_printer_enabled', 'Виртуальный принтер P1S', 'Симулятор для тестов и демо: очередь, телеметрия, камера-демо', 'bool'],
  ['demo_speed', 'Скорость демо, мин/с', 'Насколько быстрее реального времени идёт виртуальная печать', 'num', 1],
];
const SYSTEM2 = [
  ['public_url', 'Адрес для QR', 'Пусто — LAN IP компьютера. Пример: http://192.168.1.50:8765', 'text'],
  ['encrypt_access_code', 'Шифровать Access Code', 'Рекомендуется: код хранится отдельно от ключа шифрования', 'bool'],
  ['backup_keep', 'Хранить бэкапов', 'Единый лимит для ручных, автоматических и страховочных копий', 'num', 1],
];
const ACCENTS = [
  ['indigo', '#4f46e5'], ['violet', '#7c3aed'], ['blue', '#2563eb'],
  ['emerald', '#059669'], ['amber', '#d97706'], ['rose', '#e11d48'],
];

function settingRow(key, label, sub, control) {
  return `<div class="set-row" data-set-row><div class="sinfo"><b>${esc(label)}</b><small>${esc(sub || '')}</small></div>${control}</div>`;
}

/* 18.12.2: адрес Mini App цеха. Раньше незаполненный адрес молча подменялся на
   example.com — кнопка «🏭 Открыть цех» открывала страницу «Example Domain».
   Здесь считаем тот же адрес, что и сервер (staffbot/core/config.py), чтобы
   владелец видел его до нажатия кнопки, а не после. */
function miniappTarget(s) {
  const direct = String(s.staff_miniapp_url || '').trim();
  const base = direct || String(s.public_url || '').trim();
  if (!base) {
    return { url: '', problem: 'Адрес не задан — кнопка «🏭 Открыть цех» в боте не появится.' };
  }
  const withScheme = /^https?:\/\//i.test(base) ? base : 'https://' + base;
  const clean = withScheme.replace(/\/+$/, '');
  // Прямой адрес цеха берём как есть; из «Публичного адреса панели» делаем /staff.
  const url = direct ? clean : (/\/staff$/i.test(clean) ? clean : clean + '/staff');
  if (!/^https:\/\//i.test(url)) {
    return { url, problem: 'Telegram открывает Mini App только по https — по этому адресу кнопки не будет.' };
  }
  return { url, problem: '' };
}

/** Строка-вердикт под полем адреса Mini App: куда ведёт кнопка или почему её нет. */
function miniappStatusRow(s) {
  const t = miniappTarget(s);
  if (t.problem) {
    return `<div class="notice"><span>⚠</span><span data-miniapp-status="off">Mini App цеха выключен. `
      + `${esc(t.problem)} Как получить адрес — docs/MINIAPP-ЦЕХА.md.</span></div>`;
  }
  return `<div class="notice"><span>✓</span><span data-miniapp-status="on">Кнопка «🏭 Открыть цех» ведёт на `
    + `<b>${esc(t.url)}</b></span></div>`;
}

/** Рисует группу настроек по описанию [ключ, подпись, пояснение, тип, шаг]. */
function settingGroup(list) {
  const s = PF.state.settings;
  return list.map(([k, label, sub, type, optionsOrStep]) => {
    let control;
    if (type === 'bool') {
      control = `<label class="switch"><input type="checkbox" data-setting="${k}"${s[k] ? ' checked' : ''}><i></i></label>`;
    } else if (type === 'select' && Array.isArray(optionsOrStep)) {
      const cur = s[k] !== undefined && s[k] !== null ? String(s[k]) : '';
      const hasMatch = optionsOrStep.some(([val]) => String(val) === cur);
      let opts = '';
      if (!hasMatch && cur !== '') {
        opts += `<option value="${esc(cur)}" selected>${esc(cur)} (текущее)</option>`;
      }
      opts += optionsOrStep.map(([val, text]) => {
        const sel = String(val) === cur ? ' selected' : '';
        return `<option value="${esc(String(val))}"${sel}>${esc(text)}</option>`;
      }).join('');
      control = `<select data-setting="${k}">${opts}</select>`;
    } else if (type === 'text') {
      control = `<input type="text" data-setting="${k}" value="${esc(String(s[k] ?? ''))}">`;
    } else {
      control = `<input type="number" step="${optionsOrStep || 1}" min="0" data-setting="${k}" value="${esc(String(num(s[k])))}">`;
    }
    return settingRow(k, label, sub, control);
  }).join('');
}

/* ============================================ 17.0 (И4): форма из схемы.
   Простые ключи (тумблер/число/строка/select) рендерятся из /api/settings/
   schema, сложные блоки остаются ручными карточками — решение заказчика
   №7 (гибрид). Секреты не показываем значением, json-поля сворачиваем в
   «Все настройки» и парсим при сохранении. */
let schemaCache = null;
async function loadSettingsSchema() {
  if (schemaCache) return schemaCache;
  schemaCache = await get('/api/settings/schema');
  return schemaCache;
}

/** Один контрол по описанию поля схемы. */
function schemaControl(f) {
  const s = PF.state.settings;
  const k = f.key, val = s[k];
  const attr = `data-setting="${k}"`;
  if (f.secret) {
    const has = Boolean(s[k]);
    return `<input type="password" autocomplete="new-password" ${attr}
      data-secret="1" placeholder="${has ? '•••••••• сохранён — не трогать' : 'не задан'}">`;
  }
  if (f.choices) {
    return `<select ${attr}>` + f.choices.map((c) =>
      `<option value="${esc(c)}"${String(val) === String(c) ? ' selected' : ''}>${esc(c)}</option>`).join('') + '</select>';
  }
  if (f.type === 'bool') {
    return `<label class="switch"><input type="checkbox" ${attr}${val ? ' checked' : ''}><i></i></label>`;
  }
  if (f.type === 'json') {
    return `<textarea ${attr} data-json="1" rows="4" style="width:100%;font-family:var(--mono);font-size:12px">${esc(JSON.stringify(val ?? f.default, null, 1))}</textarea>`;
  }
  if (f.type === 'int' || f.type === 'float') {
    const step = f.type === 'int' ? '1' : 'any';
    return `<input type="number" step="${step}" ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''} ${attr} value="${esc(String(val ?? f.default ?? ''))}">`;
  }
  return `<input type="text" ${attr} ${f.max_len ? `maxlength="${f.max_len}"` : ''} value="${esc(String(val ?? f.default ?? ''))}">`;
}

/** Карточка-обёртка для полей одной группы. */
function schemaCard(title, sub, fields, opts = {}) {
  const rows = fields.map((f) => {
    const adv = f.advanced ? ' data-advanced="1"' : '';
    // 18.6.3: у каждой настройки видно пояснение (hint из схемы), а не только подпись.
    const hint = f.hint ? `<small>${esc(f.hint)}</small>` : (f.advanced ? '<small>техническое</small>' : '');
    return `<div class="set-row"${adv} data-set-row><div class="sinfo"><b>${esc(f.label)}</b>${hint}`
      + `</div>${schemaControl(f)}</div>`;
  }).join('');
  return `<div class="card" data-set-card data-schema-group="${opts.group || ''}">`
    + `<div class="card-head"><div><h2>${esc(title)}</h2><p>${esc(sub || '')}</p></div></div>`
    + rows + '</div>';
}

/** Поля схемы по группам. */
async function schemaFields(groups) {
  const d = await loadSettingsSchema();
  const out = {};
  for (const g of groups) out[g] = (d.fields[g] || []);
  return out;
}

/** Вкладка «Все настройки»: все группы карточками, технические свёрнуты. */
async function renderAllSettings() {
  const host = $('set_all_fields');
  if (!host) return;
  const d = await loadSettingsSchema();
  host.innerHTML = d.groups.map((g) =>
    schemaCard(g.title, g.id === 'system' ? 'Системные и технические ключи' : '', d.fields[g.id] || [], { group: g.id })
  ).join('');
  const cnt = $('set_all_count');
  const adv = $('set_all_advanced');
  const apply = () => {
    const showAdv = adv.checked;
    let visible = 0;
    $$('[data-schema-group]', host).forEach((card) => {
      let n = 0;
      $$('[data-advanced]', card).forEach((r) => {
        const isOn = showAdv;
        r.classList.toggle('hidden', !isOn);
        if (isOn) n++;
      });
      n += $$('.set-row:not([data-advanced])', card).length;
      card.classList.toggle('hidden', n === 0);
      visible += n;
    });
    cnt.textContent = `${visible} из ${d.count} настроек`;
  };
  applyAllSettingsFilter();
  apply();
  adv.onchange = () => { apply(); applyAllSettingsFilter(); };
  $('set_all_search').oninput = U.debounce((e) => applyAllSettingsFilter(e.target.value), 120);
}

/** Фильтр внутри вкладки «Все настройки». */
function applyAllSettingsFilter(q0) {
  const input = $('set_all_search');
  const raw = (q0 !== undefined ? q0 : (input ? input.value : '')) || '';
  const q = String(raw).trim().toLowerCase();
  const host = $('set_all_fields');
  if (!host) return;
  $$('[data-schema-group]', host).forEach((card) => {
    let any = false;
    $$('.set-row', card).forEach((r) => {
      const hit = !q || r.textContent.toLowerCase().includes(q) ||
        ((r.querySelector('[data-setting]') || {}).dataset || {}).setting?.includes(q);
      r.classList.toggle('hidden', !hit);
      if (hit && !r.hidden) any = true;
    });
    card.classList.toggle('hidden', !any);
  });
}

/** Кассы на связи (17.0.13): кто вошёл, когда отвечал, кто молчит.
 *
 * До этого владелец узнавал об обрыве у кассы только от кассира. Здесь видно
 * и «касса на связи», и «молчит N минут» — по отметкам из cashier_tokens.
 * Список только читается: никаких кнопок «выгнать сессию» здесь нет.
 */
async function renderCashierSessions() {
  const el = $('cashier_sessions');
  if (!el) return;
  let data;
  try {
    data = await get('/api/cashier/sessions');
  } catch (e) {
    el.innerHTML = `<div class="notice warn"><span>⚠</span><span>Список касс не получен: ${esc(e.message || e)}</span></div>`;
    return;
  }
  const rows = data.sessions || [];
  if (!rows.length) {
    el.innerHTML = '<div class="notice"><span>ℹ</span><span>Никто ещё не входил в кассу с телефона. Вход — «Касса» на телефоне по коду из настроек справа.</span></div>';
    return;
  }
  const when = (iso) => {
    const d = new Date(iso || '');
    return isNaN(d) ? '—' : d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  };
  const silent = (sec) => {
    const s = Number(sec) || 0;
    if (s < 90) return 'только что';
    if (s < 3600) return `${Math.round(s / 60)} мин назад`;
    return `${Math.round(s / 360) / 10} ч назад`;
  };
  const online = Number(data.online) || 0;
  const head = `<div class="notice ${online ? 'ok' : 'warn'}"><span>${online ? '✓' : '⚠'}</span><span>`
    + (online ? `На связи: ${online} из ${rows.length}.` : 'Ни одна касса сейчас не отвечает.')
    + '</span></div>';
  el.innerHTML = head + '<table class="table"><thead><tr><th>Касса</th><th>Роль</th><th>Вошёл</th>'
    + '<th>Последний отклик</th><th></th></tr></thead><tbody>'
    + rows.map((r) => {
      const live = !!r.online;
      const label = live ? 'на связи' : (r.silent_seconds == null ? 'не отвечала' : `молчит · ${silent(r.silent_seconds)}`);
      return `<tr><td><b>${esc(r.name || 'кассир')}</b></td>`
        + `<td>${esc(r.role === 'manager' ? 'старший' : 'кассир')}${r.legacy ? ' · код магазина' : ''}</td>`
        + `<td>${when(r.created_at)}</td>`
        + `<td>${r.last_seen ? when(r.last_seen) + ' · ' + silent(r.silent_seconds) : '—'}</td>`
        + `<td><span class="pill ${live ? 'ok' : ''}">${esc(label)}</span></td></tr>`;
    }).join('') + '</tbody></table>';
  const reload = $('cashier_sessions_reload');
  if (reload && !reload.dataset.wired) {
    reload.dataset.wired = '1';
    reload.addEventListener('click', () => renderCashierSessions());
  }
}

// Пока вкладка «Касса» открыта, список обновляется сам: смысл блока — увидеть
// «касса ушла в офлайн», а не вспомнить о нём через час.
setInterval(() => {
  const el = $('cashier_sessions');
  if (el && el.offsetParent !== null && !document.hidden) renderCashierSessions();
}, 30000);

/** Поля для вкладок «Касса» / «СБП» / «Банк» из схемы. */
async function renderCashierSettings() {
  const d = await loadSettingsSchema();
  const s = PF.state.settings;
  const mk = (groups) => groups.flatMap((g) => d.fields[g] || [])
    .filter((f) => !f.secret && f.type !== 'json')
    .map((f) => settingRow(f.key, f.label, f.hint || (f.advanced ? 'Технический ключ' : ''), schemaControl(f)))
    .join('');

  const cEl = $('set_cashier_fields');
  if (cEl) cEl.innerHTML = mk(['cashier']);
  renderCashierSessions();
  const acc = $('set_cashier_account');
  if (acc) {
    const accounts = (PF.state.accounts || []).filter((a) => !num(a.archived));
    acc.innerHTML = settingRow('default_account', 'Касса по умолчанию', 'Куда попадают деньги без уточнения',
      `<select data-setting="default_account">` + (accounts.map((a) =>
        `<option value="${esc(a.id)}"${(s.default_account || 'cash') === a.id ? ' selected' : ''}>${esc(a.name)}</option>`).join('')
        || '<option value="cash">Наличные</option>') + '</select>');
  }
  const sbpEl = $('set_sbp_fields');
  if (sbpEl) sbpEl.innerHTML = mk(['sbp']);
  const bankEl = $('set_bank_fields');
  if (bankEl) bankEl.innerHTML = mk(['bank']);
}


/* Bambu Cloud: вход в аккаунт для управления принтером без LAN Only Mode. */
async function renderCloudSettings(s) {
  const el = $('set_cloud');
  if (!el) return;
  let st = {};
  try {
    st = await get('/api/cloud/status');
  } catch (e) { st = {}; }
  const devices = st.devices || [];
  const status = st.logged
    ? `<div class="notice ok"><span>✓</span><span>Вход выполнен · аккаунт ${esc(st.email || '')} · принтеров: ${devices.length}${st.bridge && st.bridge.connected ? ' · облачный канал на связи' : ''}</span></div>`
    : `<div class="notice warn"><span>⚠</span><span>Вход не выполнен — принтеры в режиме «Облако» не подключены.${st.hint ? ' ' + esc(st.hint) : ''}</span></div>`;
  el.innerHTML = status
    + settingRow('cloud_email', 'Email аккаунта Bambu', 'Тот же, что в Bambu Handy / MakerWorld',
      `<input type="text" data-setting="cloud_email" value="${esc(String(s.cloud_email || ''))}" placeholder="you@mail.com">`)
    + settingRow('cloud_region', 'Регион', 'Global — для большинства аккаунтов',
      `<select data-setting="cloud_region"><option value="global"${(s.cloud_region || 'global') === 'global' ? ' selected' : ''}>Global</option><option value="china"${s.cloud_region === 'china' ? ' selected' : ''}>China</option></select>`)
    + `<div class="set-row"><div class="sinfo"><b>Пароль</b><small>Только для входа, нигде не сохраняется. При истечении токена вход повторяется кодом с почты.</small></div>`
    + `<input type="password" autocomplete="new-password" id="cloud_password" placeholder="пароль Bambu"></div>`
    + `<div class="set-row" id="cloud_code_row" hidden><div class="sinfo"><b>Код из письма/SMS</b><small>Bambu прислал код для входа</small></div>`
    + `<input id="cloud_code" placeholder="6 цифр"></div>`
    + `<div class="set-row"><div class="sinfo"></div><span class="acts">`
    + `<button class="btn sm" type="button" id="cloud_login_btn">Войти</button> `
    + `<button class="btn sm" type="button" id="cloud_code_btn" hidden>Подтвердить код</button> `
    + `<button class="btn sm" type="button" id="cloud_logout_btn"${st.logged ? '' : ' hidden'}>Выйти</button>`
    + '</span></div>'
    + (devices.length ? `<div class="set-row"><div class="sinfo"><b>Принтеры аккаунта</b><small>Добавляются в «Принтеры» → «＋ Принтер» → «Найти»</small></div>`
      + `<span class="chip ok">${devices.length} шт</span></div>` : '');
  const passInput = $('cloud_password');
  if (passInput) {
    passInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        el.querySelector('#cloud_login_btn').click();
      }
    });
  }
  const codeInput = $('cloud_code');
  if (codeInput) {
    codeInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        el.querySelector('#cloud_code_btn').click();
      }
    });
  }
  el.querySelector('#cloud_login_btn').addEventListener('click', async () => {
    const email = ($$('[data-setting="cloud_email"]')[0] || {}).value || '';
    const region = ($$('[data-setting="cloud_region"]')[0] || {}).value || 'global';
    const password = $('cloud_password').value || '';
    if (!email) return toast('Укажите email аккаунта', '', 'warn');
    try {
      const res = await post('/api/cloud/login', { email, password, region });
      if (res.status === 'ok') {
        toast('Bambu Cloud подключён', 'Принтеры аккаунта добавятся в список');
        $('cloud_password').value = '';
        await PF.refreshCore();
        renderSettings();
      } else if (res.status === 'need_code') {
        $('cloud_code_row').hidden = false;
        $('cloud_code_btn').hidden = false;
        $('cloud_code').focus();
        toast('Код отправлен', 'Введите код из письма/SMS');
      } else if (res.status === 'need_tfa') {
        const code = await ask({
          title: 'Двухфакторный вход',
          sub: 'Код из приложения-аутентификатора',
          fields: [{ name: 'code', label: 'Код', type: 'text', placeholder: '123456' }],
          ok: 'Войти',
        });
        if (!code) return;
        const res2 = await post('/api/cloud/login', { email, region, tfa_code: code });
        if (res2.status !== 'ok') return fail(new Error(res2.message || 'Не удалось войти'));
        toast('Bambu Cloud подключён');
        $('cloud_password').value = '';
        await PF.refreshCore();
        renderSettings();
      } else {
        fail(new Error(res.message || 'Не удалось войти'));
      }
    } catch (e) { fail(e); }
  });
  el.querySelector('#cloud_code_btn').addEventListener('click', async () => {
    const email = ($$('[data-setting="cloud_email"]')[0] || {}).value || '';
    const region = ($$('[data-setting="cloud_region"]')[0] || {}).value || 'global';
    const code = ($('cloud_code').value || '').trim();
    if (!code) return toast('Введите код', 'Код из письма/SMS', 'warn');
    try {
      const res = await post('/api/cloud/code', { code, email, region });
      if (res.status !== 'ok') return fail(new Error(res.message || 'Код не подошёл'));
      toast('Bambu Cloud подключён', 'Принтеры аккаунта добавятся в список');
      $('cloud_code').value = '';
      $('cloud_code_row').hidden = true;
      $('cloud_code_btn').hidden = true;
      await PF.refreshCore();
      renderSettings();
    } catch (e) { fail(e); }
  });
  el.querySelector('#cloud_logout_btn').addEventListener('click', async () => {
    if (!confirmDanger('Выйти из Bambu Cloud? Принтеры в облачном режиме отключатся.')) return;
    try {
      await post('/api/cloud/logout', {});
      toast('Выход выполнен');
      await PF.refreshCore();
      renderSettings();
    } catch (e) { fail(e); }
  });
}

/* ==================================================== материалы (свои пластики) */
let materialsFull = [];
let editingMaterial = '';
async function loadMaterials() {
  try {
    const data = await get('/api/materials');
    materialsFull = data.materials_full || [];
  } catch (e) { materialsFull = []; }
  renderMaterials();
}
function renderMaterials() {
  const host = $('set_materials');
  const datalist = $('materials_datalist');
  if (datalist) {
    const names = [...new Set(materialsFull.map((m) => m.name || m.key).filter(Boolean))];
    datalist.innerHTML = names.map((name) => `<option value="${esc(name)}">`).join('');
  }
  if (!host) return;
  const custom = materialsFull.filter((m) => !m.builtin);
  const builtin = materialsFull.filter((m) => m.builtin);
  host.innerHTML = (custom.length ? custom.map((m) => `<div class="set-row">`
    + `<div class="sinfo"><b>${esc(m.name)}</b><small>${esc(m.full_name || '')} · сопло ${m.temp_nozzle[0]}–${m.temp_nozzle[1]}°C · стол ${m.temp_bed[0]}–${m.temp_bed[1]}°C · скорость ×${m.speed_factor}`
    + (m.price_per_kg ? ` · ${money(m.price_per_kg)}/кг` : ' · цена: из шаблона')
    + (m.abrasive ? ' · абразивный' : '') + (m.uv_resistant ? ' · УФ-стойкий' : '') + '</small></div>'
    + `<div class="row-actions"><button class="btn sm" data-mat-edit="${esc(m.id)}" type="button">Править</button>`
    + `<button class="btn sm danger" data-mat-del="${esc(m.id)}" type="button" title="Убрать">×</button></div></div>`).join('')
    : '<div class="empty compact"><span>Своих пластиков пока нет — добавьте материал со своими температурами, скоростью и ценой.</span></div>')
    + `<details style="margin-top:10px"><summary style="cursor:pointer;font-size:12.5px;color:var(--muted)">База пластиков — ${builtin.length} типов (нажмите на тип, чтобы настроить под себя)</summary>`
    + `<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px">`
    + builtin.map((m) => (m.id
      ? `<button class="tag" data-mat-edit="${esc(m.id)}" type="button" style="cursor:pointer" title="Настроить: ${esc(m.full_name || '')} · сопло ${m.temp_nozzle[0]}–${m.temp_nozzle[1]}°C · стол ${m.temp_bed[0]}–${m.temp_bed[1]}°C · ${m.price_per_kg} ₽/кг${m.abrasive ? ' · абразивный' : ''}">${esc(m.name)}</button>`
      : `<span class="tag">${esc(m.name)}</span>`)).join('')
    + '</div></details>';
}
function openMaterial(id) {
  editingMaterial = id || '';
  const m = id ? materialsFull.find((x) => x.id === id) : null;
  const isBuiltin = Boolean(m && m.builtin);
  const catalog = materialsFull.filter((x) => x.builtin);
  put('mat_base', '<option value="">— без шаблона —</option>'
    + catalog.map((b) => `<option value="${esc(b.key)}">${esc(b.name)}</option>`).join(''));
  // У встроенного типа ключ не меняется — он и есть имя типа.
  const keyEl = $('mat_key');
  if (keyEl) { keyEl.readOnly = isBuiltin; keyEl.disabled = isBuiltin; }
  const resetBtn = $('mat_reset');
  if (resetBtn) resetBtn.hidden = !isBuiltin;
  const set = (k, v) => { const el = $('mat_' + k); if (el) el.value = v == null ? '' : String(v); };
  set('name', m ? m.name : '');
  set('key', m ? m.key : '');
  set('base', m ? (m.base || '') : '');
  set('full_name', m ? m.full_name : '');
  set('price_per_kg', m ? (m.price_per_kg || '') : '');
  set('speed_factor', m ? m.speed_factor : '');
  set('nozzle_min', m ? m.temp_nozzle[0] : '');
  set('nozzle_max', m ? m.temp_nozzle[1] : '');
  set('bed_min', m ? m.temp_bed[0] : '');
  set('bed_max', m ? m.temp_bed[1] : '');
  set('fan', m ? m.fan : '');
  set('chamber', m ? (m.chamber || 'open') : 'open');
  set('density', m ? m.density : '');
  set('shrinkage', m ? m.shrinkage : '');
  set('dry_temp', m ? m.dry_temp : '');
  set('dry_hours', m ? m.dry_hours : '');
  set('heat_resistance', m ? m.heat_resistance : '');
  set('support_factor', m ? m.support_factor : '');
  set('strengths', m ? m.strengths : '');
  set('weaknesses', m ? m.weaknesses : '');
  set('use_cases', m ? m.use_cases : '');
  set('note', m ? m.note : '');
  $('mat_abrasive').checked = Boolean(m && m.abrasive);
  $('mat_uv_resistant').checked = Boolean(m && m.uv_resistant);
  $('mat_food_safe').checked = Boolean(m && m.food_safe);
  $('material_modal_title').textContent = m
    ? (m.builtin ? `Встроенный материал · ${m.name}` : `Свой материал · ${m.name}`)
    : 'Новый материал';
  openModal('material_modal');
}
function fillMaterialFromBase(key) {
  const m = materialsFull.find((x) => x.key === key && !x.custom);
  if (!m) return;
  const setIfEmpty = (k, v) => { const el = $('mat_' + k); if (el && !el.value) el.value = String(v); };
  setIfEmpty('price_per_kg', m.price_per_kg);
  setIfEmpty('speed_factor', m.speed_factor);
  setIfEmpty('nozzle_min', m.temp_nozzle[0]);
  setIfEmpty('nozzle_max', m.temp_nozzle[1]);
  setIfEmpty('bed_min', m.temp_bed[0]);
  setIfEmpty('bed_max', m.temp_bed[1]);
  setIfEmpty('fan', m.fan);
  setIfEmpty('chamber', m.chamber);
  setIfEmpty('density', m.density);
  setIfEmpty('shrinkage', m.shrinkage);
  setIfEmpty('dry_temp', m.dry_temp);
  setIfEmpty('dry_hours', m.dry_hours);
  setIfEmpty('heat_resistance', m.heat_resistance);
  setIfEmpty('support_factor', m.support_factor);
  setIfEmpty('full_name', m.full_name);
  setIfEmpty('strengths', m.strengths);
  setIfEmpty('weaknesses', m.weaknesses);
  setIfEmpty('use_cases', m.use_cases);
  if (!$('mat_abrasive').checked) $('mat_abrasive').checked = Boolean(m.abrasive);
  if (!$('mat_uv_resistant').checked) $('mat_uv_resistant').checked = Boolean(m.uv_resistant);
}
async function saveMaterial() {
  /* 18.6.3: пустое числовое поле = «не задано», а не ноль. Раньше пустые
     температуры/цену saveMaterial отправлял как 0 — у материала появлялись
     «сопло 222–0°C», «стол 0–0°C», и значения казались несохранёнными.
     Теперь на сервер уходит null: он возьмёт значение из шаблона (base)
     или каталога пластиков. */
  const numOrNull = (id) => { const v = $(id).value.trim(); return v === '' ? null : num(v); };
  const payload = {
    id: editingMaterial || '',
    name: $('mat_name').value.trim(),
    key: $('mat_key').value.trim(),
    base: $('mat_base').value,
    full_name: $('mat_full_name').value.trim(),
    price_per_kg: numOrNull('mat_price_per_kg'),
    speed_factor: numOrNull('mat_speed_factor'),
    temp_nozzle_min: numOrNull('mat_nozzle_min'),
    temp_nozzle_max: numOrNull('mat_nozzle_max'),
    temp_bed_min: numOrNull('mat_bed_min'),
    temp_bed_max: numOrNull('mat_bed_max'),
    fan: numOrNull('mat_fan'),
    chamber: $('mat_chamber').value,
    density: numOrNull('mat_density'),
    shrinkage: numOrNull('mat_shrinkage'),
    dry_temp: numOrNull('mat_dry_temp'),
    dry_hours: numOrNull('mat_dry_hours'),
    heat_resistance: numOrNull('mat_heat_resistance'),
    support_factor: numOrNull('mat_support_factor'),
    abrasive: $('mat_abrasive').checked ? 1 : 0,
    uv_resistant: $('mat_uv_resistant').checked ? 1 : 0,
    food_safe: $('mat_food_safe').checked ? 1 : 0,
    strengths: $('mat_strengths').value.trim(),
    weaknesses: $('mat_weaknesses').value.trim(),
    use_cases: $('mat_use_cases').value.trim(),
    note: $('mat_note').value.trim(),
  };
  if (!payload.name) return fail(new Error('Укажите название материала'));
  try {
    await post('/api/materials/save', payload);
    closeModal('material_modal');
    toast('Материал сохранён', payload.name);
    await loadMaterials();
    if (PF.modules.money && PF.modules.money.loadCalcMaterials) PF.modules.money.loadCalcMaterials();
  } catch (e) { fail(e); }
}
async function deleteMaterial(id) {
  const m = materialsFull.find((x) => x.id === id);
  if (!m) return;
  if (!confirmDanger(`Убрать «${m.name}» из справочника? История и прошлые расчёты не пострадают.`)) return;
  try {
    await post('/api/materials/delete', { id });
    toast('Материал убран', m.name);
    await loadMaterials();
    if (PF.modules.money && PF.modules.money.loadCalcMaterials) PF.modules.money.loadCalcMaterials();
  } catch (e) { fail(e); }
}

function renderSettings() {
  const s = PF.state.settings;
  /* Каждая карточка рисуется независимо (18.6.3): сбой одной (нет элемента в
     старой кэшированной разметке, неожиданные данные) раньше обрывал функцию —
     и все карточки ниже оставались пустыми, включая «Принтеры и Bambu» и
     «Материалы для печати». Теперь падение изолировано внутри своей карточки. */
  const safe = (name, fn) => {
    try { fn(); } catch (e) { console.error('Настройки · карточка «' + name + '»: ', e); }
  };

  safe('tarifs', () => {
    if ($('set_rates')) put('set_rates', settingGroup(RATES));
  });

  safe('storage', () => {
    if ($('set_storage_rules')) put('set_storage_rules', settingGroup(STORAGE_RULES));
  });

  // --- бизнес
  safe('business', () => {
    put('set_company', settingGroup(COMPANY));
    put('set_goals', settingGroup(GOALS));
  });

  // --- налоги
  safe('tax', () => {
    const mode = s.tax_mode || 'none';
    if ($('set_tax_mode')) $('set_tax_mode').value = mode;
    const hints = (PF.modules.finance && PF.modules.finance.MODE_HINTS) || {};
    if ($('set_tax_hint') && $('set_tax_hint').lastElementChild) {
      $('set_tax_hint').lastElementChild.textContent = hints[mode] || '';
    }
    put('set_tax', settingGroup(TAX_FIELDS[mode] || [])
      || '<div class="empty compact"><span>Для этого режима настраивать нечего.</span></div>');
    put('set_insurance', ['usn6', 'usn15', 'patent'].includes(mode)
      ? settingGroup(INSURANCE)
      : '<div class="notice"><span>ℹ</span><span>На выбранном режиме страховые взносы «за себя» не платятся.</span></div>');
    put('set_vat', settingGroup(VAT));
  });

  // --- цены
  safe('pricing', () => {
    put('set_pricing', settingGroup(PRICING));
    put('set_discounts', settingGroup(DISCOUNTS));
    put('set_payments', settingGroup(PAYMENTS));
  });

  // --- учёт денег
  safe('money', () => {
    const accounts = (PF.state.accounts || []).filter((a) => !num(a.archived));
    const BANK_RULES_SAMPLE = [
      { match: 'ozon', kind: 'income', category: 'sale', title: 'Продажа (Ozon)' },
      { match: 'пластик|филамент|petg|pla|abs|tpu', kind: 'expense', category: 'filament', title: 'Закупка пластика' },
      { match: 'электроэнергия|энергосбыт', kind: 'expense', category: 'energy', title: 'Электричество' },
      { match: 'налог', kind: 'expense', category: 'tax', title: 'Налог' },
    ];
    const bankRules = Array.isArray(s.bank_rules) && s.bank_rules.length ? s.bank_rules : BANK_RULES_SAMPLE;
    put('set_money_rules', settingGroup(MONEY_RULES)
      + settingRow('bank_rules', 'Правила импорта выписки', 'JSON: match — регулярное выражение по назначению платежа, kind: income|expense, category — статья, title — название проводки.',
        `<textarea data-setting="bank_rules" rows="7" style="width:100%;font-family:ui-monospace,monospace;font-size:12px">${esc(JSON.stringify(bankRules, null, 1))}</textarea>`)
      + settingRow('default_account', 'Касса по умолчанию', 'Куда попадают деньги без уточнения',
        `<select data-setting="default_account">${accounts.map((a) =>
          `<option value="${esc(a.id)}"${(s.default_account || 'cash') === a.id ? ' selected' : ''}>${esc(a.name)}</option>`)
          .join('') || '<option value="cash">Наличные</option>'}</select>`));
  });

  safe('production', () => {
    put('set_auto', AUTOS.map(([k, label, sub]) => settingRow(k, label, sub,
      `<label class="switch"><input type="checkbox" data-setting="${k}"${s[k] ? ' checked' : ''}><i></i></label>`)).join(''));
    put('set_auto_extra', settingGroup(AUTO_EXTRA));
    put('set_guard', settingGroup(GUARD));
    put('set_queue_rules', settingGroup(QUEUE_RULES));
    put('set_upkeep', settingGroup(UPKEEP));
    if ($('set_rules')) renderRules();
  });

  safe('watch', () => {
    if ($('set_watch')) {
      put('set_watch', settingGroup(WATCH)
        + `<div class="set-row" data-set-row><div class="sinfo"><b>Быстрый выбор пути</b><small>Нажмите, чтобы подставить типовую папку для 3MF</small></div>`
        + `<div style="display:flex;gap:6px;flex-wrap:wrap">`
        + `<button class="btn sm" type="button" data-watch-preset="default">~/PrintFlow-Inbox</button>`
        + `<button class="btn sm" type="button" data-watch-preset="desktop">~/Desktop/3MF</button>`
        + `<button class="btn sm" type="button" data-watch-preset="win">C:\\PrintFlow-Inbox</button>`
        + `</div></div>`);
    }
  });
  safe('studio', () => {
    if ($('set_studio')) {
      const printerOpts = [['', 'Любой свободный принтер']].concat(
        (PF.state.printers || []).map((p) => [p.id, `${p.name} (${p.model || 'Bambu'})`])
      );
      const curPrinter = s.studio_gateway_printer_id || '';
      const printerSelect = `<select data-setting="studio_gateway_printer_id">`
        + printerOpts.map(([val, label]) => `<option value="${esc(val)}"${val === curPrinter ? ' selected' : ''}>${esc(label)}</option>`).join('')
        + `</select>`;

      put('set_studio', settingGroup(STUDIO)
        + settingRow('studio_gateway_printer_id', 'Принтер по умолчанию для шлюза', 'На какой принтер направлять печать из Studio', printerSelect)
        + settingRow('studio_gateway_access_code', 'Access Code для Studio',
          s.has_studio_gateway_access_code
            ? 'Сохранён — оставьте пустым, чтобы не менять. Studio спросит этот код при подключении.'
            : 'Задайте сами: Studio спросит его при подключении. Пусто при включении — сгенерируется и повторно не покажется.',
          '<input type="password" autocomplete="new-password" data-setting="studio_gateway_access_code" data-secret="1" placeholder="'
            + (s.has_studio_gateway_access_code ? '••••••••' : 'задайте код') + '">'));
      get('/api/studio/status').then((data) => {
        const el = $('studio_status');
        if (!el) return;
        const st = data.enabled ? 'вкл' : 'выкл';
        const mark = (on) => on
          ? '<b style="color:var(--ok,#22c55e)">✓</b>'
          : '<b style="color:var(--bad,#ef4444)">✗</b>';
        const model = esc(data.dev_model || data.model || '');
        const hostPinned = data.host_pinned ? ' (закреплён в настройках)' : ' (авто)';
        let html = `<span>ℹ</span><span>Шлюз ${st} · адрес <b>${esc(data.host || '—')}</b>${hostPinned}`
          + ` · опрос :${data.bind_port || 3000}${data.bind_tls_port ? `/${data.bind_tls_port}` : ''} ${mark(data.bind_running)}`
          + ` · MQTT :${data.mqtt_port || 8883} ${mark(data.mqtt_running)}`
          + ` · FTPS :${data.ftp_port || 990} ${mark(data.ftp_running)}`
          + ` · SSDP ${mark(data.ssdp_running)}`
          + ` · ${esc(data.name || '')} · ${esc(data.serial || 'нет SN')} · ${model}</span>`;
        if (data.enabled && !data.bind_running) {
          html += `<span style="display:block;margin-top:4px;color:var(--bad,#ef4444)">Studio не узна́ет шлюз: порт ${data.bind_port || 3000} не поднят —`
            + ` без него Studio отдаёт «код=-1» ещё до MQTT. Порт занят или шлюз не стартовал: смотрите ошибку ниже и журнал.</span>`;
        }
        const targets = Array.isArray(data.ssdp_targets) ? data.ssdp_targets : [];
        if (data.enabled && data.ssdp_running && targets.length) {
          const viaLoopback = targets.includes('127.0.0.1:2021');
          html += `<span style="display:block;margin-top:4px">Объявление уходит на ${targets.length} адрес(ов)`
            + `${viaLoopback ? ', включая 127.0.0.1:2021 — Studio на этом же компьютере видит шлюз даже при включённом брандмауэре' : ''};`
            + ` UDP :2021 шлюз не занимает (он принадлежит Studio)${data.ssdp_bound_port && data.ssdp_bound_port !== 1900 ? `, M-SEARCH слушается на :${data.ssdp_bound_port}` : ''}.</span>`;
        }
        if (data.ssdp_note) {
          html += `<span style="display:block;margin-top:4px">⚠ ${esc(data.ssdp_note)}</span>`;
        }
        if ((data.dropped_connections || 0) > 0) {
          const silent = data.tls_handshake_timeouts || 0;
          html += `<span style="display:block;margin-top:4px">ℹ Отброшено соединений без TLS: ${data.dropped_connections}`
            + `${silent ? ` (из них молчунов, отброшенных по таймауту ${Math.round(data.tls_handshake_timeout || 8)} с: ${silent})` : ''}`
            + ` — это проверки порта и сканеры; рукопожатие идёт в потоке клиента, приём остальных не блокируется.</span>`;
        }
        // 18.11: TLS-сессии общего контекста — Studio возобновляет сессию на канале данных.
        if (data.enabled && (data.tls_handshakes || 0) > 0) {
          const sess = data.tls_sessions || {};
          html += `<span style="display:block;margin-top:4px">🔐 TLS: рукопожатий ${data.tls_handshakes}, возобновлено сессий ${data.tls_resumed || 0}`
            + `${sess.cached != null ? `, в кэше ${sess.cached}` : ''} — возобновление означает, что FTPS-канал данных не жмёт руку заново.</span>`;
        }
        // 18.12.1: Studio подписалась на device/<чужой серийник>/report —
        // отчёты шлюза уходят в свою тему, карточка гаснет, а Studio пишет
        // «Unsubscribe device». Причина видна здесь, а не только в журнале.
        if ((data.subscribe_serial_mismatch || 0) > 0) {
          const topics = Array.isArray(data.subscribe_topics) ? data.subscribe_topics : [];
          html += `<span style="display:block;margin-top:4px;color:var(--warn,#f59e0b)">⚠ Studio подписалась на чужой серийник`
            + `${topics.length ? ` (${topics.map(esc).join(', ')})` : ''} — пересоздайте подключение,`
            + ` серийник из этой карточки: <b>${esc(data.serial || '—')}</b>.`
            + ` Отчёты шлюза идут только на свою тему, поэтому карточка устройства гаснет`
            + ` и Studio отписывает принтер («Unsubscribe device»).</span>`;
        }
        // 18.12.1: пакет MQTT прервался тайм-аутом после первого байта —
        // поток рассинхронизирован, соединение закрыто защитой, не сбой службы.
        if ((data.mqtt_broken_packets || 0) > 0) {
          html += `<span style="display:block;margin-top:4px;color:var(--muted,#6b7280)">ℹ MQTT: разорвано по защите от рассинхрона — ${data.mqtt_broken_packets} пакет(ов)`
            + ` прервано тайм-аутом после первого байта. Stream был прочитан наполовину, поэтому соединение закрыто честно:`
            + ` Studio переподключится сама.</span>`;
        }
        // 18.11: ретранслятор реальных станков.
        if (data.enabled && data.relay_enabled) {
          const rp = Array.isArray(data.relay_printers) ? data.relay_printers : [];
          const rt = Array.isArray(data.relay_targets) ? data.relay_targets : [];
          html += `<span style="display:block;margin-top:4px">📡 Ретранслятор: объявляем ${rp.length} станок(ов)`
            + `${rp.length ? ' — ' + rp.map((x) => `${esc(x.name)} (${esc(x.host)})` ).join(', ') : ''}`
            + `${rt.length ? `; адресаты Studio: ${rt.map(esc).join(', ')}` : ''}; отправлено объявлений станков: ${data.relay_sent || 0}.</span>`;
          if (data.relay_note) html += `<span style="display:block;margin-top:4px">⚠ ${esc(data.relay_note)}</span>`;
        }
        if (data.enabled) {
          html += `<span style="display:block;margin-top:8px"><button class="btn sm" type="button" id="studio_announce_btn" title="Разослать SSDP-объявления шлюза и станков немедленно, не дожидаясь периода рассылки">📣 Объявить сейчас</button>`
            + ` <small class="muted">Studio только что открыли и она не видит шлюз — нажмите: объявление уйдёт на все адреса сразу.</small></span>`;
        }
        const fails = (data.mqtt_auth_failures || 0) + (data.ftp_auth_failures || 0);
        if (fails > 0) {
          const at = String(data.last_auth_fail_at || '').slice(11, 16);
          html += `<span style="display:block;margin-top:4px">⚠ Studio стучалась, код не подошёл: ${fails} раз`
            + `${at ? `, последний в ${esc(at)}` : ''} — сверйте Access Code в настройках шлюза</span>`;
        }
        const errs = data.errors || {};
        const errBits = ['bind', 'ssdp', 'mqtt', 'ftps', 'host']
          .map((k) => errs[k] ? `${k.toUpperCase()}: ${errs[k]}` : '')
          .filter(Boolean);
        const lastErr = errBits.join(' · ') || (data.last_error ? String(data.last_error) : '');
        if (lastErr) html += `<span style="display:block;margin-top:4px;color:var(--bad,#ef4444)">Ошибка: ${esc(lastErr)}</span>`;
        el.innerHTML = html;
      }).catch(() => {});
      get('/api/slicer/status').then((data) => {
        const el = $('studio_status');
        if (!el || !data) return;
        const extra = data.available
          ? ` · слайсер ${esc(data.name || data.bin || '')}`
          : ' · CLI-слайсер не найден';
        if (el.lastElementChild) el.lastElementChild.textContent += extra;
      }).catch(() => {});
    }
  });

  safe('bambu-protocols', () => {
    if ($('set_preflight')) put('set_preflight', settingGroup(PREFLIGHT));
    if ($('set_ftps')) put('set_ftps', settingGroup(FTPS));
    if ($('set_mqtt')) put('set_mqtt', settingGroup(MQTT));
    if ($('set_ams')) put('set_ams', settingGroup(AMS_SETTINGS));
    if ($('set_phase11')) put('set_phase11', settingGroup(PHASE11));
    if ($('set_system2')) put('set_system2', settingGroup(SYSTEM2));
    // FarmLoop — в разделе «Конвейер» (18.8), из настроек карточка снята.
    // профили настроек
    if ($('set_profiles')) renderProfiles();
  });

  safe('telegram', () => {
    put('set_tg', settingRow('telegram_enabled', 'Включить Telegram', 'Уведомления о печати',
      `<label class="switch"><input type="checkbox" data-setting="telegram_enabled"${s.telegram_enabled ? ' checked' : ''}><i></i></label>`)
      + settingRow('telegram_token', 'Bot Token', s.has_telegram_token ? 'Сохранён — оставьте пустым, чтобы не менять' : 'Получите у @BotFather',
        '<input type="password" autocomplete="new-password" data-setting="telegram_token" data-secret="1" placeholder="' + (s.has_telegram_token ? '••••••••' : 'токен') + '">')
      + settingRow('telegram_chat_id', 'Chat ID', 'Ваш идентификатор в Telegram',
        `<input type="text" data-setting="telegram_chat_id" value="${esc(String(s.telegram_chat_id || ''))}">`)
      + settingRow('telegram_bot', 'Отвечать на команды', 'Бот принимает «статус», «кадр», «пауза» с телефона',
        `<label class="switch"><input type="checkbox" data-setting="telegram_bot"${s.telegram_bot ? ' checked' : ''}><i></i></label>`)
      + settingRow('staff_miniapp_url', 'Адрес Mini App цеха',
        'Куда ведёт кнопка «🏭 Открыть цех». Telegram открывает Mini App только по https — как получить адрес: docs/MINIAPP-ЦЕХА.md',
        `<input type="text" data-setting="staff_miniapp_url" maxlength="300" placeholder="https://ceh.example.ru/staff" value="${esc(String(s.staff_miniapp_url || ''))}">`)
      + miniappStatusRow(s)
      + NOTIFY.map(([k, label, sub]) => settingRow(k, label, sub || '',
        `<label class="switch"><input type="checkbox" data-setting="${k}"${s[k] ? ' checked' : ''}><i></i></label>`)).join('')
      + settingRow('browser_notify_enabled', 'Уведомления в браузере', 'Пока PrintFlow открыт вкладкой — события приходят сразу',
        `<label class="switch"><input type="checkbox" data-setting="browser_notify_enabled"${s.browser_notify_enabled ? ' checked' : ''}><i></i></label>
         <button class="btn sm" type="button" id="notify_perm_btn" style="margin-top:8px">Разрешить уведомления</button>`));
  });

  safe('theme', () => {
    if ($('set_cloud')) renderCloudSettings(s);
    if ($('set_theme')) $('set_theme').value = s.theme || 'system';
    put('set_accent', ACCENTS.map(([name, color]) =>
      `<button type="button" data-accent="${name}" class="${(s.accent || 'indigo') === name ? 'on' : ''}" style="background:${color}" title="${name}"></button>`).join(''));
  });

  safe('printers', () => {
    put('set_printers', PF.state.printers.length ? PF.state.printers.map((p) => {
      const livep = PF.livePrinter(p.id);
      return `<div class="set-row"><div class="sinfo"><b>${esc(p.name)}</b>`
        + `<small>${esc(p.model || '')} · ${esc(p.host || 'IP не задан')} · ${p.has_access_code ? 'код сохранён' : 'нет Access Code'}`
        + `${livep ? ' · ' + esc(livep.printer.state_label) : ''}</small></div>`
        + `<button class="btn sm" type="button" data-printer-edit="${esc(p.id)}">Изменить</button></div>`;
    }).join('') : '<div class="empty compact"><span>Принтеры не добавлены.</span></div>');
  });

  safe('misc', () => {
    if ($('set_data_dir')) {
      $('set_data_dir').textContent = (navigator.platform || '').toLowerCase().includes('win')
        ? '%APPDATA%\\PrintFlow' : '~/.config/printflow';
    }
    $$('#set_shortcuts [data-set-shortcut]').forEach((button) => {
      button.classList.toggle('on', button.dataset.setShortcut === settingsPane);
    });
    // 17.0 (И4): вкладки «Касса / СБП / Банк» и «Все настройки» строятся из схемы
    renderCashierSettings().catch(() => {});
    renderAllSettings().catch(() => {
      if ($('set_all_fields')) {
        put('set_all_fields', '<div class="empty compact"><span>Схема настроек недоступна — проверьте связь с коннектором.</span></div>');
      }
    });
    renderUpdateInfo();
  });
}

/* ============================================================ обновления */
let updateInfo = null;
let updateBusy = false;

async function checkUpdate(force) {
  try {
    const data = await get('/api/update-check');
    updateInfo = data;
    renderUpdateInfo();
    return data;
  } catch (e) { return null; }
}

/** Установить обновление и дождаться, пока коннектор поднимется заново. */
async function applyUpdate(force) {
  if (updateBusy) return;
  const latest = (updateInfo && updateInfo.latest) || {};
  const what = latest.title ? `«${latest.title}»` : latest.short || 'обновление';
  if (!confirmDanger(`Установить ${what}?\n\nПеред установкой будет сделана копия базы, `
    + 'после — коннектор перезапустится. Печать в это время не должна идти.')) return;
  updateBusy = true;
  renderUpdateInfo();
  try {
    const res = await post('/api/update/apply', { force: !!force });
    if (!res.changed) {
      toast('Обновлять нечего', 'Файлы уже актуальны');
      updateBusy = false;
      await checkUpdate(true);
      return;
    }
    toast('Обновление установлено', `${res.before} → ${res.after} · файлов: ${res.files}`);
    if (res.restarting) await waitForRestart();
    else updateBusy = false;
  } catch (e) {
    updateBusy = false;
    fail(e);
    renderUpdateInfo();
  }
}

/** Пингуем коннектор, пока он не ответит после перезапуска, затем перезагружаем страницу. */
async function waitForRestart() {
  const host = $('set_update_info');
  if (host) {
    host.innerHTML = '<div class="notice"><span>⏳</span><span><b>Коннектор перезапускается…</b>'
      + ' Страница обновится сама через несколько секунд.</span></div>';
  }
  const deadline = Date.now() + 90000;
  // Сначала ждём, пока старый процесс действительно уйдёт.
  await new Promise((r) => setTimeout(r, 2500));
  while (Date.now() < deadline) {
    try {
      const res = await fetch('/api/update-check', { cache: 'no-store' });
      if (res.ok) {
        toast('Готово', 'Перезагружаем интерфейс', 'ok');
        setTimeout(() => location.reload(), 700);
        return;
      }
    } catch (e) { /* ещё не поднялся */ }
    await new Promise((r) => setTimeout(r, 1500));
  }
  updateBusy = false;
  toast('Коннектор не ответил', 'Перезапустите PrintFlow вручную', 'bad');
  renderUpdateInfo();
}

function renderUpdateInfo() {
  const host = $('set_update_info');
  if (!host) return;
  const u = updateInfo;

  if (updateBusy) {
    host.innerHTML = '<div class="notice"><span>⏳</span><span><b>Устанавливаем обновление…</b>'
      + ' Не закрывайте окно коннектора.</span></div>';
  } else if (!u) {
    host.innerHTML = '<button class="btn sm" type="button" id="update_check_btn">Проверить обновления</button>';
  } else if (u.disabled) {
    host.innerHTML = '<span class="muted" style="font-size:12.4px">Проверка обновлений выключена ниже.</span>';
  } else if (u.error) {
    host.innerHTML = `<div class="notice warn"><span>⚠</span><span><b>Не удалось проверить обновления.</b> ${esc(u.error)}</span></div>`
      + '<button class="btn sm" type="button" id="update_check_btn" style="margin-top:8px">Повторить</button>';
  } else if (u.update && u.latest) {
    const commits = (u.commits || []).slice(0, 8);
    host.innerHTML = `<div class="notice" style="border-color:var(--ok)"><span>⬆</span><span>`
      + `<b>Доступно обновление ${esc(u.latest.short)}</b>`
      + (u.latest.date ? ` от ${esc(dateText(u.latest.date))}` : '')
      + ` — у вас ${esc(u.local || u.current)}.<br>${esc(u.latest.title || '')}`
      + (u.latest.url ? ` <a href="${esc(u.latest.url)}" target="_blank" rel="noopener">Посмотреть на GitHub →</a>` : '')
      + '</span></div>'
      + (commits.length ? '<div class="upd-list">' + commits.map((c) =>
        `<div class="upd-row"><code>${esc(c.short)}</code><span>${esc(c.title)}</span>`
        + `<small class="muted">${esc(dateText(c.date))}</small></div>`).join('') + '</div>' : '')
      + (u.busy_reason
        ? `<div class="notice warn" style="margin-top:8px"><span>⚠</span><span>${esc(u.busy_reason)}. `
          + 'Обновление можно поставить, когда принтеры освободятся.</span></div>'
        : '')
      + '<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">'
      + `<button class="btn primary sm" type="button" id="update_apply_btn"${u.can_apply ? '' : ' disabled'}>⬆ Установить и перезапустить</button>`
      + '<button class="btn sm" type="button" id="update_check_btn">Проверить ещё раз</button>'
      + '</div>';
  } else {
    host.innerHTML = `<span class="muted" style="font-size:12.4px">У вас актуальная версия ${esc(u.current)}`
      + (u.local ? ` · ${esc(u.local)}` : '') + (u.branch ? ` · ветка ${esc(u.branch)}` : '')
      + (u.last_update_at ? ` · обновлено ${esc(dateTimeText(u.last_update_at))}` : '') + '</span>'
      + ' <button class="btn sm" type="button" id="update_check_btn">Проверить</button>';
  }

  const check = $('update_check_btn');
  if (check) {
    check.addEventListener('click', async () => {
      check.disabled = true;
      const data = await checkUpdate(true);
      toast(data && data.update ? 'Есть обновление' : 'Обновлений нет',
        data && data.update ? (data.latest.title || '') : 'У вас актуальная версия');
    });
  }
  const apply = $('update_apply_btn');
  if (apply) apply.addEventListener('click', () => applyUpdate(false));

  // Настройки автообновления — рядом с карточкой.
  const rows = $('set_update_rows');
  if (rows) {
    rows.innerHTML = settingGroup([
      ['update_check_enabled', 'Проверять обновления', 'Спрашивать GitHub автоматически', 'bool'],
      ['auto_update_enabled', 'Ставить обновления сами',
        'Тихо обновляться, когда принтеры свободны, и перезапускаться', 'bool'],
      ['update_check_hours', 'Как часто проверять, часов', 'Минимум — раз в 10 минут', 'num', 1],
      ['update_branch', 'Ветка обновлений', 'Обычно main — стабильная версия', 'text'],
    ]);
  }
}

/** Поиск по настройкам: ищет сразу по всем вкладкам и прячет лишнее. */
let settingsPane = 'printers';

function selectSettingsPane(name) {
  let target = name;
  if (target === 'tax') target = 'pricing';
  if (target === 'money') target = 'business';
  if (target === 'sbp' || target === 'bank') target = 'cashier';
  const known = ['printers', 'production', 'storage', 'pricing', 'business', 'cashier', 'system', 'all', 'tax', 'money', 'sbp', 'bank'];
  if (!known.includes(target)) return;
  settingsPane = target;
  const search = $('set_search');
  if (search) search.value = '';
  $$('#set_shortcuts [data-set-shortcut]').forEach((button) => {
    button.classList.toggle('on', button.dataset.setShortcut === settingsPane);
  });
  filterSettings('');
}

function filterSettings(query) {
  const q = String(query || '').trim().toLowerCase();
  const panes = $$('[id^="setpane-"]');
  const tabs = $$('#set_tabs button');
  if (!q) {
    // Возврат к обычному режиму вкладок.
    panes.forEach((p) => {
      p.classList.toggle('on', p.id === `setpane-${settingsPane}`);
      $$('[data-set-card]', p).forEach((c) => {
        c.classList.remove('hidden');
        c.classList.remove('search-hit');
      });
      $$('[data-set-row]', p).forEach((r) => r.classList.remove('hidden'));
    });
    tabs.forEach((b) => {
      b.classList.toggle('on', b.dataset.pane === settingsPane);
      b.classList.remove('dim');
      const badge = b.querySelector('.tab-hits');
      if (badge) badge.remove();
    });
    $$('#set_shortcuts [data-set-shortcut]').forEach((button) => {
      button.classList.toggle('on', button.dataset.setShortcut === settingsPane);
    });
    $('set_no_results').hidden = true;
    return;
  }
  let found = 0;
  const hits = {};
  let firstHitPane = null;
  panes.forEach((pane) => {
    if (pane.hasAttribute('hidden')) return;
    let paneHits = 0;
    const paneName = pane.id.replace('setpane-', '');
    $$('[data-set-card]', pane).forEach((card) => {
      const head = (card.querySelector('.card-head') || {}).textContent || '';
      const headHit = head.toLowerCase().includes(q);
      let cardHits = 0;
      const rows = $$('[data-set-row]', card);
      rows.forEach((r) => {
        const hit = headHit || r.textContent.toLowerCase().includes(q);
        r.classList.toggle('hidden', !hit);
        if (hit) cardHits += 1;
      });
      const visible = cardHits > 0 || (headHit && !rows.length);
      card.classList.toggle('hidden', !visible);
      card.classList.toggle('search-hit', visible);
      if (visible) paneHits += cardHits || 1;
    });
    hits[paneName] = paneHits;
    if (paneHits > 0 && !firstHitPane) {
      firstHitPane = paneName;
    }
    found += paneHits;
  });

  // Автоматический переход к первой вкладке с совпадениями, если на текущей пусто
  let activePane = settingsPane;
  if (!hits[activePane] && firstHitPane) {
    activePane = firstHitPane;
  }

  panes.forEach((pane) => {
    if (pane.hasAttribute('hidden')) return;
    const paneName = pane.id.replace('setpane-', '');
    pane.classList.toggle('on', paneName === activePane && (hits[paneName] > 0 || found === 0));
  });

  // Вкладки в режиме поиска показывают бейджи с числом совпадений
  tabs.forEach((b) => {
    const pName = b.dataset.pane;
    const n = hits[pName] || 0;
    b.classList.toggle('on', pName === activePane);
    b.classList.toggle('dim', n === 0 && pName !== activePane);
    let badge = b.querySelector('.tab-hits');
    if (n > 0) {
      if (!badge) { badge = document.createElement('i'); badge.className = 'tab-hits'; b.appendChild(badge); }
      badge.textContent = String(n);
    } else if (badge) badge.remove();
  });
  $('set_no_results').hidden = found > 0;
}

async function resetSettings() {
  if (!confirmDanger('Вернуть настройки к заводским? Заказы, клиенты и проводки останутся на месте.')) return;
  try {
    const res = await post('/api/settings/reset', {});
    PF.setSettings(res.settings);
    PF.applyTheme();
    renderSettings();
    toast('Настройки сброшены', 'Вернулись значения по умолчанию');
    PF.refreshFinance();
    PF.refreshCore();
  } catch (e) { fail(e); }
}

async function saveSettings() {
  const payload = {};
  const jsonErrors = [];
  $$('[data-setting]').forEach((el) => {
    const k = el.dataset.setting;
    // Пустой секрет = «не менять» (сервер хранит коды/токены вне браузера)
    if (el.dataset.secret === '1' && !el.value) return;
    if (el.type === 'checkbox') payload[k] = el.checked;
    else if (el.type === 'number') payload[k] = num(el.value);
    else if (el.dataset.json === '1') {
      try { payload[k] = JSON.parse(el.value || 'null'); }
      catch (e) { jsonErrors.push(k); }
    } else if (el.tagName === 'SELECT' && !isNaN(Number(el.value)) && el.value !== '' && !['tax_mode', 'default_location', 'studio_gateway_mode', 'watch_auto_action', 'digest_time', 'weekly_report_time', 'quiet_from', 'quiet_to', 'studio_gateway_name', 'theme'].includes(k)) {
      payload[k] = num(el.value);
    } else payload[k] = el.value;
  });
  if (jsonErrors.length) {
    toast('Проверьте JSON', 'Не распознаны поля: ' + jsonErrors.join(', '));
    return;
  }
  payload.theme = $('set_theme').value;
  payload.accent = PF.state.settings.accent || 'indigo';
  try {
    const res = await post('/api/settings', payload);
    PF.setSettings(res.settings);
    PF.applyTheme();
    renderSettings();
    toast('Настройки сохранены', 'Расчёты пересчитаны по новым тарифам');
    PF.refreshFinance();
    PF.refreshCore();
    PF.refreshMoney && PF.refreshMoney();
  } catch (e) { fail(e); }
}

/* ============================================================== бэкап */
async function downloadBackup() {
  try {
    const data = await get('/api/backup');
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: 'application/json' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `printflow-копия-${U.todayISO()}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast('Копия сохранена', 'Секреты в файл не попали');
  } catch (e) { fail(e); }
}
function restoreBackup() {
  const input = $('backup_file');
  input.onchange = async () => {
    const file = input.files[0];
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text());
      const stats = await post('/api/import', payload);
      toast('Данные восстановлены', Object.entries(stats.imported || {})
        .map(([k, v]) => `${k}: ${v}`).join(', ') || 'готово');
      await PF.refreshLists();
      await PF.refreshCore();
      PF.refreshFinance();
    } catch (e) { fail(e); }
    input.value = '';
  };
  input.click();
}
/** Полные SQLite-копии базы: список и откат одной кнопкой (10.12). */
async function loadDbBackups() {
  const host = $('db_backups_list');
  if (!host) return;
  try {
    const res = await get('/api/system/backups');
    const items = res.backups || [];
    if (res.pending && res.pending.file) {
      host.innerHTML = `<div class="notice warn"><span>⏳</span><span>Запланирован откат к копии <code>${esc(res.pending.file)}</code> — выполнится после перезапуска приложения.</span></div>`;
      return;
    }
    if (!items.length) {
      host.innerHTML = '<p class="muted" style="font-size:12px;margin-top:8px">Копий пока нет. Нажмите «Копия сейчас» — и перед каждой миграцией схемы копия делается сама.</p>';
      return;
    }
    host.innerHTML = items.slice(0, 8).map((b) => `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--line,#e5e7eb)">
        <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><code>${esc(b.name)}</code><br><small class="muted">${esc(b.at)} · ${(b.size / 1048576).toFixed(1)} МБ</small></span>
        <button class="btn sm ghost" data-db-restore="${esc(b.name)}" type="button">Откатить</button>
      </div>`).join('');
    host.querySelectorAll('[data-db-restore]').forEach((btn) => {
      btn.addEventListener('click', () => restoreDbFile(btn.dataset.dbRestore));
    });
  } catch (e) { host.innerHTML = '<p class="muted" style="font-size:12px">Список недоступен: коннектор не запущен.</p>'; }
}
async function restoreDbFile(name) {
  if (!confirmDanger(`Откатить базу к копии ${name}?\n\nТекущая база будет сохранена в before-restore-*.sqlite3, приложение перезапустится.`)) return;
  try {
    await post('/api/system/restore', { file: name });
    toast('Откат запланирован', 'Приложение перезапускается…');
    setTimeout(() => location.reload(), 2500);
  } catch (e) { fail(e); }
}

/** Перенос данных старой браузерной версии PrintFlow. */
async function importLocalStorage() {
  const KEYS = ['ops_orders1', 'ops_customers1', 'ops_statuses1', 'ops_niches1',
    'catalog1', 'shelf3', 'hist1', 'plan1', 'spool1'];
  const payload = {};
  let found = 0;
  KEYS.forEach((k) => {
    const raw = store.get(k);
    if (raw) { payload[k] = raw; found++; }
  });
  if (!found) return toast('Переносить нечего', 'В браузере нет данных старой версии', 'warn');
  if (!confirmDanger(`Найдено ${found} наборов данных старой версии. Перенести их в базу коннектора?`)) return;
  try {
    const res = await post('/api/import/localstorage', payload);
    toast('Перенос завершён', Object.entries(res.imported || {}).map(([k, v]) => `${k}: ${v}`).join(', '));
    await PF.refreshLists();
    await PF.refreshCore();
    PF.refreshFinance();
  } catch (e) { fail(e); }
}
async function wipeData() {
  if (!confirmDanger('Стереть ВСЕ данные PrintFlow: заказы, клиентов, склад, финансы и журнал печати?')) return;
  if (!confirmDanger('Точно? Сначала скачайте резервную копию. Восстановить можно будет только из файла.')) return;
  try {
    for (const o of PF.state.orders) await post('/api/order/delete', { id: o.id });
    for (const c of PF.state.customers) await post('/api/customer/delete', { id: c.id });
    for (const s of PF.state.spools) await post('/api/spool/delete', { id: s.id });
    for (const c of PF.state.catalog) await post('/api/catalog/delete', { id: c.id });
    const tx = (PF.state.finance && PF.state.finance.transactions) || [];
    for (const t of tx) await post('/api/transaction/delete', { id: t.id });
    toast('Данные стёрты', 'Настройки и принтеры сохранены');
    await PF.refreshCore();
    PF.refreshFinance();
  } catch (e) { fail(e); }
}

/* =========================================================== библиотека */
const CHK_PREFIX = 'chk_';
function initLibraryChecks() {
  let i = 0;
  $$('#library-body li').forEach((li) => {
    if (li.dataset.chk) return;
    const m = li.innerHTML.match(/^\s*\[([ xX])\]\s*/);
    if (!m) return;
    const id = CHK_PREFIX + (i++);
    li.dataset.chk = id;
    li.innerHTML = li.innerHTML.replace(/^\s*\[([ xX])\]\s*/, '');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = store.get(id) === '1';
    cb.addEventListener('change', () => {
      store.set(id, cb.checked ? '1' : '0');
      updateLibProgress();
    });
    li.insertBefore(cb, li.firstChild);
  });
  updateLibProgress();
}
function updateLibProgress() {
  const all = $$('#library-body input[type=checkbox]');
  const done = all.filter((c) => c.checked).length;
  $('lib_progress').textContent = `${done} / ${all.length}`;
  $('lib_bar').style.width = (all.length ? done / all.length * 100 : 0) + '%';
  renderLibNext();
}

/* 13.1 (63): «Следующий шаг» — маршрут ведёт, а не просто показывает %.
   Находим первый неотмеченный пункт любого чек-листа и показываем карточку
   с кнопкой «Открыть гайд» (или «всё отмечено»). */
const LIB_SCROLL_PREFIX = 'printflow:library:scroll:';
const LIB_READ_KEY = 'printflow:library:read';
function libReadSet() {
  try { return new Set(JSON.parse(localStorage.getItem(LIB_READ_KEY) || '[]')); } catch (e) { return new Set(); }
}
function libReadSave(set) {
  try { localStorage.setItem(LIB_READ_KEY, JSON.stringify([...set])); } catch (e) { /* ок */ }
}
function renderLibNext() {
  const block = $('lib_next');
  if (!block) return;
  const all = $$('#library-body input[type=checkbox]');
  const firstOpen = all.find((c) => !c.checked);
  const titleEl = $('lib_next_title');
  const subEl = $('lib_next_sub');
  const btn = $('lib_next_btn');
  if (!firstOpen) {
    titleEl.textContent = 'Все чек-листы отмечены!';
    subEl.textContent = `Готово: ${all.filter((c) => c.checked).length} из ${all.length} пунктов.`;
    btn.textContent = 'К началу';
    btn.onclick = () => showArticle('');
    block.hidden = false;
    return;
  }
  const article = firstOpen.closest('.library-article');
  const name = article && article.dataset.article;
  const title = libraryArticleTitle(article) || 'Гайд';
  const heading = firstOpen.closest('h2, h3, .sechead');
  titleEl.textContent = title;
  subEl.textContent = heading ? `Отметьте: «${heading.textContent.replace(/\s+/g, ' ').trim().slice(0, 60)}»` : `Осталось ${all.length - all.filter((c) => c.checked).length} пунктов — продолжите «${title}»`;
  btn.textContent = 'Открыть гайд';
  btn.onclick = () => { if (name) showArticle(name); };
  block.hidden = false;
}

/* 13.1 (64): подсветка совпадений поиска в карточках библиотеки.
   Пересобираем только <b>/<small> — иконка и бейдж «прочитано» остаются. */
function highlightLibCard(card, query) {
  const b = card.querySelector('b');
  const small = card.querySelector('small');
  const mark = (text) => {
    if (!query) return esc(text);
    const idx = text.toLowerCase().indexOf(query);
    if (idx === -1) return esc(text);
    return esc(text.slice(0, idx)) + `<mark>${esc(text.slice(idx, idx + query.length))}</mark>` + esc(text.slice(idx + query.length));
  };
  if (b) b.innerHTML = mark(b.textContent);
  if (small) small.innerHTML = mark(small.textContent);
}

/* 13.1 (65): бейдж «прочитано» на карточках главной. */
function refreshLibReadBadges() {
  const read = libReadSet();
  $$('#lib_grid .lib-card').forEach((card) => {
    let badge = card.querySelector('.lib-read-badge');
    const name = card.dataset.article || '';
    const isRead = read.has(name);
    if (isRead && !badge) {
      badge = document.createElement('span');
      badge.className = 'lib-read-badge';
      badge.textContent = '✓ прочитано';
      card.appendChild(badge);
    }
    if (badge) badge.hidden = !isRead;
  });
}

function initCopyButtons() {
  $$('#library-body pre').forEach((pre) => {
    if (pre.querySelector('.copy')) return;
    const btn = document.createElement('button');
    btn.className = 'copy';
    btn.type = 'button';
    btn.textContent = 'Копировать';
    btn.addEventListener('click', () => {
      const code = pre.querySelector('code');
      navigator.clipboard.writeText(code ? code.innerText : pre.innerText);
      btn.textContent = 'Скопировано ✓';
      setTimeout(() => { btn.textContent = 'Копировать'; }, 1600);
    });
    pre.appendChild(btn);
  });
}
/* ===================================================== шаблоны ответов */
let replyTemplates = [];
async function loadTemplates() {
  try {
    const data = await get('/api/templates');
    replyTemplates = data.templates || [];
    renderTemplates();
  } catch (e) { /* офлайн */ }
}
function renderTemplates() {
  const host = $('lib_templates');
  if (!host) return;
  host.innerHTML = replyTemplates.map((t, i) => `<div class="set-row" data-tpl-row="${i}">`
    + `<div class="sinfo" style="flex:1;min-width:0"><b>${esc(t.title || 'Шаблон')}</b>`
    + `<small style="white-space:pre-wrap">${esc(t.text || '')}</small></div>`
    + `<button class="btn sm" type="button" data-tpl-copy="${i}">Копировать</button>`
    + `<button class="icon-btn sm" type="button" data-tpl-del="${i}">×</button></div>`).join('')
    || '<div class="empty compact"><span>Шаблонов пока нет — добавьте тексты для Авито и Telegram.</span></div>';
}
async function saveTemplates() {
  try {
    const res = await post('/api/templates/save', { templates: replyTemplates });
    replyTemplates = res.templates || [];
    renderTemplates();
    toast('Шаблоны сохранены');
  } catch (e) { fail(e); }
}
function initTemplatesEditor() {
  const host = $('lib_templates');
  if (!host) return;
  host.addEventListener('click', (e) => {
    const copy = e.target.closest('[data-tpl-copy]');
    if (copy) {
      const t = replyTemplates[+copy.dataset.tplCopy];
      if (t) { navigator.clipboard.writeText(t.text); toast('Скопировано', t.title); }
      return;
    }
    const del = e.target.closest('[data-tpl-del]');
    if (del) {
      replyTemplates.splice(+del.dataset.tplDel, 1);
      renderTemplates();
      saveTemplates();
    }
  });
  const add = $('lib_tpl_add');
  if (add) add.addEventListener('click', async () => {
    replyTemplates.push({ id: 't' + Date.now().toString(36), title: 'Новый шаблон', text: '' });
    renderTemplates();
    const row = host.querySelector('[data-tpl-row="' + (replyTemplates.length - 1) + '"]');
    if (row) {
      const ans = await ask({
        title: 'Шаблон ответа',
        fields: [
          { name: 'title', label: 'Название', type: 'text', value: 'Новый шаблон' },
          { name: 'text', label: 'Текст', type: 'textarea', value: 'Здравствуйте! Ваш заказ готов, можно забрать.' },
        ],
        ok: 'Сохранить',
      });
      if (!ans) { replyTemplates.pop(); renderTemplates(); return; }
      replyTemplates[replyTemplates.length - 1].title = ans.title;
      replyTemplates[replyTemplates.length - 1].text = ans.text;
      renderTemplates();
      saveTemplates();
    }
  });
}

/* ==================================================== конверты-накопления */
async function loadEnvelopes() {
  try {
    const data = await get('/api/envelopes');
    PF.state.envelopes = data.envelopes || [];
    renderEnvelopes();
  } catch (e) { /* офлайн */ }
}
function renderEnvelopes() {
  const host = $('set_envelopes');
  if (!host) return;
  const list = PF.state.envelopes || [];
  host.innerHTML = list.length ? list.map((e) => `<div class="set-row" data-env-row>`
    + `<div class="sinfo"><b>${esc(e.name)}</b>`
    + `<small>${nfmt(e.pct, 0)}% с дохода${e.goal ? ' · цель ' + money(e.goal) : ''}</small></div>`
    + `<div class="sinfo" style="text-align:right"><b>${money(e.balance)}</b>`
    + `<small>${e.goal_progress != null ? pct(e.goal_progress) : 'копилка'}</small></div>`
    + `<button class="btn sm" type="button" data-env-edit="${esc(e.id)}">✎</button>`
    + `<button class="btn sm" type="button" data-env-out="${esc(e.id)}">Забрать</button>`
    + `<button class="icon-btn sm danger" type="button" data-env-del="${esc(e.id)}">×</button></div>`).join('')
    : '<div class="empty compact"><span>Конвертов нет. Добавьте «Налог 6%» или «Второй принтер».</span>'
      + '<button class="btn sm primary" type="button" data-empty-click="env_add">+ Конверт</button></div>';
}
async function envSave(id) {
  const cur = (PF.state.envelopes || []).find((e) => e.id === id) || {};
  const ans = await ask({
    title: id ? 'Конверт' : 'Новый конверт',
    fields: [
      { name: 'name', label: 'Название', type: 'text', value: cur.name || '' },
      { name: 'pct', label: 'Процент с дохода', type: 'number', value: String(cur.pct ?? 0), min: 0, max: 100, hint: '0–100' },
      { name: 'goal', label: 'Цель, ₽', type: 'number', value: String(cur.goal || 0), min: 0, hint: '0 — без цели' },
    ],
    ok: 'Сохранить',
  });
  if (!ans) return;
  try {
    await post('/api/envelope/save', { id: id || '', name: ans.name, pct: num(ans.pct), goal: num(ans.goal) });
    toast('Конверт сохранён', ans.name);
    loadEnvelopes();
  } catch (e) { fail(e); }
}
async function envWithdraw(id) {
  const cur = (PF.state.envelopes || []).find((e) => e.id === id);
  if (!cur) return;
  const amount = await ask({
    title: 'Забрать из конверта',
    sub: `«${cur.name}» · остаток ${money(cur.balance)}`,
    fields: [{ name: 'amount', label: 'Сумма, ₽', type: 'number', value: '', min: 0, step: 'any' }],
    ok: 'Забрать',
  });
  if (amount == null) return;
  try {
    await post('/api/envelope/withdraw', { id, amount: num(amount), note: 'изъятие' });
    toast('Из конверта забрали', money(amount));
    loadEnvelopes();
  } catch (e) { fail(e); }
}

/* ==================================================== профили настроек 8.0 */
async function renderProfiles(){
  const host=$('set_profiles');
  if (!host) return;
  try{
    const data=await get('/api/settings/profiles');
    const list=data.profiles||[];
    host.innerHTML = list.length ? list.map(p=>`<div class="set-row"><div class="sinfo"><b>${esc(p.name)}</b><small>${esc(p.at||'')}</small></div><button class="btn sm" data-prof-restore="${esc(p.id)}">Восстановить</button><button class="icon-btn sm danger" data-prof-del="${esc(p.id)}">×</button></div>`).join('') : '<div class="empty compact"><span>Снапшотов нет — сохраните текущий набор.</span></div>';
    host.querySelectorAll('[data-prof-restore]').forEach(b=>b.addEventListener('click', async()=>{ if(!confirmDanger('Восстановить снапшот «'+b.dataset.profRestore+'»? Текущие настройки будут перезаписаны.')) return; try{ await post('/api/settings/profile/restore',{id:b.dataset.profRestore}); PF.setSettings((await get('/api/settings')).settings); renderSettings(); toast('Настройки восстановлены'); }catch(e){fail(e);} }));
    host.querySelectorAll('[data-prof-del]').forEach(b=>b.addEventListener('click', async()=>{ await post('/api/settings/profile/delete',{id:b.dataset.profDel}); renderProfiles(); }));
  }catch(e){ host.innerHTML='<div class="notice bad"><span>✕</span><span>'+esc(e.message)+'</span></div>';}
}

/* ============================================== правила «если — то» */
async function renderRules(){
  const host=$('set_rules');
  if (!host) return;
  try{
    const data=await get('/api/rules');
    const rules=data.rules||[];
    const trig=data.triggers||{}, acts=data.actions||{};
    host.innerHTML = rules.length ? rules.map(r=>{
      const cfg=r.config||{};
      const detail = r.event==='debt_overdue' ? `дней: ${num(cfg.days,14)}`
        : r.event==='order_status' ? `статус: ${esc(cfg.status||'')}`
        : (cfg.template ? `шаблон: ${esc(String(cfg.template).slice(0,60))}` : '');
      return `<div class="set-row"><div class="sinfo"><b>${esc(r.name)}</b>`
        + `<small>${esc(trig[r.event]||r.event)} → ${esc(acts[r.action]||r.action)}${detail?' · '+detail:''}${num(r.fires)?' · сработало '+nfmt(r.fires):''}</small></div>`
        + `<label class="switch"><input type="checkbox" data-rule-toggle="${esc(r.id)}"${num(r.enabled)?' checked':''}><i></i></label>`
        + `<button class="btn sm" data-rule-test="${esc(r.id)}" type="button">▶</button>`
        + `<button class="icon-btn sm danger" data-rule-del="${esc(r.id)}">×</button></div>`;
    }).join('') : '<div class="empty compact"><span>Правил нет — добавьте первое.</span></div>';

    host.querySelectorAll('[data-rule-toggle]').forEach(b=>b.addEventListener('change', async()=>{
      try{ await post('/api/rules/toggle',{id:b.dataset.ruleToggle, enabled:b.checked}); renderRules(); }
      catch(e){ fail(e); }
    }));
    host.querySelectorAll('[data-rule-del]').forEach(b=>b.addEventListener('click', async()=>{
      if(!confirmDanger('Удалить правило?')) return;
      try{ await post('/api/rules/delete',{id:b.dataset.ruleDel}); renderRules(); }catch(e){ fail(e); }
    }));
    host.querySelectorAll('[data-rule-test]').forEach(b=>b.addEventListener('click', async()=>{
      try{ await post('/api/rules/run',{id:b.dataset.ruleTest}); toast('Правило выполнено','Проверьте Telegram/журнал'); }
      catch(e){ fail(e); }
    }));
  }catch(e){ host.innerHTML='<div class="notice bad"><span>✕</span><span>'+esc(e.message)+'</span></div>'; }
}

function openRuleModal(){
  openModal('rule_modal');
  if ($('rl_event').options.length) return;
  get('/api/rules').then(d=>{
    put('rl_event',Object.entries(d.triggers||{}).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join(''));
    put('rl_action',Object.entries(d.actions||{}).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join(''));
  });
}
function saveRule(){
  const event=$('rl_event').value, action=$('rl_action').value;
  const config={template:$('rl_template').value.trim()};
  if(event==='debt_overdue') config.days=14;
  if(event==='order_status') config.status='ready';
  post('/api/rules/save',{name:$('rl_name').value.trim()||'Новое правило',event,action,config,enabled:1})
    .then(()=>{ closeModal('rule_modal'); renderRules(); toast('Правило сохранено'); })
    .catch(fail);
}

/* ==================================================== проверка данных */
async function runDataCheck() {
  const host = $('data_check_list');
  if (!host) return;
  host.innerHTML = '<div class="skeleton" style="height:36px"></div>';
  try {
    const data = await get('/api/data-check');
    if (!data.count) {
      host.innerHTML = '<div class="notice ok"><span>✓</span><span>Данные в порядке — хвостов нет.</span></div>';
    } else {
      host.innerHTML = `<div class="notice warn"><span>⚠</span><span>Найдено ${data.count} проблем:</span></div>`
        + data.problems.slice(0, 20).map((p) => `<div class="tx-row">`
          + `<span class="tx-ic expense">✕</span>`
          + `<div class="tx-body"><b>${esc(p.title)}</b><small>${esc(p.detail || '')}</small></div></div>`).join('');
    }
  } catch (e) { host.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`; }
}

const LIBRARY_CATEGORIES = {
  start: 'start', plan: 'start', models: 'start', stock: 'start',
  b2b: 'sales', avito: 'sales', tg: 'sales', posts: 'sales', content: 'sales', storefront: 'sales', vitrina: 'sales', tpl: 'sales',
  tech: 'workshop', night: 'workshop', auto: 'workshop', plastics: 'workshop', 'plastics-step': 'workshop',
  ip: 'system', legal: 'system', network: 'system', bot: 'system', 'settings-guide': 'system', faq: 'system', 'camera-cloud': 'system',
  mats: 'tools',
};
const LIBRARY_CATEGORY_LABELS = {
  start: 'Старт', sales: 'Продажи', workshop: 'Цех', system: 'Система', tools: 'Инструменты',
};
const LIBRARY_RECENT_KEY = 'printflow:library:last-article';
let libraryFilter = 'all';
/* 13.1 (65): один глобальный обработчик прокрутки — вешаем и снимаем,
   чтобы повторные открытия статей не копили слушателей. */
let libraryScrollHandler = null;

function libraryArticleById(name) {
  return $$('#library-body .library-article').find((article) => article.dataset.article === name) || null;
}
function libraryArticleTitle(article) {
  const heading = article && article.querySelector('.sechead h1');
  return heading ? heading.textContent.trim() : '';
}
function renderLibraryHome() {
  const button = $('lib_continue');
  if (!button) return;
  const name = store.get(LIBRARY_RECENT_KEY, '');
  const article = libraryArticleById(name);
  const title = libraryArticleTitle(article);
  button.hidden = !title;
  button.dataset.libraryOpen = title ? name : '';
  button.textContent = title ? `Продолжить: ${title}` : 'Продолжить чтение';
}
function rememberLibraryArticle(name, article) {
  if (!name || !article) return;
  store.set(LIBRARY_RECENT_KEY, name);
  renderLibraryHome();
}
function renderArticleNavigation(article) {
  const nav = $('lib_article_nav');
  const title = $('lib_article_nav_title');
  const links = $('lib_article_nav_links');
  if (!nav || !title || !links) return;
  if (!article) {
    nav.hidden = true;
    links.innerHTML = '';
    return;
  }
  const name = article.dataset.article || 'guide';
  const headings = Array.from(article.querySelectorAll('h2'));
  title.textContent = libraryArticleTitle(article) || 'Разделы';
  links.innerHTML = headings.map((heading, index) => {
    const text = heading.textContent.replace(/\s+/g, ' ').trim();
    const id = heading.id || `library-${name}-${index + 1}`;
    heading.id = id;
    return `<button type="button" data-library-heading="${esc(id)}" title="${esc(text)}">${esc(text)}</button>`;
  }).join('');
  nav.hidden = !headings.length;
}
function filterLibrary() {
  const input = $('lib_search');
  const count = $('lib_count');
  const empty = $('lib_empty');
  const cards = $$('#lib_grid .lib-card');
  const rawQuery = String((input || {}).value || '').trim();
  const query = rawQuery.toLowerCase();
  let visible = 0;
  cards.forEach((card) => {
    const category = card.dataset.libraryCategory || 'tools';
    const text = `${card.textContent || ''} ${card.dataset.article || ''} ${card.dataset.libraryLabel || ''}`.toLowerCase();
    const isOn = (libraryFilter === 'all' || category === libraryFilter) && (!query || text.includes(query));
    card.hidden = !isOn;
    if (isOn) visible += 1;
    // 13.1 (64): совпадения в <mark>, а не просто «нашлось/не нашлось»
    if (isOn && query) highlightLibCard(card, rawQuery);
  });
  if (count) count.textContent = query || libraryFilter !== 'all'
    ? `Найдено ${visible} из ${cards.length}`
    : `Все материалы · ${cards.length}`;
  if (empty) empty.hidden = visible > 0;
  if (!query) refreshLibReadBadges();
}

function initLibraryDiscovery() {
  const input = $('lib_search');
  const filters = $('lib_filters');
  if (!input || !filters || filters.dataset.bound) return;
  filters.dataset.bound = '1';
  $$('#lib_grid .lib-card').forEach((card) => {
    const article = card.dataset.article || '';
    const category = card.dataset.libraryCategory || LIBRARY_CATEGORIES[article] || (article ? 'system' : 'tools');
    card.dataset.libraryCategory = category;
    card.dataset.libraryLabel = LIBRARY_CATEGORY_LABELS[category] || LIBRARY_CATEGORY_LABELS.tools;
  });
  input.addEventListener('input', U.debounce(filterLibrary, 130));
  filters.addEventListener('click', (event) => {
    const button = event.target.closest('[data-lib-filter]');
    if (!button) return;
    libraryFilter = button.dataset.libFilter || 'all';
    $$('#lib_filters [data-lib-filter]').forEach((item) => item.classList.toggle('on', item === button));
    filterLibrary();
  });
  renderLibraryHome();
  filterLibrary();
}

function showArticle(name) {
  const articles = $$('#library-body .library-article');
  let activeArticle = null;
  articles.forEach((article) => {
    const isOn = article.dataset.article === name;
    article.classList.toggle('on', isOn);
    if (isOn) activeArticle = article;
  });
  // 13.1 (65): «прочитано» и возврат к месту остановки
  if (activeArticle && name) {
    const read = libReadSet();
    if (!read.has(name)) { read.add(name); libReadSave(read); }
    const saved = Number(store.get(LIB_SCROLL_PREFIX + name, '0')) || 0;
    if (saved > 4) requestAnimationFrame(() => window.scrollTo(0, saved));
  }
  const shown = Boolean(activeArticle);
  $('lib_grid').hidden = shown;
  const discovery = $('lib_discovery');
  if (discovery) discovery.hidden = shown;
  const home = $('library_home');
  if (home) home.hidden = shown;
  $('lib_back').hidden = !shown;
  const tpl = $('lib_tpl_wrap');
  if (tpl) tpl.hidden = name !== 'tpl';
  if (name === 'tpl') loadTemplates();
  renderArticleNavigation(activeArticle);
  // 13.1 (З2-6): хлебная крошка «Библиотека → гайд» в шапке раздела
  const libView = $('view-library');
  const eyebrow = libView && libView.querySelector('.view-head .eyebrow');
  if (eyebrow) {
    const title = activeArticle ? libraryArticleTitle(activeArticle) : '';
    eyebrow.textContent = title ? `Библиотека → ${title}` : 'База знаний · маршрут вместо папки файлов';
  }
  if (shown) {
    rememberLibraryArticle(name, activeArticle);
    // 13.1 (65): память прокрутки — сохраняем место, где остановились
    if (libraryScrollHandler) window.removeEventListener('scroll', libraryScrollHandler);
    libraryScrollHandler = U.debounce(() => {
      if (name) store.set(LIB_SCROLL_PREFIX + name, String(window.scrollY || 0));
    }, 300);
    window.addEventListener('scroll', libraryScrollHandler, { passive: true });
  } else {
    if (libraryScrollHandler) {
      window.removeEventListener('scroll', libraryScrollHandler);
      libraryScrollHandler = null;
    }
    renderLibraryHome();
    filterLibrary();
  }
}

/* ============================================================= события */
function bind() {
  // ПР9: клик по карточке принтера на Обзоре — выбрать его на вкладке «Принтеры».
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-dp-printer]');
    if (!btn) return;
    PF.state.activePrinter = btn.dataset.dpPrinter;
    location.hash = '#printers';
  });
  on('dash_period', 'click', async (e) => {
    const btn = e.target.closest('[data-days]');
    if (!btn) return;
    $$('#dash_period button').forEach((b) => b.classList.toggle('on', b === btn));
    PF.state.dashDays = +btn.dataset.days;
    if (PF.state.dashDays > PF.state.financeDays) await PF.refreshFinance(PF.state.dashDays);
    renderDashboard();
  });
  on('dash_chart_mode', 'click', (e) => {
    const btn = e.target.closest('[data-mode]');
    if (!btn) return;
    $$('#dash_chart_mode button').forEach((b) => b.classList.toggle('on', b === btn));
    dashMode = btn.dataset.mode;
    renderDashboard();
  });
  on('dash_refresh', 'click', async () => {
    try {
      await Promise.all([PF.refreshCore(), PF.refreshFinance(), PF.refreshEvents(), refreshTimeline(), refreshPlan(), refreshInsights()]);
      toast('Обновлено');
    } catch (e) { fail(e); }
  });
  on('dash_events_refresh', 'click', () => PF.refreshEvents().catch(fail));
  on('operator_focus_refresh', 'click', async () => {
    const button = $('operator_focus_refresh');
    button.disabled = true;
    try {
      await Promise.all([PF.refreshCore(), PF.poll()]);
      toast('Центр действий обновлён');
    } catch (e) { fail(e); }
    finally { button.disabled = false; }
  });
  const cashStrip = $('owner_cash');
  if (cashStrip) cashStrip.addEventListener('click', () => PF.go('finance'));
  on('operator_focus', 'click', (e) => {
    const fulfill = e.target.closest('[data-focus-fulfill]');
    if (fulfill) {
      if (PF.modules.ops && PF.modules.ops.openOrderFulfillment) PF.modules.ops.openOrderFulfillment(fulfill.dataset.focusFulfill);
      else if (PF.modules.ops && PF.modules.ops.openOrder) {
        PF.go('orders'); PF.modules.ops.openOrder(fulfill.dataset.focusFulfill);
      }
      return;
    }
    const action = e.target.closest('[data-focus-route]');
    if (!action) return;
    if (action.dataset.focusRoute === 'printers' && action.dataset.focusId) PF.state.activePrinter = action.dataset.focusId;
    PF.go(action.dataset.focusRoute);
  });
  // 18.11: «Объявить сейчас» — кнопка рисуется вместе со статусом шлюза.
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest && e.target.closest('#studio_announce_btn');
    if (!btn) return;
    try {
      await U.withBusy(btn, async () => {
        const r = await post('/api/studio/announce', {});
        const where = (r.targets || []).length;
        toast('Объявление отправлено', `Шлюз: ${r.sent || 0} пакетов на ${where} адрес(ов)`
          + `${r.relay ? `, станков: ${r.relay}` : ''}. Studio обновит список устройств в течение нескольких секунд.`);
      });
    } catch (err) { fail(err); }
  });
  on('dash_hero_pult', 'click', async (e) => {
    const pill = e.target.closest('[data-hero-printer]');
    if (pill) {
      PF.state.dashHeroPrinterId = pill.dataset.heroPrinter;
      renderActivePrint();
      return;
    }
    const cmdBtn = e.target.closest('[data-hero-cmd]');
    if (cmdBtn) {
      const pid = cmdBtn.dataset.pid;
      const cmd = cmdBtn.dataset.heroCmd;
      await execHeroCommand(pid, cmd, cmdBtn);
      return;
    }
    const speedBtn = e.target.closest('[data-hero-speed]');
    if (speedBtn) {
      const pid = speedBtn.dataset.pid;
      const level = parseInt(speedBtn.dataset.heroSpeed, 10);
      await execHeroSpeed(pid, level, speedBtn);
      return;
    }
    const startBtn = e.target.closest('[data-hero-start-job]');
    if (startBtn) {
      const jobId = startBtn.dataset.heroStartJob;
      const pid = startBtn.dataset.pid;
      await execHeroStartJob(jobId, pid, startBtn);
      return;
    }
    const convOrder = e.target.closest('[data-convert-order]');
    if (convOrder) {
      if (PF.modules.printer && PF.modules.printer.convertActiveToOrder) {
        PF.modules.printer.convertActiveToOrder(convOrder.dataset.convertOrder);
      } else {
        PF.go('printers');
      }
      return;
    }
  });

  on('dash_analytics_toggle', 'click', () => {
    const sec = $('dash_analytics_section');
    const btn = $('dash_analytics_toggle');
    if (!sec || !btn) return;
    const willShow = sec.classList.contains('hidden');
    sec.classList.toggle('hidden', !willShow);
    btn.setAttribute('aria-expanded', String(willShow));
    const txt = $('dash_analytics_toggle_txt');
    const arr = $('dash_analytics_arr');
    if (txt) txt.textContent = willShow ? 'Скрыть аналитику и графики' : 'Показать аналитику и графики смены';
    if (arr) arr.textContent = willShow ? '▴' : '▾';
    store.set('pf_dash_analytics_open', willShow ? '1' : '0');
  });
  if (store.get('pf_dash_analytics_open') === '1') {
    const sec = $('dash_analytics_section');
    const btn = $('dash_analytics_toggle');
    if (sec) sec.classList.remove('hidden');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    const txt = $('dash_analytics_toggle_txt');
    const arr = $('dash_analytics_arr');
    if (txt) txt.textContent = 'Скрыть аналитику и графики';
    if (arr) arr.textContent = '▴';
  }

  on('dash_widgets_btn', 'click', () => { renderWidgetsList(); openModal('dash_widgets_modal'); });
  on('dash_widgets_list', 'change', (e) => {
    const cb = e.target.closest('[data-widget-check]');
    if (!cb) return;
    let prefs = widgetPrefs();
    if (cb.checked) { if (!prefs.includes(cb.dataset.widgetCheck)) prefs.push(cb.dataset.widgetCheck); }
    else prefs = prefs.filter((id) => id !== cb.dataset.widgetCheck);
    saveWidgetPrefs(prefs);
    applyWidgets();
    renderWidgetsList();
  });
  on('dash_widgets_reset', 'click', () => {
    saveWidgetPrefs(DASH_WIDGETS.map(([id]) => id));
    renderWidgetsList();
    applyWidgets();
    toast('Все виджеты возвращены');
  });
  on('dash_pdf', 'click', () => window.print());

  on('settings_save', 'click', saveSettings);
  on('settings_reset', 'click', resetSettings);
  on('mat_add', 'click', () => openMaterial(''));
  on('mat_save', 'click', saveMaterial);
  on('mat_reset', 'click', async () => {
    const m = materialsFull.find((x) => x.id === editingMaterial);
    if (!m || !m.builtin) return;
    if (!confirmDanger(`Вернуть «${m.name}» к заводским параметрам каталога?`)) return;
    try {
      await post('/api/materials/reset', { id: m.id });
      closeModal('material_modal');
      toast('Материал сброшен', `${m.name} — параметры каталога`);
      await loadMaterials();
      if (PF.modules.money && PF.modules.money.loadCalcMaterials) PF.modules.money.loadCalcMaterials();
    } catch (e) { fail(e); }
  });
  on('set_materials', 'click', (e) => {
    const edit = e.target.closest('[data-mat-edit]');
    const del = e.target.closest('[data-mat-del]');
    if (edit) return openMaterial(edit.dataset.matEdit);
    if (del) return deleteMaterial(del.dataset.matDel);
  });
  on('mat_base', 'change', (e) => fillMaterialFromBase(e.target.value));
  document.addEventListener('click', (e) => {
    const presetBtn = e.target.closest('[data-watch-preset]');
    if (presetBtn) {
      const p = presetBtn.dataset.watchPreset;
      const inp = document.querySelector('[data-setting="watch_folder_path"]');
      if (inp) {
        if (p === 'default') inp.value = '~/PrintFlow-Inbox';
        else if (p === 'desktop') inp.value = '~/Desktop/3MF';
        else if (p === 'win') inp.value = 'C:\\PrintFlow-Inbox';
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
  });
  on('set_tabs', 'click', (e) => {
    const btn = e.target.closest('[data-pane]');
    if (btn) selectSettingsPane(btn.dataset.pane);
  });
  const settingShortcuts = $('set_shortcuts');
  if (settingShortcuts) settingShortcuts.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-set-shortcut]');
    if (!btn) return;
    selectSettingsPane(btn.dataset.setShortcut);
    const tabs = $('set_tabs');
    if (tabs && typeof tabs.scrollIntoView === 'function') {
      tabs.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
  const settingsHost = $('view-settings');
  if (settingsHost) {
    const syncSettingEl = (e) => {
      const k = e.target.dataset && e.target.dataset.setting;
      if (!k || e.target.type === 'file') return;
      const isCb = e.target.type === 'checkbox';
      const val = isCb ? e.target.checked : e.target.value;
      $$(`[data-setting="${k}"]`, settingsHost).forEach((el) => {
        if (el === e.target) return;
        if (isCb && el.type === 'checkbox') el.checked = val;
        else if (!isCb && el.type !== 'checkbox') el.value = val;
      });
    };
    settingsHost.addEventListener('input', syncSettingEl);
    settingsHost.addEventListener('change', syncSettingEl);
  }
  on('set_search', 'input', U.debounce((e) => filterSettings(e.target.value), 150));
  on('set_tax_mode', 'change', (e) => {
    // Показываем поля выбранного режима сразу, не дожидаясь сохранения.
    PF.state.settings.tax_mode = e.target.value;
    renderSettings();
    $('set_search').value = '';
    filterSettings('');
  });
  on('set_accent', 'click', (e) => {
    const btn = e.target.closest('[data-accent]');
    if (!btn) return;
    PF.state.settings.accent = btn.dataset.accent;
    PF.applyTheme();
    $$('#set_accent button').forEach((b) => b.classList.toggle('on', b === btn));
  });
  on('set_theme', 'change', (e) => {
    PF.state.settings.theme = e.target.value;
    PF.applyTheme();
  });
  on('set_printer_add', 'click', () => PF.modules.printer.openPrinterModal());
  if ($('rule_add')) on('rule_add', 'click', openRuleModal);
  if ($('rule_save')) on('rule_save', 'click', saveRule);
  on('set_printers', 'click', (e) => {
    const btn = e.target.closest('[data-printer-edit]');
    if (btn) PF.modules.printer.openPrinterModal(btn.dataset.printerEdit);
  });
  const permBtn = $('notify_perm_btn');
  if (permBtn) permBtn.addEventListener('click', requestBrowserNotify);
  on('tg_test', 'click', async () => {
    const token = $$('[data-setting="telegram_token"]')[0].value;
    const chat = $$('[data-setting="telegram_chat_id"]')[0].value;
    try {
      const res = await post('/api/telegram/test', { telegram_token: token, telegram_chat_id: chat, telegram_enabled: true });
      if (res.ok) toast('Сообщение отправлено', 'Проверьте Telegram');
      else fail(new Error(res.error || 'Telegram не ответил'));
    } catch (e) { fail(e); }
  });

  on('backup_download', 'click', downloadBackup);
  on('backup_restore', 'click', restoreBackup);
  on('backup_import_ls', 'click', importLocalStorage);
  on('backup_wipe', 'click', wipeData);
  on('db_backup_now', 'click', async () => {
    try {
      const res = await post('/api/system/backup', {});
      if (!res.ok) return fail(new Error(res.error || 'Копия не создалась'));
      toast('Копия базы создана', res.file);
      loadDbBackups();
    } catch (e) { fail(e); }
  });
  loadDbBackups();
  const profSave=$('prof_save');
  if (profSave) profSave.addEventListener('click', async()=>{ const name=await ask({title:'Снапшот настроек',fields:[{name:'name',label:'Название',type:'text',value:'Снапшот '+new Date().toLocaleString('ru-RU')}],ok:'Сохранить'}); if(name==null) return; try{ await post('/api/settings/profile/save',{name}); renderProfiles(); toast('Снапшот сохранён', name);}catch(e){fail(e);} });
  on('data_check_btn', 'click', runDataCheck);

  on('env_add', 'click', () => envSave(''));
  on('set_envelopes', 'click', (e) => {
    const edit = e.target.closest('[data-env-edit]');
    if (edit) { envSave(edit.dataset.envEdit); return; }
    const out = e.target.closest('[data-env-out]');
    if (out) { envWithdraw(out.dataset.envOut); return; }
    const del = e.target.closest('[data-env-del]');
    if (del && confirmDanger('Удалить конверт? Движения сохранятся в истории.')) {
      post('/api/envelope/delete', { id: del.dataset.envDel }).then(() => loadEnvelopes()).catch(fail);
    }
  });

  initLibraryDiscovery();
  on('library_home', 'click', (e) => {
    const button = e.target.closest('[data-library-open]');
    if (button && button.dataset.libraryOpen) PF.go('library', button.dataset.libraryOpen);
  });
  on('lib_grid', 'click', (e) => {
    const card = e.target.closest('[data-article]');
    if (!card) return;
    e.preventDefault();
    PF.go('library', card.dataset.article);
  });
  on('lib_back', 'click', () => PF.go('library'));
  on('lib_reset_filters', 'click', () => {
    libraryFilter = 'all';
    $('lib_search').value = '';
    $$('#lib_filters [data-lib-filter]').forEach((button) => button.classList.toggle('on', button.dataset.libFilter === 'all'));
    filterLibrary();
  });
  on('lib_article_nav_links', 'click', (e) => {
    const button = e.target.closest('[data-library-heading]');
    const heading = button && document.getElementById(button.dataset.libraryHeading);
    if (heading) heading.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  on('lib_article_top', 'click', () => {
    const article = document.querySelector('#library-body .library-article.on');
    if (article) article.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

/* ============================================== 8.5: статус-бар цеха (#75) */
function renderCycobar() {
  const bar = $('cycobar');
  if (!bar) return;
  bar.hidden = false;
  const live = PF.state.live;
  const snap = (live && (live.active || (live.printers || [])[0])) || null;
  const info = (snap && snap.printer) || {};
  const running = ['RUNNING', 'PAUSE', 'PREPARE'].includes(info.state);
  const print = $('cb_print_text');
  const dot = $('cb_print').querySelector('.cb-dot');
  if (running) {
    dot.className = 'cb-dot' + (info.state === 'PAUSE' ? ' bad' : '');
    print.textContent = `${info.task || 'печать'} · ${Math.round(num(info.progress))}%`;
  } else if (snap) {
    dot.className = 'cb-dot idle';
    print.textContent = `${snap.name} · ${info.state_label || 'свободен'}`;
  } else {
    dot.className = 'cb-dot idle';
    print.textContent = 'принтеры не подключены';
  }
  $('cb_prog').style.width = (running ? clamp(num(info.progress), 0, 100) : 0) + '%';
  $('cb_queue').textContent = (PF.state.jobs.queue || []).length;
  const shelfText = (PF.modules.shelf && PF.modules.shelf.shelfSummary) || null;
  if (shelfText) $('cb_shelf').textContent = shelfText;
  const h = cycobarHeartbeat;
  $('cb_dot').className = 'cb-dot' + (h && h.ok === false ? ' bad' : '');
}

/* ============================================== 8.5: здоровье (#36) */
let cycobarHeartbeat = null;
async function refreshHeartbeat() {
  const el = $('dash_heartbeat');
  try {
    const h = await get('/api/system/heartbeat');
    cycobarHeartbeat = h;
    if (!el) { renderCycobar(); return; }
    const rows = [];
    rows.push(`<div class="mini-row"><span class="dot ${h.telegram.ok ? 'on' : ''}"></span>`
      + `<div class="mbody"><b>Telegram-бот</b><small>${esc(h.telegram.note || '')}</small></div>`
      + `<span class="chip ${h.telegram.ok ? 'ok' : 'warn'}">${h.telegram.ok ? 'жив' : 'нет'}</span></div>`);
    (h.printers || []).forEach((p) => {
      rows.push(`<div class="mini-row"><span class="dot ${p.connected ? 'on' : 'bad'}"></span>`
        + `<div class="mbody"><b>${esc(p.name)}${p.virtual ? ' (вирт.)' : ''}</b>`
        + `<small>${p.connected ? 'на связи' : esc(p.last_error || 'нет связи')}</small></div>`
        + `<span class="chip ${p.connected ? 'ok' : 'warn'}">${p.connected ? 'ok' : 'нет'}</span></div>`);
    });
    rows.push(`<div class="mini-row"><span class="dot ${h.db_exists ? 'on' : 'bad'}"></span>`
      + `<div class="mbody"><b>База и бэкапы</b>`
      + `<small>последняя копия ${h.backup_newest_at ? agoText(h.backup_newest_at) : 'нет'}${h.backup_age_h != null ? ` · ${nfmt(h.backup_age_h)} ч назад` : ''}</small></div>`
      + `<span class="chip ${h.db_exists ? 'ok' : 'warn'}">${h.db_exists ? 'ok' : 'нет'}</span></div>`);
    if (h.disk) rows.push(`<div class="mini-row"><span class="dot ${h.disk.ok === false ? 'bad' : 'on'}"></span>`
      + `<div class="mbody"><b>Диск</b><small>${nfmt(h.disk.free_gb, 1)} ГБ свободно${h.disk.error ? ' · ' + esc(h.disk.error) : ''}</small></div>`
      + `<span class="chip ${h.disk.ok === false ? 'warn' : 'outline'}">${nfmt(h.disk.used_pct)}% занято</span></div>`);
    const mqtt = h.mqtt || (h.channels && h.channels.mqtt) || { ok: true, printers: [] };
    const ftps = h.ftps || (h.channels && h.channels.ftps) || { ok: true, printers: [] };
    rows.push(`<div class="mini-row"><span class="dot ${mqtt.ok ? 'on' : 'bad'}"></span>`
      + `<div class="mbody"><b>MQTT</b><small>${mqtt.ok ? 'принтеры на связи' : esc((mqtt.printers || []).filter((x) => !x.ok).map((x) => x.error || x.name).join(', ') || 'молчит')}</small></div>`
      + `<span class="chip ${mqtt.ok ? 'ok' : 'warn'}">${mqtt.ok ? 'ok' : 'нет'}</span></div>`);
    rows.push(`<div class="mini-row"><span class="dot ${ftps.ok ? 'on' : 'bad'}"></span>`
      + `<div class="mbody"><b>FTPS / SD</b><small>${ftps.ok ? 'карта доступна' : esc((ftps.printers || []).filter((x) => !x.ok).map((x) => x.error || x.name).join(', ') || 'нет ответа')}</small></div>`
      + `<span class="chip ${ftps.ok ? 'ok' : 'warn'}">${ftps.ok ? 'ok' : 'нет'}</span></div>`);
    el.innerHTML = rows.join('');
    if (PF.ui.setChannelBar) PF.ui.setChannelBar(h.channels || { mqtt, ftps, disk: h.disk });
    renderCycobar();
  } catch (e) {
    if (el) el.innerHTML = `<span class="muted">${esc(e.message)}</span>`;
  }
}

/* ============================================== 8.5: достижения (#90) */
async function refreshAchievements() {
  const el = $('dash_achievements');
  try {
    const d = await get('/api/achievements');
    const done = (d.badges || []).filter((b) => b.achieved);
    el.innerHTML = (d.badges || []).map((b) =>
      `<span class="mk-badge${b.achieved ? ' on' : ''}" title="${esc(b.desc)}">`
      + `${b.achieved ? '★' : '☆'} ${esc(b.title)}<small class="muted">${nfmt(b.progress)}/${nfmt(b.target)}</small></span>`
    ).join('');
    if ($('dash_ach_sub')) $('dash_ach_sub').textContent = `Открыто ${done.length} из ${(d.badges || []).length}`;
  } catch (e) { el.textContent = '—'; }
}

/* ============================================== 8.5: NOZZA tour (#27) */
async function renderTour() {
  const el = $('set_tour');
  if (!el) return;
  let st = {};
  try { st = await get('/api/tour/state'); } catch (e) { /* старый коннектор */ }
  if (st.active) {
    el.innerHTML = `<div class="notice" style="margin-bottom:8px"><span>🎬</span><span>Демо активно: виртуальный принтер и демо-данные работают. Откат вернёт базу из копии ${esc(st.backup || '')}.</span></div>`
      + `<button class="btn danger wide" type="button" id="tour_stop">■ Завершить tour и вернуть мои данные</button>`;
    on('tour_stop', 'click', async () => {
      if (!confirmDanger('Завершить NOZZA tour? Приложение перезапустится с исходной базой.')) return;
      try { const r = await post('/api/tour/stop', {}); toast('Tour завершён', r.message || 'Перезапуск…'); }
      catch (e) { fail(e); }
    });
    return;
  }
  el.innerHTML = `<p class="muted" style="font-size:12.5px;margin-bottom:8px">Покажите PrintFlow гостю: симулятор P1S печатает, заказы и полка живые, но всё — демо-данные. Завершение одной кнопкой откатывает базу.</p>`
    + `<button class="btn primary wide" type="button" id="tour_start">▶ Запустить NOZZA tour</button>`;
  on('tour_start', 'click', async () => {
    try { const r = await post('/api/tour/start', {}); toast('Tour запущен', r.job_started ? 'Виртуальная печать стартовала' : 'Демо-данные готовы'); renderSettings(); }
    catch (e) { fail(e); }
  });
}

/* =============================================================== старт */
PF.on('ready', () => {
  bind();
  renderSettings();
  initLibraryChecks();
  initCopyButtons();
  initDashDrag();
  initTemplatesEditor();
  loadEnvelopes();
  refreshTimeline();
  refreshPlan();
  refreshInsights();
  loadTemplates();
  initBrowserNotify();
  checkUpdate(false);
  renderCycobar();
  refreshHeartbeat();
  refreshAchievements();
  setInterval(refreshTimeline, 60000);
  setInterval(refreshPlan, 60000);
  setInterval(refreshInsights, 90000);
  setInterval(refreshHeartbeat, 60000);
  setInterval(refreshAchievements, 300000);
});
/* 14.0 (идея 45): перерисовка только видимого раздела. Телеметрия
   печатящего принтера приходила пачками и заставляла перерисовывать
   заказы, клиентов, склад, каталог и контент — все 19 разделов сразу.
   Шапка (cycobar) видна всегда, поэтому она без охраны. */
PF.on('data', PF.whenView('dashboard', renderDashboard));
PF.on('data', renderCycobar);
PF.on('live', PF.whenView('dashboard', renderDashboard));
PF.on('live', renderCycobar);
PF.on('finance', PF.whenView('dashboard', renderDashboard));
PF.on('money', PF.whenView('dashboard', renderDashboard));
PF.on('events', PF.whenView('dashboard', renderEvents));
PF.on('printers', PF.whenView('settings', renderSettings));
PF.on('printers', renderCycobar);
PF.on('bootstrap', PF.whenView('settings', renderSettings));
PF.on('money', () => { if (document.querySelector('#view-settings.on')) renderSettings(); });
PF.on('view', (d) => {
  // 18.6.3: каждый шаг — сам по себе. Падение отрисовки настроек больше
  // не отменяет загрузку материалов и тура (и наоборот).
  if (d.view === 'library') showArticle(d.sub || '');
  if (d.view === 'settings') {
    try { renderSettings(); } catch (e) { console.error('renderSettings:', e); }
    try { loadMaterials(); } catch (e) { console.error('loadMaterials:', e); }
    try { renderTour(); } catch (e) { console.error('renderTour:', e); }
  }
  if (d.view === 'dashboard') {
    renderDashboard();
    if (PF.refreshMoney) PF.refreshMoney();
  }
});
window.addEventListener('resize', U.debounce(() => {
  if (document.querySelector('#view-dashboard.on')) renderDashboard();
  if (document.querySelector('#view-finance.on') && PF.modules.money) PF.modules.money.renderFinance();
}, 220));

/* Н1: карточка «Самодиагностика» — один снимок состояния коннектора.
   Данные берём из /api/diagnostics: тот же источник, что у бота и pf doctor,
   поэтому панель не может показать здоровье, отличное от реального. */
let diagReport = null;
function diagRow(ok, title, note, badge) {
  return `<div class="mini-row"><span class="dot ${ok ? 'on' : 'bad'}"></span>`
    + `<div class="mbody"><b>${esc(title)}</b><small>${esc(note || '')}</small></div>`
    + `<span class="chip ${ok ? 'ok' : 'warn'}">${esc(badge || (ok ? 'ок' : 'внимание'))}</span></div>`;
}
function renderDiag(report) {
  const host = $('set_diag');
  if (!host) return;
  diagReport = report || diagReport;
  const r = diagReport;
  if (!r) { host.innerHTML = '<span>Нет данных.</span>'; return; }
  const rows = [];
  const threads = r.threads || {};
  const missing = threads.missing || [];
  rows.push(diagRow(!missing.length, 'Потоки',
    `всего ${threads.total || 0}` + (missing.length ? ` · не запущены: ${missing.join(', ')}` : ' · все на месте'),
    missing.length ? `${missing.length} нет` : 'ок'));
  const db = r.database || {};
  rows.push(diagRow(!!db.exists, 'База данных',
    `${nfmt((db.size || 0) / 1024)} КБ · таблиц ${db.tables || 0} · WAL ${nfmt((db.wal_size || 0) / 1024)} КБ`
    + ` · схема ${db.schema_version || 0} из ${(r.schema || {}).current || 0}`,
    (r.schema || {}).matches === false ? 'схема' : 'ок'));
  const backups = r.backups || {};
  rows.push(diagRow((backups.count || 0) > 0, 'Резервные копии',
    `${backups.count || 0} шт.` + (backups.last_at ? ` · последняя ${agoText(backups.last_at)}` : ' · копий нет'),
    backups.count ? 'ок' : 'нет'));
  const errors = r.errors || {};
  rows.push(diagRow((errors.errors || 0) === 0, 'Ошибки в журнале',
    `${errors.errors || 0} за ${errors.window_hours || 24} ч`,
    errors.errors ? String(errors.errors) : 'ок'));
  const outbox = r.outbox || {};
  if (outbox && outbox.total_pending != null) {
    const stuck = (outbox.staff && outbox.staff.pending) || 0;
    const client = (outbox.client && outbox.client.pending) || 0;
    const lastErr = (outbox.staff && outbox.staff.last_error)
      || (outbox.client && outbox.client.last_error) || '';
    rows.push(diagRow(outbox.total_pending === 0, 'Очередь в Telegram',
      `сотрудникам ${stuck} · покупателям ${client}` + (lastErr ? ` · ${lastErr}` : ''),
      outbox.total_pending === 0 ? 'ок' : String(outbox.total_pending)));
  }
  const farm = r.farm || {};
  if (farm && farm.printers != null) {
    rows.push(diagRow(true, 'Парк принтеров',
      `${farm.online || 0}/${farm.printers || 0} онлайн · печатают ${farm.printing || 0} · в очереди ${r.queue || 0}`,
      `${farm.online || 0}/${farm.printers || 0}`));
  }
  const services = r.services || {};
  rows.push(diagRow(true, 'Сервисы',
    ['бот сотрудников', 'клиент-бот', 'облачный мост', 'шлюз Studio']
      .filter((_, i) => [services.telegram_bot, services.client_bot,
        services.bambu_cloud, services.studio_gateway][i]).join(', ') || 'ничего не настроено',
    'инфо'));
  const rt = r.router || {};
  host.innerHTML = rows.join('')
    + `<p class="muted" style="font-size:12px;margin-top:8px">PrintFlow ${esc(r.version || '')}`
    + ` · Python ${esc(r.python || '')} · аптайм ${Math.floor((r.uptime_sec || 0) / 60)} мин`
    + ` · маршрутов ${rt.registered || 0} · снято ${esc(r.at || '')}</p>`;
}
async function refreshDiag() {
  const host = $('set_diag');
  if (!host) return;
  try {
    const report = await get('/api/diagnostics');
    renderDiag(report);
  } catch (error) {
    host.innerHTML = `<span>Не удалось снять показания: ${esc(error.message || error)}</span>`;
  }
}
PF.on('view', (d) => {
  if (d.view !== 'settings') return;
  refreshDiag();
  const refresh = $('diag_refresh');
  if (refresh && !refresh.dataset.bound) {
    refresh.dataset.bound = '1';
    refresh.onclick = () => refreshDiag();
  }
  const copy = $('diag_copy');
  if (copy && !copy.dataset.bound) {
    copy.dataset.bound = '1';
    copy.onclick = async () => {
      try {
        const text = await get('/api/diagnostics/report');
        if (navigator.clipboard) await navigator.clipboard.writeText(text.text || '');
        toast('Отчёт скопирован', 'Можно вставить в чат поддержки', 'ok');
      } catch (error) { fail(error); }
    };
  }
});


/* ================================================= 15.1 (В-серия) */

/* В2: карты-шторки Обзора — карточка раскрывается на месте, контекст
   страницы не теряется. Состояние шторки запоминается по виджету. */
function initDashboardFolds() {
  const dash = document.getElementById('view-dashboard');
  if (!dash || dash.dataset.foldsReady === '1') return;
  dash.dataset.foldsReady = '1';
  $$('.card[data-widget]', dash).forEach((cardEl) => {
    const head = cardEl.querySelector('.card-head');
    if (!head || head.querySelector('.fold-caret')) return;
    const fold = document.createElement('div');
    fold.className = 'card-fold';
    [...cardEl.children].forEach((child) => {
      if (child !== head && !child.classList.contains('card-fold')) fold.appendChild(child);
    });
    cardEl.appendChild(fold);
    const caret = document.createElement('span');
    caret.className = 'fold-caret';
    caret.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 14.5l6-5.5 6 5.5"/></svg>';
    head.appendChild(caret);
    const widget = cardEl.dataset.widget || '';
    if (U.store.get('pf.fold.' + widget, '0') === '1') cardEl.classList.add('folded');
    cardEl.classList.add('card-foldable');
    head.addEventListener('click', (e) => {
      if (e.target.closest('a, button, input, select, label')) return;
      cardEl.classList.toggle('folded');
      U.store.set('pf.fold.' + widget, cardEl.classList.contains('folded') ? '1' : '0');
    });
  });
}
initDashboardFolds();

/* В82: превью-макеты генераторов в Библиотеке — плитка «как будет
   выглядеть» рисуется стилями по типу материала. */
const GEN_THUMB_CLASS = [
  ['вывеси', 'g-sign'], ['воблеры', 'g-wobbler'], ['визитки', 'g-card'],
  ['постеры', 'g-poster'], ['ценники', 'g-tag'], ['наклейки', 'g-tag'],
  ['сертификаты', 'g-card'], ['шапки', 'g-poster'], ['авито', 'g-card'],
  ['контент-план', 'g-poster'], ['памятка', 'g-sign'], ['о-нас', 'g-poster'],
];
function decorateGeneratorCards() {
  $$('.mat').forEach((mat) => {
    if (mat.querySelector('.gen-thumb')) return;
    const href = (mat.querySelector('a[href]') || {}).getAttribute?.('href') || '';
    const hit = GEN_THUMB_CLASS.find(([key]) => href.includes(key));
    if (!hit) return;
    const thumb = document.createElement('div');
    thumb.className = 'gen-thumb ' + hit[1];
    thumb.setAttribute('aria-hidden', 'true');
    mat.insertBefore(thumb, mat.firstChild);
  });
}

/* В79/В80/В81: галерея 3D-моделей в Библиотеке — мозаика, облако фасетов
   и шторное сравнение версий. Модели берутся из реестра /api/models. */
const modelState = { loaded: false, items: [], facets: new Set() };

function modelFacets(m) {
  const tags = [];
  if (m.complexity) tags.push('сложность: ' + m.complexity);
  if (m.source) tags.push('источник: ' + m.source);
  if (m.nom_id) tags.push('в товарах');
  const dims = [num(m.dim_x), num(m.dim_y), num(m.dim_z)].filter((v) => v > 0);
  if (dims.length === 3) tags.push(Math.max(...dims) > 120 ? 'крупная' : (Math.max(...dims) < 40 ? 'мелкая' : 'средняя'));
  if (num(m.versions_count, 0) > 1 || num(m.version, 0) > 1) tags.push('есть версии');
  return tags;
}

function renderModelGallery() {
  const host = document.getElementById('model_gallery');
  if (!host) return;
  const items = modelState.items.filter((m) => {
    if (!modelState.facets.size) return true;
    const tags = modelFacets(m);
    return [...modelState.facets].every((f) => tags.includes(f));
  });
  const cloud = document.getElementById('model_facets');
  if (cloud) {
    const freq = new Map();
    modelState.items.forEach((m) => modelFacets(m).forEach((t) => freq.set(t, (freq.get(t) || 0) + 1)));
    cloud.innerHTML = [...freq.entries()].sort((a, b) => b[1] - a[1]).map(([tag, cnt]) =>
      `<button class="tg${modelState.facets.has(tag) ? ' on' : ''}" type="button" data-facet="${esc(tag)}">${esc(tag)}<small>${cnt}</small></button>`).join('');
  }
  if (!modelState.items.length) {
    host.innerHTML = '<div class="empty compact"><span>◇</span><b>Моделей в реестре нет</b>'
      + '<span>Реестр наполняется из конструктора изделий и карточек товаров.</span></div>';
    return;
  }
  host.innerHTML = items.length ? items.map((m) => {
    const dims = [num(m.dim_x), num(m.dim_y), num(m.dim_z)].filter((v) => v > 0);
    return `<article class="model-tile" data-model-open="${esc(m.id)}" title="Сравнить версии (В81)">`
      + `<div class="mt-art"><i data-icon="cube">◇</i></div>`
      + `<h4>${esc(m.name || 'Модель')}</h4>`
      + `<div class="mt-meta"><span class="chip outline">v${esc(m.version || '1.0')}</span>`
      + (dims.length === 3 ? `<span>${nfmt(dims[0], 1)}×${nfmt(dims[1], 1)}×${nfmt(dims[2], 1)} мм</span>` : '')
      + (m.updated_at ? `<span>${esc(U.dateText(m.updated_at))}</span>` : '')
      + '</div></article>';
  }).join('')
    : '<div class="empty compact"><span>⌕</span><b>Под выбранные фасеты ничего не подошло</b><span>Снимите пару меток в облаке.</span></div>';
}

async function loadModelGallery() {
  const host = document.getElementById('model_gallery');
  if (!host) return;
  if (!modelState.loaded) host.innerHTML = U.skeletonStack(4);
  try {
    const data = await get('/api/models');
    modelState.items = (data && data.models) || [];
    modelState.loaded = true;
    renderModelGallery();
  } catch (e) {
    host.innerHTML = `<div class="empty compact"><span>!</span><b>Реестр моделей недоступен</b><span>${esc(e.message || '')}</span></div>`;
  }
}

/* В81: шторное сравнение двух версий модели. */
async function openModelCompare(modelId) {
  let model = null;
  try {
    model = await get('/api/model', { id: modelId });
  } catch (e) { return fail(e); }
  if (!model) return;
  const versions = (model.versions && model.versions.length ? model.versions : [{ version: model.version, note: model.notes || '' }]).slice(-3);
  if (versions.length < 2) {
    toast('Версий пока одна', 'Сравнение появится после второй версии модели', 'info');
    return;
  }
  const left = versions[versions.length - 2];
  const right = versions[versions.length - 1];
  const pane = (v, title) => `<div class="cmp-pane"><div>`
    + `<svg class="cmp-dim" viewBox="0 0 200 110" aria-hidden="true">`
    + `<rect x="30" y="18" width="140" height="74" rx="6" fill="none" stroke="var(--line-strong)"/>`
    + `<path d="M30 100h140M100 18v74" stroke="var(--line)" stroke-dasharray="4 4"/>`
    + `<text x="100" y="12" text-anchor="middle" font-size="9" fill="var(--muted)">${esc(String(num(model.dim_x, 0)))} мм</text>`
    + `<text x="8" y="58" font-size="9" fill="var(--muted)">${esc(String(num(model.dim_y, 0)))}</text></svg>`
    + `<b style="display:block;text-align:center;margin-top:8px">${esc(title)} v${esc(v.version || '')}</b>`
    + `<small class="muted" style="display:block;text-align:center">${esc(v.note || v.file || '')}</small></div></div>`;
  let box = document.getElementById('model_cmp_modal');
  if (!box) {
    box = document.createElement('dialog');
    box.id = 'model_cmp_modal';
    box.className = 'modal wide';
    box.innerHTML = '<div class="card pad-0" style="padding:18px">'
      + '<div class="card-head"><div><h2 id="model_cmp_title">Сравнение версий</h2>'
      + '<p>Тяните шторку: слева — предыдущая версия, справа — новая</p></div>'
      + '<button class="icon-btn" type="button" data-close="model_cmp_modal" aria-label="Закрыть">×</button></div>'
      + '<div class="cmp-wrap" id="model_cmp_wrap"></div>'
      + '<input class="cmp-range" id="model_cmp_range" type="range" min="5" max="95" value="50" aria-label="Положение шторки">'
      + '<div class="cmp-labels"><span id="model_cmp_l"></span><span id="model_cmp_r"></span></div></div>';
    document.body.appendChild(box);
  }
  const wrap = box.querySelector('#model_cmp_wrap');
  wrap.style.setProperty('--cmp', '50%');
  wrap.innerHTML = pane(right, 'Новая') + `<div class="cmp-top">${pane(left, 'Прежняя')}</div><div class="cmp-divider"></div>`;
  box.querySelector('#model_cmp_title').textContent = `Сравнение версий · ${model.name || 'модель'}`;
  box.querySelector('#model_cmp_l').textContent = `Прежняя: v${left.version || ''}`;
  box.querySelector('#model_cmp_r').textContent = `Новая: v${right.version || ''}`;
  const range = box.querySelector('#model_cmp_range');
  range.value = '50';
  range.oninput = () => wrap.style.setProperty('--cmp', range.value + '%');
  openModal('model_cmp_modal');
}

function bindModelGallery() {
  document.addEventListener('click', (e) => {
    const facet = e.target.closest('[data-facet]');
    if (facet) {
      const tag = facet.dataset.facet || '';
      if (modelState.facets.has(tag)) modelState.facets.delete(tag);
      else modelState.facets.add(tag);
      renderModelGallery();
      return;
    }
    const tile = e.target.closest('[data-model-open]');
    if (tile) openModelCompare(tile.dataset.modelOpen);
  });
}
bindModelGallery();


/* Контейнеры галереи моделей в Библиотеке + вход в раздел. */
function ensureModelGalleryHost() {
  const home = document.getElementById('library_home');
  if (!home || document.getElementById('model_gallery_card')) return;
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'model_gallery_card';
  card.style.marginTop = '16px';
  card.innerHTML = '<div class="card-head"><div><h2>Галерея моделей (В79)</h2>'
    + '<p>Мозаика реестра 3D-моделей: клик по плитке — сравнение версий шторкой</p></div></div>'
    + '<div class="tag-cloud" id="model_facets" title="В80: фасеты реестра — чем чаще встречается метка, тем она крупнее"></div>'
    + '<div class="model-masonry" id="model_gallery"></div>';
  home.insertBefore(card, home.firstChild);
}

PF.on('view', (d) => {
  if (d.view !== 'library') return;
  ensureModelGalleryHost();
  loadModelGallery();
  decorateGeneratorCards();
});

/* В84: живой справочник стиля в Настройках — токены из :root, клик копирует значение. */
function ensureStyleGuideHost() {
  const view = document.getElementById('view-settings');
  if (!view || document.getElementById('styleguide_card')) return view.querySelector('#styleguide_card');
  const card = document.createElement('div');
  card.className = 'card styleguide';
  card.id = 'styleguide_card';
  card.style.marginTop = '16px';
  card.innerHTML = '<div class="card-head"><div><h2>Стиль панели (В84)</h2>'
    + '<p>Живые токены дизайна: клик по образцу копирует значение</p></div></div>'
    + '<div class="sg-colors" id="sg_colors"></div>'
    + '<div class="sg-row" id="sg_typo"></div>'
    + '<div class="sg-row" id="sg_surfaces"></div>'
    + '<div class="sg-row" id="sg_buttons"></div>';
  view.appendChild(card);
  return card;
}

function copyText(text) {
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(
    () => toast('Скопировано', text, 'info'),
    () => {});
}

function renderStyleGuide() {
  const card = ensureStyleGuideHost();
  if (!card) return;
  const cs = getComputedStyle(document.documentElement);
  const colors = [
    ['--accent', 'Акцент'], ['--accent-2', 'Акцент 2'], ['--ok', 'Успех'],
    ['--warn', 'Внимание'], ['--bad', 'Ошибка'], ['--info', 'Инфо'],
    ['--bg', 'Фон'], ['--panel', 'Панель'], ['--panel-2', 'Панель 2'],
    ['--line', 'Линия'], ['--text', 'Текст'], ['--muted', 'Приглушённый'],
  ];
  const colorsHost = card.querySelector('#sg_colors');
  colorsHost.innerHTML = colors.map(([token, label]) => {
    const value = cs.getPropertyValue(token).trim() || '#000';
    return `<div class="sg-color" data-copy="${esc(value)}" title="Скопировать ${esc(value)}">`
      + `<i style="background:${esc(value)}"></i><span>${esc(token)} · ${esc(value)}</span></div>`;
  }).join('');
  card.querySelector('#sg_typo').innerHTML = '<div class="sg-typo">'
    + `<span style="font:800 24px/1 var(--font)">Aa ${esc((cs.getPropertyValue('--font').split(',')[0] || '').replace(/"/g, ''))}</span>`
    + `<span style="font:600 15px/1.3 var(--font)">Заголовок карточки 15.5px</span>`
    + `<span style="font:400 12.5px/1.4 var(--font);color:var(--muted)">Основной текст 12.5–13.3px, подписи — приглушённым</span>`
    + `<span style="font:700 12px/1 var(--mono)">0123456789 · JetBrains Mono для цифр</span></div>`;
  card.querySelector('#sg_surfaces').innerHTML =
    '<div class="sg-surface">лист · elev-1</div>'
    + '<div class="sg-surface s2">парящий · elev-2</div>'
    + '<div class="sg-surface s3">модалка · elev-3</div>';
  card.querySelector('#sg_buttons').innerHTML =
    '<button class="btn primary" type="button" data-nocopy>Основная</button>'
    + '<button class="btn" type="button" data-nocopy>Обычная</button>'
    + '<button class="btn ghost" type="button" data-nocopy>Тихая</button>'
    + '<span class="chip ok">чип успех</span>'
    + '<span class="chip warn">чип внимание</span>'
    + '<span class="chip bad">чип ошибка</span>';
}

PF.on('view', (d) => {
  if (d.view !== 'settings') return;
  renderStyleGuide();
});
document.addEventListener('click', (e) => {
  const sw = e.target.closest('[data-copy]');
  if (!sw || e.target.closest('[data-nocopy]')) return;
  if (navigator.clipboard) navigator.clipboard.writeText(sw.dataset.copy || '')
    .then(() => toast('Скопировано', sw.dataset.copy, 'info'), () => {});
});

// settingGroup/settingRow открыты для других разделов (18.8): «Конвейер»
// рисует свою карточку допусков тем же рендерером, что и «Настройки».
PF.modules.settings = { downloadBackup, renderSettings, saveSettings, resetSettings, filterSettings,
  renderDiag, refreshDiag, settingGroup, settingRow };
})();
