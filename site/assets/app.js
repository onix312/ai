/* PrintFlow 2.0 — дашборд, настройки и библиотека. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, clamp, money, nfmt, pct, hoursText, minutesText,
  dateText, dateTimeText, agoText, toast, fail, confirmDanger, drawChart, legend, store,
  openModal, closeModal } = U;
const { get, post } = PF.api;

let dashMode = 'money';

/* ============================================================ дашборд */
function kpi(label, value, sub, kind, extra) {
  return `<div class="kpi ${kind || ''}"><span class="label">${esc(label)}</span>`
    + `<b class="value">${value}</b><span class="sub">${sub || ''}</span>${extra || ''}</div>`;
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

  $('dash_kpis').innerHTML = [
    kpi('Печатает сейчас', `${nfmt(farm.printing)} / ${nfmt(farm.total)}`,
      `${nfmt(farm.online)} на связи · загрузка ${nfmt(farm.utilization)}%`,
      num(farm.printing) ? 'ok' : ''),
    kpi('Очередь печати', hoursText(needHours), `${nfmt(queuedJobs.length)} заданий · ${pct(load)} от ${nfmt(capacity)} ч в неделю`,
      load > 100 ? 'bad' : load > 85 ? 'warn' : '',
      `<div class="bar ${load > 100 ? 'bad' : load > 85 ? 'warn' : ''}"><i style="width:${clamp(load, 0, 100)}%"></i></div>`),
    kpi('Активные заказы', String(activeOrders.length), `${nfmt(farm.queued)} заданий в производстве`),
    kpi('Требуют внимания', String(late.length), 'срок сегодня или прошёл', late.length ? 'bad' : 'ok'),
    kpi('Ждём оплату', money(pipeline), 'по активным заказам'),
    kpi('Прибыль за период', money(s.profit), `маржа ${pct(s.margin)}`, num(s.profit) >= 0 ? 'ok' : 'bad'),
    kpi('Сегодня напечатано', `${nfmt(farm.today_hours, 1)} ч`, `${nfmt(farm.today_grams)} г · ${nfmt(farm.today_jobs)} задан.`),
    kpi('Простой парка', `${nfmt((farm.idle && farm.idle.idle_hours) || 0, 1)} ч`,
      `упущено ≈ ${money((farm.idle && farm.idle.lost_profit) || 0)}`,
      num((farm.idle && farm.idle.lost_profit) || 0) > 0 ? 'warn' : 'ok'),
    kpi('Нужно пластика', `${nfmt(needGrams)} г`, `на складе ${nfmt(stock)} г`,
      needGrams > stock ? 'warn' : 'ok'),
  ].join('');

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
  $('dash_printers').innerHTML = printers.length ? printers.map((p) => {
    const st = p.printer.state;
    const running = st === 'RUNNING' || st === 'PREPARE';
    const progress = clamp(num(p.printer.progress), 0, 100);
    return `<div style="padding:11px 0;border-bottom:1px solid var(--line-soft)">`
      + `<div style="display:flex;align-items:center;gap:9px">`
      + `<span class="dot ${p.connection.connected ? (running ? 'busy' : 'ok') : 'bad'}"></span>`
      + `<b style="flex:1;font-size:13.4px">${esc(p.name)}</b>`
      + `<span class="chip ${running ? 'accent' : p.connection.connected ? 'ok' : 'outline'}">${esc(p.printer.state_label)}</span></div>`
      + (running ? `<div class="bar thin" style="margin-top:8px"><i style="width:${progress}%"></i></div>`
        + `<small class="muted">${esc(p.printer.task || '')} · ${Math.round(progress)}% · осталось ${minutesText(p.printer.remaining_min)}</small>`
        : `<small class="muted">${esc(p.connection.connected ? 'Готов к печати' : (p.connection.last_error || 'Нет связи'))}</small>`)
      + '</div>';
  }).join('') : '<div class="empty compact"><span>Принтер ещё не добавлен.</span></div>';

  const due = activeOrders
    .filter((o) => o.due)
    .sort((a, b) => String(a.due).localeCompare(String(b.due)))
    .slice(0, 8);
  $('dash_due').innerHTML = due.length ? due.map((o) => {
    const st = PF.status(o.status);
    const isLate = o.due < today, isToday = o.due === today;
    return `<div class="tx-row clickable" data-order="${esc(o.id)}" style="cursor:pointer">`
      + `<span class="tx-ic ${isLate ? 'expense' : 'income'}">${isLate ? '!' : '◷'}</span>`
      + `<div class="tx-body"><b>№${esc(o.number)} · ${esc(o.product)}</b>`
      + `<small>${esc(o.customer_name || 'без клиента')} · ${esc(st.name)}</small></div>`
      + `<span class="amt ${isLate ? 'neg' : ''}">${isLate ? 'просрочен' : isToday ? 'сегодня' : esc(dateText(o.due))}</span></div>`;
  }).join('') : '<div class="empty compact"><span>Заказов со сроками нет.</span></div>';

  renderOperatorFocus();
  renderPlan();
  renderHealth();
  renderActivePrint();
  renderGauge();
  renderAmsPanel();
  renderFilamentForecast();
  renderTimeline();
  applyWidgets();
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

function renderEvents() {
  const list = PF.state.events || [];
  $('dash_events').innerHTML = list.length ? list.slice(0, 18).map((e) => `<div class="event ${esc(e.kind)}">`
    + '<span class="edot"></span><span class="etext">'
    + `<b>${esc(e.title)}</b><small>${esc(e.detail || '')}</small></span>`
    + `<time title="${esc(dateTimeText(e.at))}">${esc(agoText(e.at))}</time></div>`).join('')
    : '<div class="empty compact"><span>Событий пока нет.</span></div>';
}

/* ================================================= виджеты панели */
const DASH_WIDGETS = [
  ['kpis', 'Показатели (KPI-ряд)'],
  ['operator_focus', 'Сейчас нужно сделать'],
  ['plan', 'План на сегодня'],
  ['health', 'Здоровье бизнеса'],
  ['active', 'Активная печать'],
  ['gauge', 'Прибыль за час печати'],
  ['filament', 'Пластик на очередь'],
  ['ams', 'AMS на главной'],
  ['chart', 'Деньги и печать по дням'],
  ['printers', 'Парк принтеров'],
  ['due', 'Ближайшие сроки'],
  ['events', 'Лента событий'],
  ['timeline', 'Таймлайн печати за день'],
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
function renderWidgetsList() {
  const prefs = widgetPrefs();
  $('dash_widgets_list').innerHTML = DASH_WIDGETS.map(([id, label]) =>
    `<label class="widget-check${prefs.includes(id) ? ' on' : ''}"><input type="checkbox" data-widget-check="${id}"${prefs.includes(id) ? ' checked' : ''}><span>${esc(label)}</span></label>`).join('');
}
function dashEmpty(msg) { return `<div class="empty compact"><span>${esc(msg)}</span></div>`; }

/* ================================================ большая карточка печати */
function renderActivePrint() {
  const host = $('dash_active');
  const live = PF.state.live;
  const snap = (live && (live.active || (live.printers || [])[0])) || null;
  if (!snap) { host.innerHTML = dashEmpty('Принтер не добавлен — подключите его в разделе «Принтеры».'); return; }
  const info = snap.printer || {};
  const running = ['RUNNING', 'PAUSE', 'PREPARE'].includes(info.state);
  if (!running) {
    $('dash_active_sub').textContent = `${snap.name} · ${info.state_label || 'готов'}`;
    host.innerHTML = `<div class="active-print idle">`
      + `<div class="ap-main"><b class="ap-pct">—</b><div class="ap-info">`
      + `<b>${esc(snap.name)} свободен</b><small>${esc(info.state_label || 'Готов к печати')}</small></div></div>`
      + ((info.problems && info.problems.length) ? `<div class="ap-warn">⚠ ${esc(info.problems[0].title)}</div>` : '')
      + '</div>';
    return;
  }
  const job = snap.job || {};
  const order = job.order || {};
  const progress = clamp(num(info.progress), 0, 100);
  const remaining = num(info.remaining_min);
  const elapsed = num(info.elapsed_min);
  const eta = info.eta ? String(info.eta).slice(11, 16) : '';
  const facts = [];
  if (info.layer) facts.push(`Слой ${info.layer} / ${info.total_layers || '—'}`);
  if (remaining) facts.push(`Осталось ${minutesText(remaining)}`);
  if (eta) facts.push(`Финиш в ${eta}`);
  if (elapsed) facts.push(`Идёт ${minutesText(elapsed)}`);
  $('dash_active_sub').textContent = `${snap.name} · ${info.state_label}`;
  host.innerHTML = `<div class="active-print${info.state === 'PAUSE' ? ' paused' : ''}">`
    + `<div class="ap-main"><b class="ap-pct">${Math.round(progress)}<small>%</small></b>`
    + `<div class="ap-info"><b>${esc(info.task || 'Печать')}</b>`
    + (order.number
      ? `<small><a href="#orders" class="order-link" data-order-open="${esc(order.id || '')}">Заказ №${esc(order.number)} · ${esc(order.product || '')}${order.customer_name ? ' · ' + esc(order.customer_name) : ''}</a></small>`
      : `<small>${esc(snap.name)} · <button class="btn xs primary" type="button" data-convert-order="${esc(snap.id)}" style="padding:1px 8px;font-size:11px"><span class="ic">✨</span>В заказ</button></small>`)
    + `<div class="bar" style="margin-top:8px"><i style="width:${progress}%"></i></div></div></div>`
    + `<div class="ap-facts">${facts.map((f) => `<span>${esc(f)}</span>`).join('')}</div>`
    + (num(job.spent)
      ? `<div class="ap-money"><span>Потрачено <b>${money(job.spent)}</b></span>`
        + (num(job.cost_total) ? `<span>Итого печать ≈ <b>${money(job.cost_total)}</b></span>` : '')
        + (job.profit != null && num(job.price) ? `<span>Прибыль <b class="${num(job.profit) >= 0 ? 'pos' : 'neg'}">${money(job.profit)}</b></span>` : '')
        + (job.break_even_pct != null ? `<span>Затраты съели <b>${nfmt(job.break_even_pct)}%</b> цены</span>` : '')
        + (num(job.per_hour) ? `<span>Стоимость часа <b>${money(job.per_hour)}</b></span>` : '')
        + '</div>'
      : '')
    + (info.state === 'PAUSE' ? '<div class="ap-warn">⚠ На паузе — проверьте принтер</div>' : '')
    + '</div>';
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
  host.innerHTML = trays.map((t) => {
    const remain = t.remain == null || t.remain < 0 ? null : num(t.remain);
    const warn = remain != null && remain < threshold;
    const cname = amsHexToName(t.color) || '';
    return `<div class="ams-row${t.active ? ' active' : ''}">`
      + `<span class="swatch" style="--filament:${esc(t.color || '#cbd5e1')}"></span>`
      + `<div class="ams-info"><b>${esc(t.label || 'Слот')}${cname ? ' · ' + esc(cname) : ''}</b>`
      + `<small>${esc(t.type || 'Не задан')}${cname ? ' · ' + esc(cname) : ''}${t.active ? ' · активен' : ''}</small></div>`
      + (remain != null
        ? `<div class="ams-remain${warn ? ' warn' : ''}"><div class="bar thin"><i style="width:${clamp(remain, 0, 100)}%"></i></div><small>${Math.round(remain)}%</small></div>`
        : '<span class="muted">—</span>')
      + '</div>';
  }).join('');
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
  ['power_kw', 'Мощность принтера, кВт', 'Средняя потребляемая мощность', 0.01],
  ['energy_price', 'Цена электричества, ₽/кВт·ч', 'По вашему тарифу', 0.1],
  ['amortization_per_hour', 'Амортизация, ₽/ч', 'Износ принтера за час печати', 1],
  ['maintenance_per_hour', 'Обслуживание, ₽/ч', 'Сопла, ремни, смазка', 1],
  ['default_spool_price', 'Цена катушки, ₽', 'По умолчанию для расчётов', 10],
  ['default_spool_weight', 'Вес катушки, г', 'Обычно 1000', 50],
  ['failure_rate', 'Резерв на брак, %', 'Закладывается в себестоимость', 0.5],
  ['filament_low_threshold', 'Порог остатка, %', 'Когда предупреждать о пластике', 1],
];
/* Ставка своей работы, упаковка, норма прибыли и ёмкость живут на вкладках
   «Цены» и «Бизнес» — второй раз их здесь не рисуем, иначе при сохранении
   побеждает то поле, которое в разметке ниже, и правка молча теряется. */
/* Группы настроек по вкладкам: [ключ, подпись, пояснение, тип, шаг|опции] */
const GOALS = [
  ['goal_profit_month', 'Цель по прибыли в месяц, ₽', 'От неё считается план продаж', 'num', 1000],
  ['target_profit_per_hour', 'Норма прибыли за час, ₽', 'Порог, ниже которого заказ невыгоден', 'num', 10],
  ['weekly_capacity_hours', 'Сколько часов печати в неделю', 'Реальный потолок вашего парка', 'num', 5],
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
  ['default_markup', 'Наценка к себестоимости, %', 'База для подсказки цены', 'num', 5],
  ['min_order_price', 'Минимальный чек, ₽', 'Ниже этой суммы браться невыгодно', 'num', 50],
  ['price_rounding', 'Округление цены, ₽', 'Цена округляется вверх до кратной суммы', 'num', 5],
  ['design_rate', 'Моделирование, ₽/ч', 'Оплата за подготовку модели', 'num', 50],
  ['labor_rate', 'Ваш час работы, ₽', 'Ориентир для оценки своей работы', 'num', 50],
];
const DISCOUNTS = [
  ['bulk_discount_10', 'Скидка от 10 шт, %', 'Автоматически в расчёте цены', 'num', 1],
  ['bulk_discount_50', 'Скидка от 50 шт, %', 'Для крупных партий', 'num', 1],
  ['rush_surcharge', 'Надбавка за срочность, %', 'Когда нужно «на вчера»', 'num', 5],
];
const PAYMENTS = [
  ['acquiring_fee', 'Эквайринг, %', 'Комиссия за приём карт', 'num', 0.1],
  ['delivery_cost', 'Доставка на заказ, ₽', 'Средние затраты, если платите вы', 'num', 10],
  ['packaging_cost', 'Упаковка на заказ, ₽', 'Пакет, коробка, бирка', 'num', 5],
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
  ['notify_complete', 'Завершение печати'],
  ['notify_error', 'Ошибки и HMS'],
  ['notify_pause', 'Пауза'],
  ['notify_filament_low', 'Пластик заканчивается'],
  ['notify_guard', 'Тревоги сторожа печати'],
  ['notify_maintenance', 'Пора обслужить принтер'],
  ['notify_photo', 'Прикладывать кадр с камеры'],
];
const AUTO_EXTRA = [
  ['printer_info_sync', 'Собирать данные принтера в базу', 'Прошивка, Wi-Fi, влажность AMS — в карточку принтера', 'bool'],
  ['ams_auto_spools', 'Заводить катушки из AMS автоматически', 'Вставили катушку — она появилась на складе', 'bool'],
  ['ams_sync_remaining', 'Обновлять остаток по датчику AMS', 'Только у катушек с галочкой «Обновлять из AMS»', 'bool'],
  ['restock_remind', 'Напоминать о закупке пластика', 'Раз в день: катушки ниже порога', 'bool'],
  ['queue_check_material', 'Проверять материал в AMS', 'Не запускать PETG, если в слоте PLA', 'bool'],
  ['dry_humidity_threshold', 'Влажность AMS для сушки, %', 'Выше порога — событие и Telegram', 'num', 1],
  ['notify_finish_remind_min', 'Напомнить о финише за, мин', '0 — выключено', 'num', 1],
  ['digest_time', 'Утренний дайджест, время', 'Например 09:00', 'text'],
  ['weekly_report_day', 'День недельного отчёта', '1 = понедельник … 7 = воскресенье', 'num', 1],
  ['weekly_report_time', 'Время недельного отчёта', 'Например 20:00', 'text'],
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
  ['queue_check_filament', 'Проверять остаток пластика', 'Не запускать печать, если катушки не хватит', 'bool'],
  ['queue_group_material', 'Группировать по материалу', 'Меньше перезаправок AMS подряд', 'bool'],
  ['quiet_hours_enabled', 'Тихие часы', 'Ночью автозапуск откладывается до утра', 'bool'],
  ['quiet_from', 'Тишина с', 'Например 23:00', 'text'],
  ['quiet_to', 'Тишина до', 'Например 08:00', 'text'],
];
const UPKEEP = [
  ['maintenance_enabled', 'Регламент обслуживания', 'Напоминать о ТО по наработке часов', 'bool'],
  ['telemetry_enabled', 'История показателей', 'Графики температур и обдува', 'bool'],
  ['telemetry_keep_days', 'Хранить историю, дней', 'Старые точки удаляются автоматически', 'num', 1],
  ['night_shift_enabled', 'Ночная смена', 'Планировать длинное на ночь, срочное — днём', 'bool'],
  ['auto_backup_days', 'Автобэкап, раз в N дней', '0 — выключен. Общий лимит задаётся в разделе «Система»', 'num', 1],
];
const WATCH = [
  ['watch_folder_enabled', 'Watch Folder — авто-импорт', 'Следить за папкой с 3MF из Bambu Studio', 'bool'],
  ['watch_folder_path', 'Путь к Watch Folder', 'Например ~/PrintFlow-Inbox или C:\\PrintFlow-Inbox', 'text'],
  ['watch_auto_action', 'Действие Watch Folder', 'notify — уведомление, queue — очередь без запуска. Значение print устарело и принудительно сводится к notify.', 'text'],
  ['watch_link_order', 'Связывать с заказом по №', 'Искать № заказа в имени файла', 'bool'],
  ['watch_create_order', 'Создавать черновик заказа', 'Если не нашли заказ — сделать новый', 'bool'],
];
const PREFLIGHT = [
  ['preflight_enabled', 'Preflight — проверка перед стартом', 'Блокировать старт при проблемах', 'bool'],
  ['preflight_block_material', 'Блок: не тот материал в AMS', 'PLA вместо PETG — брак', 'bool'],
  ['preflight_block_filament', 'Блок: мало пластика', 'Сверять граммы с остатком катушки', 'bool'],
  ['preflight_block_hms', 'Блок: HMS ошибки', 'Не давать старт при ошибке принтера', 'bool'],
  ['preflight_warn_nozzle', 'Предупр.: сопло', 'Диаметр сопла в файле vs принтер', 'bool'],
  ['preflight_warn_humidity', 'Предупр.: влажность AMS', 'Выше порога — сушить', 'bool'],
];
const FTPS = [
  ['ftps_timeout', 'Таймаут FTPS, сек', 'Для операций с SD-картой принтера', 'num', 1],
  ['ftps_retries', 'Повторы загрузки FTPS', 'Сколько раз повторить временно оборванную загрузку', 'num', 1],
  ['ftps_block_kb', 'Блок загрузки, КБ', 'Размер порции при отправке файла', 'num', 16],
];
const MQTT = [
  ['mqtt_keepalive', 'Keepalive MQTT, сек', 'Интервал heartbeat', 'num', 5],
  ['mqtt_backoff', 'Backoff переподключений', 'Увеличивать паузу после повторных сбоев', 'bool'],
];
const AMS_SETTINGS = [
  ['dry_humidity_threshold', 'Порог влажности AMS, %', 'Выше порога система рекомендует сушку', 'num', 1],
];
const SYSTEM2 = [
  ['public_url', 'Адрес для QR', 'Пусто — LAN IP компьютера. Пример: http://192.168.1.50:8080', 'text'],
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

/** Рисует группу настроек по описанию [ключ, подпись, пояснение, тип, шаг]. */
function settingGroup(list) {
  const s = PF.state.settings;
  return list.map(([k, label, sub, type, step]) => {
    let control;
    if (type === 'bool') {
      control = `<label class="switch"><input type="checkbox" data-setting="${k}"${s[k] ? ' checked' : ''}><i></i></label>`;
    } else if (type === 'text') {
      control = `<input type="text" data-setting="${k}" value="${esc(String(s[k] ?? ''))}">`;
    } else {
      control = `<input type="number" step="${step || 1}" min="0" data-setting="${k}" value="${esc(String(num(s[k])))}">`;
    }
    return settingRow(k, label, sub, control);
  }).join('');
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
        const code = window.prompt('Код из приложения-аутентификатора:', '');
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
  $('mat_base').innerHTML = '<option value="">— без шаблона —</option>'
    + catalog.map((b) => `<option value="${esc(b.key)}">${esc(b.name)}</option>`).join('');
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
  const payload = {
    id: editingMaterial || '',
    name: $('mat_name').value.trim(),
    key: $('mat_key').value.trim(),
    base: $('mat_base').value,
    full_name: $('mat_full_name').value.trim(),
    price_per_kg: num($('mat_price_per_kg').value),
    speed_factor: num($('mat_speed_factor').value),
    temp_nozzle_min: num($('mat_nozzle_min').value),
    temp_nozzle_max: num($('mat_nozzle_max').value),
    temp_bed_min: num($('mat_bed_min').value),
    temp_bed_max: num($('mat_bed_max').value),
    fan: num($('mat_fan').value),
    chamber: $('mat_chamber').value,
    density: num($('mat_density').value),
    shrinkage: num($('mat_shrinkage').value),
    dry_temp: num($('mat_dry_temp').value),
    dry_hours: num($('mat_dry_hours').value),
    heat_resistance: num($('mat_heat_resistance').value),
    support_factor: num($('mat_support_factor').value),
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
  $('set_rates').innerHTML = RATES.map(([k, label, sub, step]) => settingRow(k, label, sub,
    `<input type="number" step="${step}" min="0" data-setting="${k}" value="${esc(String(num(s[k])))}">`)).join('');

  // --- бизнес
  $('set_company').innerHTML = settingGroup(COMPANY);
  $('set_goals').innerHTML = settingGroup(GOALS);

  // --- налоги
  const mode = s.tax_mode || 'none';
  $('set_tax_mode').value = mode;
  const hints = (PF.modules.finance && PF.modules.finance.MODE_HINTS) || {};
  $('set_tax_hint').lastElementChild.textContent = hints[mode] || '';
  $('set_tax').innerHTML = settingGroup(TAX_FIELDS[mode] || [])
    || '<div class="empty compact"><span>Для этого режима настраивать нечего.</span></div>';
  $('set_insurance').innerHTML = ['usn6', 'usn15', 'patent'].includes(mode)
    ? settingGroup(INSURANCE)
    : '<div class="notice"><span>ℹ</span><span>На выбранном режиме страховые взносы «за себя» не платятся.</span></div>';
  $('set_vat').innerHTML = settingGroup(VAT);

  // --- цены
  $('set_pricing').innerHTML = settingGroup(PRICING);
  $('set_discounts').innerHTML = settingGroup(DISCOUNTS);
  $('set_payments').innerHTML = settingGroup(PAYMENTS);

  // --- учёт денег
  const accounts = (PF.state.accounts || []).filter((a) => !num(a.archived));
  const BANK_RULES_SAMPLE = [
    { match: 'ozon', kind: 'income', category: 'sale', title: 'Продажа (Ozon)' },
    { match: 'пластик|филамент|petg|pla|abs|tpu', kind: 'expense', category: 'filament', title: 'Закупка пластика' },
    { match: 'электроэнергия|энергосбыт', kind: 'expense', category: 'energy', title: 'Электричество' },
    { match: 'налог', kind: 'expense', category: 'tax', title: 'Налог' },
  ];
  const bankRules = Array.isArray(s.bank_rules) && s.bank_rules.length ? s.bank_rules : BANK_RULES_SAMPLE;
  $('set_money_rules').innerHTML = settingGroup(MONEY_RULES)
    + settingRow('bank_rules', 'Правила импорта выписки', 'JSON: match — регулярное выражение по назначению платежа, kind: income|expense, category — статья, title — название проводки.',
      `<textarea data-setting="bank_rules" rows="7" style="width:100%;font-family:ui-monospace,monospace;font-size:12px">${esc(JSON.stringify(bankRules, null, 1))}</textarea>`)
    + settingRow('default_account', 'Касса по умолчанию', 'Куда попадают деньги без уточнения',
      `<select data-setting="default_account">${accounts.map((a) =>
        `<option value="${esc(a.id)}"${(s.default_account || 'cash') === a.id ? ' selected' : ''}>${esc(a.name)}</option>`)
        .join('') || '<option value="cash">Наличные</option>'}</select>`);
  $('set_auto').innerHTML = AUTOS.map(([k, label, sub]) => settingRow(k, label, sub,
    `<label class="switch"><input type="checkbox" data-setting="${k}"${s[k] ? ' checked' : ''}><i></i></label>`)).join('');
  $('set_auto_extra').innerHTML = settingGroup(AUTO_EXTRA);
  $('set_guard').innerHTML = settingGroup(GUARD);
  $('set_queue_rules').innerHTML = settingGroup(QUEUE_RULES);
  $('set_upkeep').innerHTML = settingGroup(UPKEEP);
  if ($('set_watch')) $('set_watch').innerHTML = settingGroup(WATCH);
  if ($('set_preflight')) $('set_preflight').innerHTML = settingGroup(PREFLIGHT);
  if ($('set_ftps')) $('set_ftps').innerHTML = settingGroup(FTPS);
  if ($('set_mqtt')) $('set_mqtt').innerHTML = settingGroup(MQTT);
  if ($('set_ams')) $('set_ams').innerHTML = settingGroup(AMS_SETTINGS);
  if ($('set_system2')) $('set_system2').innerHTML = settingGroup(SYSTEM2);
  // профили настроек
  if ($('set_profiles')) renderProfiles();
  // правила «если-то»
  if ($('set_rules')) renderRules();

  $('set_tg').innerHTML = settingRow('telegram_enabled', 'Включить Telegram', 'Уведомления о печати',
    `<label class="switch"><input type="checkbox" data-setting="telegram_enabled"${s.telegram_enabled ? ' checked' : ''}><i></i></label>`)
    + settingRow('telegram_token', 'Bot Token', s.has_telegram_token ? 'Сохранён — оставьте пустым, чтобы не менять' : 'Получите у @BotFather',
      '<input type="password" autocomplete="new-password" data-setting="telegram_token" placeholder="' + (s.has_telegram_token ? '••••••••' : 'токен') + '">')
    + settingRow('telegram_chat_id', 'Chat ID', 'Ваш идентификатор в Telegram',
      `<input type="text" data-setting="telegram_chat_id" value="${esc(String(s.telegram_chat_id || ''))}">`)
    + settingRow('telegram_bot', 'Отвечать на команды', 'Бот принимает «статус», «кадр», «пауза» с телефона',
      `<label class="switch"><input type="checkbox" data-setting="telegram_bot"${s.telegram_bot ? ' checked' : ''}><i></i></label>`)
    + NOTIFY.map(([k, label]) => settingRow(k, label, '',
      `<label class="switch"><input type="checkbox" data-setting="${k}"${s[k] ? ' checked' : ''}><i></i></label>`)).join('')
    + settingRow('browser_notify_enabled', 'Уведомления в браузере', 'Пока PrintFlow открыт вкладкой — события приходят сразу',
      `<label class="switch"><input type="checkbox" data-setting="browser_notify_enabled"${s.browser_notify_enabled ? ' checked' : ''}><i></i></label>
       <button class="btn sm" type="button" id="notify_perm_btn" style="margin-top:8px">Разрешить уведомления</button>`);

  if ($('set_cloud')) renderCloudSettings(s);
  $('set_theme').value = s.theme || 'system';
  $('set_accent').innerHTML = ACCENTS.map(([name, color]) =>
    `<button type="button" data-accent="${name}" class="${(s.accent || 'indigo') === name ? 'on' : ''}" style="background:${color}" title="${name}"></button>`).join('');

  $('set_printers').innerHTML = PF.state.printers.length ? PF.state.printers.map((p) => {
    const livep = PF.livePrinter(p.id);
    return `<div class="set-row"><div class="sinfo"><b>${esc(p.name)}</b>`
      + `<small>${esc(p.model || '')} · ${esc(p.host || 'IP не задан')} · ${p.has_access_code ? 'код сохранён' : 'нет Access Code'}`
      + `${livep ? ' · ' + esc(livep.printer.state_label) : ''}</small></div>`
      + `<button class="btn sm" type="button" data-printer-edit="${esc(p.id)}">Изменить</button></div>`;
  }).join('') : '<div class="empty compact"><span>Принтеры не добавлены.</span></div>';

  $('set_data_dir').textContent = navigator.platform.toLowerCase().includes('win')
    ? '%APPDATA%\\PrintFlow' : '~/.config/printflow';
  renderUpdateInfo();
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
let settingsPane = 'business';

function filterSettings(query) {
  const q = String(query || '').trim().toLowerCase();
  const panes = $$('[id^="setpane-"]');
  const tabs = $$('#set_tabs button');
  if (!q) {
    // Возврат к обычному режиму вкладок.
    panes.forEach((p) => {
      p.classList.toggle('on', p.id === `setpane-${settingsPane}`);
      $$('[data-set-card]', p).forEach((c) => c.classList.remove('hidden'));
      $$('[data-set-row]', p).forEach((r) => r.classList.remove('hidden'));
    });
    tabs.forEach((b) => {
      b.classList.toggle('on', b.dataset.pane === settingsPane);
      b.classList.remove('dim');
      const badge = b.querySelector('.tab-hits');
      if (badge) badge.remove();
    });
    $('set_no_results').hidden = true;
    return;
  }
  let found = 0;
  const hits = {};
  panes.forEach((pane) => {
    let paneHits = 0;
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
      if (visible) paneHits += cardHits || 1;
    });
    pane.classList.toggle('on', paneHits > 0);
    hits[pane.id.replace('setpane-', '')] = paneHits;
    found += paneHits;
  });
  // Вкладки в режиме поиска показывают, где именно нашлось.
  tabs.forEach((b) => {
    const n = hits[b.dataset.pane] || 0;
    b.classList.toggle('on', n > 0);
    b.classList.toggle('dim', n === 0);
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
    PF.state.settings = res.settings;
    PF.applyTheme();
    renderSettings();
    toast('Настройки сброшены', 'Вернулись значения по умолчанию');
    PF.refreshFinance();
    PF.refreshCore();
  } catch (e) { fail(e); }
}

async function saveSettings() {
  const payload = {};
  $$('[data-setting]').forEach((el) => {
    const k = el.dataset.setting;
    if (el.type === 'checkbox') payload[k] = el.checked;
    else if (el.type === 'number') payload[k] = num(el.value);
    else payload[k] = el.value;
  });
  payload.theme = $('set_theme').value;
  payload.accent = PF.state.settings.accent || 'indigo';
  try {
    const res = await post('/api/settings', payload);
    PF.state.settings = res.settings;
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
  if (add) add.addEventListener('click', () => {
    replyTemplates.push({ id: 't' + Date.now().toString(36), title: 'Новый шаблон', text: '' });
    renderTemplates();
    // простейшее редактирование: prompt'ом
    const row = host.querySelector('[data-tpl-row="' + (replyTemplates.length - 1) + '"]');
    if (row) {
      const title = window.prompt('Название шаблона', 'Новый шаблон');
      if (title == null) { replyTemplates.pop(); renderTemplates(); return; }
      const text = window.prompt('Текст шаблона', 'Здравствуйте! Ваш заказ готов, можно забрать.');
      if (text == null) { replyTemplates.pop(); renderTemplates(); return; }
      replyTemplates[replyTemplates.length - 1].title = title;
      replyTemplates[replyTemplates.length - 1].text = text;
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
    : '<div class="empty compact"><span>Конвертов нет. Добавьте «Налог 6%» или «Второй принтер».</span></div>';
}
async function envSave(id) {
  const cur = (PF.state.envelopes || []).find((e) => e.id === id) || {};
  const name = window.prompt('Название конверта', cur.name || '');
  if (name == null) return;
  const pct = window.prompt('Процент с дохода (0–100)', String(cur.pct ?? 0));
  if (pct == null) return;
  const goal = window.prompt('Цель накопления, ₽ (0 — без цели)', String(cur.goal || 0));
  if (goal == null) return;
  try {
    await post('/api/envelope/save', { id: id || '', name, pct: num(pct), goal: num(goal) });
    toast('Конверт сохранён', name);
    loadEnvelopes();
  } catch (e) { fail(e); }
}
async function envWithdraw(id) {
  const cur = (PF.state.envelopes || []).find((e) => e.id === id);
  if (!cur) return;
  const amount = window.prompt(`Сколько забрать из «${cur.name}» (остаток ${money(cur.balance)})?`, '');
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
    host.querySelectorAll('[data-prof-restore]').forEach(b=>b.addEventListener('click', async()=>{ if(!confirmDanger('Восстановить снапшот «'+b.dataset.profRestore+'»? Текущие настройки будут перезаписаны.')) return; try{ await post('/api/settings/profile/restore',{id:b.dataset.profRestore}); PF.state.settings=(await get('/api/settings')).settings; renderSettings(); toast('Настройки восстановлены'); }catch(e){fail(e);} }));
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
    $('rl_event').innerHTML=Object.entries(d.triggers||{}).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');
    $('rl_action').innerHTML=Object.entries(d.actions||{}).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');
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

function showArticle(name) {
  const articles = $$('#library-body .library-article');
  let shown = false;
  articles.forEach((a) => {
    const on = a.dataset.article === name;
    a.classList.toggle('on', on);
    if (on) shown = true;
  });
  $('lib_grid').hidden = shown;
  $('lib_back').hidden = !shown;
  const tpl = $('lib_tpl_wrap');
  if (tpl) tpl.hidden = name !== 'tpl';
  if (name === 'tpl') loadTemplates();
}

/* ============================================================= события */
function bind() {
  $('dash_period').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-days]');
    if (!btn) return;
    $$('#dash_period button').forEach((b) => b.classList.toggle('on', b === btn));
    PF.state.dashDays = +btn.dataset.days;
    if (PF.state.dashDays > PF.state.financeDays) await PF.refreshFinance(PF.state.dashDays);
    renderDashboard();
  });
  $('dash_chart_mode').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-mode]');
    if (!btn) return;
    $$('#dash_chart_mode button').forEach((b) => b.classList.toggle('on', b === btn));
    dashMode = btn.dataset.mode;
    renderDashboard();
  });
  $('dash_refresh').addEventListener('click', async () => {
    try {
      await Promise.all([PF.refreshCore(), PF.refreshFinance(), PF.refreshEvents(), refreshTimeline(), refreshPlan(), refreshInsights()]);
      toast('Обновлено');
    } catch (e) { fail(e); }
  });
  $('dash_events_refresh').addEventListener('click', () => PF.refreshEvents().catch(fail));
  $('operator_focus_refresh').addEventListener('click', async () => {
    const button = $('operator_focus_refresh');
    button.disabled = true;
    try {
      await Promise.all([PF.refreshCore(), PF.poll()]);
      toast('Центр действий обновлён');
    } catch (e) { fail(e); }
    finally { button.disabled = false; }
  });
  $('operator_focus').addEventListener('click', (e) => {
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
  $('dash_widgets_btn').addEventListener('click', () => { renderWidgetsList(); openModal('dash_widgets_modal'); });
  $('dash_widgets_list').addEventListener('change', (e) => {
    const cb = e.target.closest('[data-widget-check]');
    if (!cb) return;
    let prefs = widgetPrefs();
    if (cb.checked) { if (!prefs.includes(cb.dataset.widgetCheck)) prefs.push(cb.dataset.widgetCheck); }
    else prefs = prefs.filter((id) => id !== cb.dataset.widgetCheck);
    saveWidgetPrefs(prefs);
    applyWidgets();
    renderWidgetsList();
  });
  $('dash_widgets_reset').addEventListener('click', () => {
    saveWidgetPrefs(DASH_WIDGETS.map(([id]) => id));
    renderWidgetsList();
    applyWidgets();
    toast('Все виджеты возвращены');
  });
  $('dash_pdf').addEventListener('click', () => window.print());

  $('settings_save').addEventListener('click', saveSettings);
  $('settings_reset').addEventListener('click', resetSettings);
  $('mat_add').addEventListener('click', () => openMaterial(''));
  $('mat_save').addEventListener('click', saveMaterial);
  $('mat_reset').addEventListener('click', async () => {
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
  $('set_materials').addEventListener('click', (e) => {
    const edit = e.target.closest('[data-mat-edit]');
    const del = e.target.closest('[data-mat-del]');
    if (edit) return openMaterial(edit.dataset.matEdit);
    if (del) return deleteMaterial(del.dataset.matDel);
  });
  $('mat_base').addEventListener('change', (e) => fillMaterialFromBase(e.target.value));
  $('set_tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-pane]');
    if (!btn) return;
    settingsPane = btn.dataset.pane;
    $('set_search').value = '';
    filterSettings('');
  });
  $('set_search').addEventListener('input', U.debounce((e) => filterSettings(e.target.value), 150));
  $('set_tax_mode').addEventListener('change', (e) => {
    // Показываем поля выбранного режима сразу, не дожидаясь сохранения.
    PF.state.settings.tax_mode = e.target.value;
    renderSettings();
    $('set_search').value = '';
    filterSettings('');
  });
  $('set_accent').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-accent]');
    if (!btn) return;
    PF.state.settings.accent = btn.dataset.accent;
    PF.applyTheme();
    $$('#set_accent button').forEach((b) => b.classList.toggle('on', b === btn));
  });
  $('set_theme').addEventListener('change', (e) => {
    PF.state.settings.theme = e.target.value;
    PF.applyTheme();
  });
  $('set_printer_add').addEventListener('click', () => PF.modules.printer.openPrinterModal());
  if ($('rule_add')) $('rule_add').addEventListener('click', openRuleModal);
  if ($('rule_save')) $('rule_save').addEventListener('click', saveRule);
  $('set_printers').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-printer-edit]');
    if (btn) PF.modules.printer.openPrinterModal(btn.dataset.printerEdit);
  });
  const permBtn = $('notify_perm_btn');
  if (permBtn) permBtn.addEventListener('click', requestBrowserNotify);
  $('tg_test').addEventListener('click', async () => {
    const token = $$('[data-setting="telegram_token"]')[0].value;
    const chat = $$('[data-setting="telegram_chat_id"]')[0].value;
    try {
      const res = await post('/api/telegram/test', { telegram_token: token, telegram_chat_id: chat, telegram_enabled: true });
      if (res.ok) toast('Сообщение отправлено', 'Проверьте Telegram');
      else fail(new Error(res.error || 'Telegram не ответил'));
    } catch (e) { fail(e); }
  });

  $('backup_download').addEventListener('click', downloadBackup);
  $('backup_restore').addEventListener('click', restoreBackup);
  $('backup_import_ls').addEventListener('click', importLocalStorage);
  $('backup_wipe').addEventListener('click', wipeData);
  $('db_backup_now').addEventListener('click', async () => {
    try {
      const res = await post('/api/system/backup', {});
      if (!res.ok) return fail(new Error(res.error || 'Копия не создалась'));
      toast('Копия базы создана', res.file);
      loadDbBackups();
    } catch (e) { fail(e); }
  });
  loadDbBackups();
  const profSave=$('prof_save');
  if (profSave) profSave.addEventListener('click', async()=>{ const name=window.prompt('Название снапшота','Снапшот '+new Date().toLocaleString('ru-RU')); if(name==null) return; try{ await post('/api/settings/profile/save',{name}); renderProfiles(); toast('Снапшот сохранён', name);}catch(e){fail(e);} });
  $('data_check_btn').addEventListener('click', runDataCheck);

  $('env_add').addEventListener('click', () => envSave(''));
  $('set_envelopes').addEventListener('click', (e) => {
    const edit = e.target.closest('[data-env-edit]');
    if (edit) { envSave(edit.dataset.envEdit); return; }
    const out = e.target.closest('[data-env-out]');
    if (out) { envWithdraw(out.dataset.envOut); return; }
    const del = e.target.closest('[data-env-del]');
    if (del && confirmDanger('Удалить конверт? Движения сохранятся в истории.')) {
      post('/api/envelope/delete', { id: del.dataset.envDel }).then(() => loadEnvelopes()).catch(fail);
    }
  });

  $('lib_grid').addEventListener('click', (e) => {
    const card = e.target.closest('[data-article]');
    if (!card) return;
    e.preventDefault();
    PF.go('library', card.dataset.article);
  });
  $('lib_back').addEventListener('click', () => PF.go('library'));
}

