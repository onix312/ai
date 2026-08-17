/* PrintFlow 2.0 — дашборд, настройки и библиотека. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, clamp, money, nfmt, pct, hoursText, minutesText,
  dateText, dateTimeText, agoText, toast, fail, confirmDanger, drawChart, legend, store } = U;
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
  const needGrams = activeOrders.reduce((a, o) => a + num(o.grams) * Math.max(1, num(o.qty, 1)), 0);
  const needHours = activeOrders.reduce((a, o) => a + num(o.hours) * Math.max(1, num(o.qty, 1)), 0);
  const capacity = num(PF.state.settings.weekly_capacity_hours, 110);
  const load = capacity ? clamp(needHours / capacity * 100, 0, 999) : 0;
  const stock = num(s.stock_grams);

  $('dash_kpis').innerHTML = [
    kpi('Печатает сейчас', `${nfmt(farm.printing)} / ${nfmt(farm.total)}`,
      `${nfmt(farm.online)} на связи · загрузка ${nfmt(farm.utilization)}%`,
      num(farm.printing) ? 'ok' : ''),
    kpi('Очередь печати', hoursText(needHours), `${pct(load)} от ${nfmt(capacity)} ч в неделю`,
      load > 100 ? 'bad' : load > 85 ? 'warn' : '',
      `<div class="bar ${load > 100 ? 'bad' : load > 85 ? 'warn' : ''}"><i style="width:${clamp(load, 0, 100)}%"></i></div>`),
    kpi('Активные заказы', String(activeOrders.length), `${nfmt(farm.queued)} заданий в очереди`),
    kpi('Требуют внимания', String(late.length), 'срок сегодня или прошёл', late.length ? 'bad' : 'ok'),
    kpi('Ждём оплату', money(pipeline), 'по активным заказам'),
    kpi('Прибыль за период', money(s.profit), `маржа ${pct(s.margin)}`, num(s.profit) >= 0 ? 'ok' : 'bad'),
    kpi('Сегодня напечатано', `${nfmt(farm.today_hours, 1)} ч`, `${nfmt(farm.today_grams)} г · ${nfmt(farm.today_jobs)} задан.`),
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
}

function renderEvents() {
  const list = PF.state.events || [];
  $('dash_events').innerHTML = list.length ? list.slice(0, 18).map((e) => `<div class="event ${esc(e.kind)}">`
    + '<span class="edot"></span><span class="etext">'
    + `<b>${esc(e.title)}</b><small>${esc(e.detail || '')}</small></span>`
    + `<time title="${esc(dateTimeText(e.at))}">${esc(agoText(e.at))}</time></div>`).join('')
    : '<div class="empty compact"><span>Событий пока нет.</span></div>';
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
  ['vat_enabled', 'Я плачу НДС', 'Включается после порога дохода', 'bool'],
  ['vat_rate', 'Ставка НДС, %', '5% без вычетов или 22% с вычетами', 'num', 1],
  ['vat_threshold', 'Порог освобождения от НДС, ₽', 'С 2026 года — 20 млн ₽ за год', 'num', 1000000],
  ['tax_reserve_enabled', 'Считать резерв под налог', 'Подсказка, сколько отложить', 'bool'],
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
  ['low_margin_alert', 'Предупреждать при марже ниже, %', 'Заказ подсветится как невыгодный', 'num', 1],
];
const AUTOS = [
  ['auto_accounting', 'Автоматический учёт', 'Считать себестоимость по фактам печати'],
  ['auto_link_orders', 'Связывать печать с заказом', 'По имени файла и номеру заказа'],
  ['auto_consume_filament', 'Списывать пластик', 'С катушки, которая стояла в AMS'],
  ['auto_income_on_done', 'Доход при закрытии заказа', 'Проводка создаётся автоматически'],
  ['auto_queue', 'Автозапуск очереди', 'Следующее задание стартует само'],
];
const NOTIFY = [
  ['notify_complete', 'Завершение печати'],
  ['notify_error', 'Ошибки и HMS'],
  ['notify_pause', 'Пауза'],
  ['notify_filament_low', 'Пластик заканчивается'],
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
  $('set_money_rules').innerHTML = settingGroup(MONEY_RULES)
    + settingRow('default_account', 'Касса по умолчанию', 'Куда попадают деньги без уточнения',
      `<select data-setting="default_account">${accounts.map((a) =>
        `<option value="${esc(a.id)}"${(s.default_account || 'cash') === a.id ? ' selected' : ''}>${esc(a.name)}</option>`)
        .join('') || '<option value="cash">Наличные</option>'}</select>`);
  $('set_auto').innerHTML = AUTOS.map(([k, label, sub]) => settingRow(k, label, sub,
    `<label class="switch"><input type="checkbox" data-setting="${k}"${s[k] ? ' checked' : ''}><i></i></label>`)).join('');
  $('set_tg').innerHTML = settingRow('telegram_enabled', 'Включить Telegram', 'Уведомления о печати',
    `<label class="switch"><input type="checkbox" data-setting="telegram_enabled"${s.telegram_enabled ? ' checked' : ''}><i></i></label>`)
    + settingRow('telegram_token', 'Bot Token', s.has_telegram_token ? 'Сохранён — оставьте пустым, чтобы не менять' : 'Получите у @BotFather',
      '<input type="password" autocomplete="new-password" data-setting="telegram_token" placeholder="' + (s.has_telegram_token ? '••••••••' : 'токен') + '">')
    + settingRow('telegram_chat_id', 'Chat ID', 'Ваш идентификатор в Telegram',
      `<input type="text" data-setting="telegram_chat_id" value="${esc(String(s.telegram_chat_id || ''))}">`)
    + NOTIFY.map(([k, label]) => settingRow(k, label, '',
      `<label class="switch"><input type="checkbox" data-setting="${k}"${s[k] ? ' checked' : ''}><i></i></label>`)).join('');

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
      await Promise.all([PF.refreshCore(), PF.refreshFinance(), PF.refreshEvents()]);
      toast('Обновлено');
    } catch (e) { fail(e); }
  });
  $('dash_events_refresh').addEventListener('click', () => PF.refreshEvents().catch(fail));

  $('settings_save').addEventListener('click', saveSettings);
  $('settings_reset').addEventListener('click', resetSettings);
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
  $('set_printers').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-printer-edit]');
    if (btn) PF.modules.printer.openPrinterModal(btn.dataset.printerEdit);
  });
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
  if (d.view === 'settings') renderSettings();
  if (d.view === 'dashboard') renderDashboard();
});
window.addEventListener('resize', U.debounce(() => {
  if (document.querySelector('#view-dashboard.on')) renderDashboard();
  if (document.querySelector('#view-finance.on') && PF.modules.money) PF.modules.money.renderFinance();
}, 220));

PF.modules.settings = { downloadBackup, renderSettings, saveSettings, resetSettings, filterSettings };
})();