/* =============================================================== старт */
PF.on('ready', () => {
  bind();
  renderSettings();
  initLibraryChecks();
  initCopyButtons();
  initTemplatesEditor();
  loadEnvelopes();
  refreshTimeline();
  refreshPlan();
  refreshInsights();
  loadTemplates();
  initBrowserNotify();
  checkUpdate(false);
  setInterval(refreshTimeline, 60000);
  setInterval(refreshPlan, 60000);
  setInterval(refreshInsights, 90000);
});
PF.on('data', renderDashboard);
PF.on('live', renderDashboard);
PF.on('finance', renderDashboard);
PF.on('events', renderEvents);
PF.on('printers', renderSettings);
PF.on('bootstrap', renderSettings);
PF.on('money', () => { if (document.querySelector('#view-settings.on')) renderSettings(); });
PF.on('view', (d) => {
  if (d.view === 'library') showArticle(d.sub || '');
  if (d.view === 'settings') { renderSettings(); loadMaterials(); }
  if (d.view === 'dashboard') renderDashboard();
});
window.addEventListener('resize', U.debounce(() => {
  if (document.querySelector('#view-dashboard.on')) renderDashboard();
  if (document.querySelector('#view-finance.on') && PF.modules.money) PF.modules.money.renderFinance();
}, 220));

PF.modules.settings = { downloadBackup, renderSettings, saveSettings, resetSettings, filterSettings };
})();
