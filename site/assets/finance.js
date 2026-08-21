/* PrintFlow 2.0 — расширенный учёт: P&L по месяцам, налоги, кассы, долги,
   точка безубыточности, отчёты и справочники (каналы, статьи, постоянные
   расходы). Все расчёты выполняет коннектор, здесь только отображение. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, clamp, money, nfmt, pct, dateText,
  toast, fail, openModal, closeModal, confirmDanger, drawChart, legend } = U;
const { get, post } = PF.api;

/* Справочники и агрегаты держим в общем состоянии: их читают и «Финансы»,
   и «Настройки», и модалка проводки. */
PF.state.money = null;
PF.state.accounts = [];
PF.state.channels = [];
PF.state.expenseCategories = [];
PF.state.fixedCosts = [];
PF.state.report = null;

let repPeriod = 'month', repOffset = 0;
let editingAccount = null, editingChannel = null, editingFixed = null,
  editingCat = null, payingOrder = null, payingDebt = 0, payingPaid = 0,
  payingOrderUpdatedAt = '', paymentRequestId = '',
  reminderDraft = null;

const MODE_HINTS = {
  none: 'Налог не считается. Подходит, пока вы только пробуете и продаёте знакомым.',
  npd: 'Самозанятость: 4% с продаж физлицам и 6% с продаж юрлицам. Лимит дохода — 2,4 млн ₽ в год, страховые взносы платить не нужно.',
  usn6: 'УСН «Доходы»: 6% с выручки. Налог уменьшается на страховые взносы ИП — без сотрудников вплоть до нуля.',
  usn15: 'УСН «Доходы минус расходы»: 15% с прибыли, но не меньше минимального налога 1% с дохода. Нужны документы на расходы.',
  patent: 'Патент: фиксированная стоимость за год, налог с оборота не считается. Укажите стоимость патента ниже.',
  manual: 'Своя ставка: система просто умножит доход на указанный процент.',
};

function kpi(label, value, sub, kind, extra) {
  return `<div class="kpi ${kind || ''}"><span class="label">${esc(label)}</span>`
    + `<b class="value">${value}</b><span class="sub">${sub || ''}</span>${extra || ''}</div>`;
}
function row(label, value, cls) {
  return `<div class="res-row ${cls || ''}"><span class="lbl">${esc(label)}</span>`
    + `<span class="val">${value}</span></div>`;
}
const signed = (v) => (num(v) >= 0 ? '+' : '−') + money(Math.abs(num(v)));
const monthLabel = (key) => {
  const names = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  const m = String(key || '').split('-');
  return m.length === 2 ? `${names[+m[1] - 1] || m[1]} ${m[0].slice(2)}` : String(key || '');
};

/* ======================================================= загрузка данных */
async function refreshMoney() {
  try {
    const data = await get('/api/money', { months: 6 });
    PF.state.money = data;
    PF.state.accounts = (data.accounts && data.accounts.accounts) || [];
    PF.state.channels = data.channels || [];
    PF.state.expenseCategories = data.categories || [];
    PF.state.fixedCosts = data.fixed_costs || [];
    PF.emit('money', data);
    return data;
  } catch (e) {
    // Молча: финансовые вкладки просто останутся с прошлыми данными.
    return null;
  }
}
PF.refreshMoney = refreshMoney;

async function refreshReport() {
  try {
    PF.state.report = await get('/api/report', { period: repPeriod, offset: repOffset });
    renderReport();
  } catch (e) { fail(e); }
}

/* ================================================================== P&L */
function renderPnl() {
  const data = PF.state.money;
  if (!data || !$('pnl_kpis')) return;
  const pnl = data.pnl || {};
  const cur = pnl.current || {};
  const cmp = pnl.compare || {};
  const dir = (f) => {
    const d = cmp[f] || {};
    if (!num(d.was)) return num(d.now) ? 'новый период' : 'нет данных';
    return `${num(d.diff) >= 0 ? '↑' : '↓'} ${pct(Math.abs(num(d.percent)))} к прошлому месяцу`;
  };
  $('pnl_kpis').innerHTML = [
    kpi('Доход за месяц', money(cur.income), dir('income'), num(cur.income) ? 'ok' : ''),
    kpi('Расход за месяц', money(cur.expense), dir('expense')),
    kpi('Прибыль', money(cur.profit), dir('profit'), num(cur.profit) >= 0 ? 'ok' : 'bad'),
    kpi('Маржа', pct(cur.margin), `в среднем ${money(pnl.average_profit)} прибыли в месяц`,
      num(cur.margin) >= num(PF.state.settings.low_margin_alert, 20) ? 'ok' : num(cur.margin) ? 'warn' : ''),
  ].join('');

  const series = (pnl.months || []).map((m) => ({
    day: monthLabel(m.key), income: num(m.income), expense: num(m.expense), profit: num(m.profit),
  }));
  const keys = [
    { key: 'income', label: 'Доход', color: 'var(--ok)', type: 'bar', fmt: (v) => money(v) },
    { key: 'expense', label: 'Расход', color: 'var(--bad)', type: 'bar', opacity: .6, fmt: (v) => money(v) },
    { key: 'profit', label: 'Прибыль', color: 'var(--accent)', type: 'line', fmt: (v) => money(v) },
  ];
  drawChart($('pnl_chart'), $('pnl_tip'), series, keys, { height: 230, fmtAxis: (v) => nfmt(v) });
  legend($('pnl_legend'), keys);

  $('pnl_tbody').innerHTML = (pnl.months || []).length
    ? pnl.months.slice().reverse().map((m) => `<tr>`
      + `<td class="strong">${esc(monthLabel(m.key))}</td>`
      + `<td class="right tnum">${money(m.income)}</td>`
      + `<td class="right tnum">${num(m.fees) ? '−' + money(m.fees) : '—'}</td>`
      + `<td class="right tnum">${num(m.variable) ? '−' + money(m.variable) : '—'}</td>`
      + `<td class="right tnum">${num(m.fixed) ? '−' + money(m.fixed) : '—'}</td>`
      + `<td class="right tnum">${num(m.taxes) ? '−' + money(m.taxes) : '—'}</td>`
      + `<td class="right tnum ${num(m.profit) >= 0 ? 'pos' : 'neg'}">${money(m.profit)}</td>`
      + `<td class="right tnum">${num(m.income) ? pct(m.margin) : '—'}</td></tr>`).join('')
    : '<tr><td colspan="8"><div class="empty compact"><span>Данных пока нет.</span></div></td></tr>';

  renderBreakEven(data.break_even || {});
}

function renderBreakEven(be) {
  const host = $('be_box');
  if (!host) return;
  const need = num(be.revenue_needed);
  const progress = clamp(num(be.progress), 0, 100);
  host.innerHTML = '<div class="res-list">'
    + row('Постоянные расходы в месяц', money(be.fixed_monthly))
    + (num(be.insurance_monthly) ? row('Страховые взносы (в месяц)', money(be.insurance_monthly)) : '')
    + row('Итого нужно покрыть', money(be.fixed_total), 'total')
    + row('Средняя маржа', pct(be.avg_margin))
    + row('Средний чек', num(be.avg_price) ? money(be.avg_price) : '—')
    + '</div>'
    + (need
      ? `<div style="margin-top:12px"><div class="res-row" style="border:0;padding:0 0 5px">`
        + `<span class="lbl">Выручка для нуля</span><span class="val">${money(need)}</span></div>`
        + `<div class="bar ${progress >= 100 ? 'ok' : 'warn'}"><i style="width:${progress}%"></i></div>`
        + `<small class="muted">${num(be.income_now) >= need
          ? `точка пройдена: заработано ${money(be.income_now)}, это ${nfmt(num(be.income_now) / need, 1)}× от порога`
          : `уже заработано ${money(be.income_now)} — это ${pct(progress)} от точки безубыточности`}`
        + `${num(be.orders_needed) ? ` · порог примерно ${nfmt(be.orders_needed, 1)} заказ(ов)` : ''}</small></div>`
      : '<div class="notice" style="margin-top:12px"><span>ℹ</span><span>Постоянных расходов нет — вы в плюсе с первого проданного изделия. Добавьте аренду или подписки, если они появятся.</span></div>')
    + `<div class="res-list" style="margin-top:12px">`
    + row('Цель по прибыли', money(be.goal_profit))
    + row('Нужна выручка', num(be.revenue_goal) ? money(be.revenue_goal) : '—', 'total')
    + (num(be.orders_goal) ? row('Это заказов за месяц', nfmt(be.orders_goal, 1)) : '')
    + '</div>';
}

/* =============================================================== налоги */
function renderTax() {
  const data = PF.state.money;
  if (!data || !$('tax_kpis')) return;
  const t = data.tax || {};
  const limitUsed = clamp(num(t.limit_used), 0, 100);
  $('tax_mode_name').textContent = t.mode_name || 'Налоговый режим';
  $('tax_kpis').innerHTML = [
    kpi('Налог за год', money(t.tax_due), `режим: ${esc(t.mode_name || '—')}`),
    kpi('Страховые взносы', money(t.insurance_due), t.mode === 'npd' ? 'на НПД не платятся' : 'фиксированная часть и 1%'),
    kpi('Уже уплачено', money(num(t.tax_paid) + num(t.insurance_paid)), 'по проводкам за год'),
    kpi('Осталось заплатить', money(t.total_due), num(t.total_due) ? 'отложите заранее' : 'всё закрыто',
      num(t.total_due) ? 'warn' : 'ok'),
  ].join('');

  $('tax_box').innerHTML = '<div class="res-list">'
    + row('Доход за год', money(t.income))
    + (t.mode === 'npd'
      ? row('от физлиц / от юрлиц', `${money(t.income_person)} / ${money(t.income_company)}`)
      : row('Расходы, принимаемые к учёту', money(t.expense)))
    + row('Налог начислен', money(t.tax_due), 'total')
    + (num(t.insurance_due) ? row('Взносы начислены', money(t.insurance_due)) : '')
    + '</div>'
    + (num(t.limit)
      ? `<div style="margin-top:12px"><div class="res-row" style="border:0;padding:0 0 5px">`
        + `<span class="lbl">Лимит режима</span><span class="val">${money(t.limit)}</span></div>`
        + `<div class="bar ${limitUsed > 80 ? 'warn' : 'ok'}"><i style="width:${limitUsed}%"></i></div>`
        + `<small class="muted">использовано ${pct(limitUsed)}</small></div>`
      : '')
    + (t.notes || []).map((n) => `<div class="tax-note">${esc(n)}</div>`).join('');

  // Резерв уже учитывает уплаченное, второй раз вычитать нельзя.
  const reserve = num(t.reserve);
  const rate = clamp(num(t.reserve_rate), 0, 100);
  $('tax_reserve').innerHTML = '<div class="res-list">'
    + row('Ещё нужно отложить', money(reserve), 'total')
    + row('Уже уплачено за год', money(num(t.tax_paid) + num(t.insurance_paid)))
    + row('Это от годового дохода', pct(rate))
    + '</div>'
    + (t.mode === 'none'
      ? '<div class="notice" style="margin-top:12px"><span>ℹ</span><span>Режим не выбран, налог не считается. Откройте «Настройки → Налоги» и укажите свою схему — расчёты по заказам сразу станут точнее.</span></div>'
      : !reserve
        ? '<div class="notice" style="margin-top:12px"><span>✓</span><span>Всё начисленное уже уплачено — откладывать пока нечего.</span></div>'
        : `<div class="notice" style="margin-top:12px"><span>ℹ</span><span>${rate >= 50
          ? 'Основная часть суммы — фиксированные страховые взносы: они не зависят от дохода, поэтому при небольшой выручке доля выглядит большой. Откладывайте равными частями до конца года.'
          : `Откладывайте ${pct(rate)} с каждого поступления на отдельный счёт — тогда налог не станет сюрпризом.`}</span></div>`);

  const qs = t.quarters || [];
  const qSum = (key) => qs.reduce((a, q) => a + (Number(q[key]) || 0), 0);
  $('tax_tbody').innerHTML = qs.map((q) => `<tr>`
    + `<td class="strong">${esc(q.key)}</td>`
    + `<td class="right tnum">${money(q.income)}</td>`
    + `<td class="right tnum">${money(q.expense)}</td>`
    + `<td class="right tnum">${money(q.tax)}</td></tr>`).join('')
    + (qs.length
      ? `<tr class="sum-row"><td class="strong">За год</td>`
        + `<td class="right tnum strong">${money(qSum('income'))}</td>`
        + `<td class="right tnum strong">${money(qSum('expense'))}</td>`
        + `<td class="right tnum strong">${money(qSum('tax'))}</td></tr>`
      : '')
    || '<tr><td colspan="4"><div class="empty compact"><span>Нет данных за год.</span></div></td></tr>';
}

/* ======================================================= кассы и долги */
function renderCash() {
  const data = PF.state.money;
  if (!data || !$('cash_kpis')) return;
  const acc = data.accounts || {};
  const debts = data.debts || {};
  const fixedMonthly = (data.fixed_costs || []).filter((f) => num(f.active))
    .reduce((a, f) => a + num(f.amount) / ({ month: 1, quarter: 3, year: 12 }[f.period] || 1), 0);
  $('cash_kpis').innerHTML = [
    kpi('Деньги в кассах', money(acc.total), `${(acc.accounts || []).length} счёт(ов)`, num(acc.total) >= 0 ? 'ok' : 'bad'),
    kpi('Долги клиентов', money(debts.total), `${nfmt(debts.count)} заказ(ов) не оплачены`, num(debts.total) ? 'warn' : 'ok'),
    kpi('Просрочено', money(debts.overdue), `дольше ${nfmt(PF.state.settings.debt_alert_days, 0)} дней`,
      num(debts.overdue) ? 'bad' : 'ok'),
    kpi('Постоянные расходы', money(fixedMonthly), 'в месяц'),
  ].join('');

  $('cash_accounts').innerHTML = (acc.accounts || []).length
    ? acc.accounts.map((a) => `<div class="mini-row">`
      + `<div class="mbody"><b>${esc(a.name)}</b><small>${esc(kindName(a.kind))}`
      + `${num(a.fee_percent) ? ` · комиссия ${nfmt(a.fee_percent, 1)}%` : ''} · ${nfmt(a.moves)} операц.</small></div>`
      + `<span class="mval ${num(a.balance) >= 0 ? 'pos' : 'neg'}">${money(a.balance)}</span>`
      + `<button class="icon-btn sm" type="button" data-acc-edit="${esc(a.id)}">✎</button></div>`).join('')
    : '<div class="empty compact"><span>Кассы не заданы.</span></div>';

  $('cash_fixed').innerHTML = renderFixedList();

  const rows = debts.rows || [];
  $('debt_tbody').innerHTML = rows.length ? rows.map((d) => `<tr>`
    + `<td class="strong">№${esc(String(d.number || '—'))}</td>`
    + `<td>${esc(d.customer || 'без имени')}</td>`
    + `<td class="right tnum">${money(d.price)}</td>`
    + `<td class="right tnum">${money(d.paid)}</td>`
    + `<td class="right tnum neg">${money(d.debt)}</td>`
    + `<td class="right tnum">${nfmt(d.days)}${d.overdue ? ' <span class="pill bad">просрочка</span>' : ''}`
      + `${d.reminded_at ? `<small class="muted">напомнили ${esc(dateText(d.reminded_at))}</small>` : ''}</td>`
    + `<td class="right"><button class="btn sm ghost" type="button" data-remind-order="${esc(d.id)}">Напомнить</button> <button class="btn sm" type="button" data-pay-order="${esc(d.id)}">Получить оплату</button></td></tr>`).join('')
    : '<tr><td colspan="7"><div class="empty compact"><span>Все заказы оплачены полностью.</span></div></td></tr>';
}

const kindName = (k) => ({ cash: 'наличные', card: 'карта', bank: 'расчётный счёт' }[k] || 'счёт');

function renderFixedList() {
  const list = PF.state.fixedCosts || [];
  const per = { month: 'ежемесячно', quarter: 'раз в квартал', year: 'раз в год' };
  return list.length ? list.map((f) => `<div class="mini-row">`
    + `<span class="dot ${num(f.active) ? 'on' : ''}"></span>`
    + `<div class="mbody"><b>${esc(f.name)}</b><small>${esc(per[f.period] || 'ежемесячно')}`
    + `, ${nfmt(f.day)}-го${f.last_charged ? ` · последнее начисление ${esc(f.last_charged)}` : ''}</small></div>`
    + `<span class="mval">${money(f.amount)}</span>`
    + `<button class="icon-btn sm" type="button" data-fix-edit="${esc(f.id)}">✎</button></div>`).join('')
    : '<div class="empty compact"><span>Постоянных расходов нет — и хорошо. Добавьте, если появится аренда или подписка.</span></div>';
}

/* =============================================================== отчёты */
function renderReport() {
  const r = PF.state.report;
  if (!r || !$('rep_kpis')) return;
  $('rep_label').textContent = `${r.label} · с ${dateText(r.start)} по ${dateText(r.end)}`;
  $('rep_kpis').innerHTML = [
    kpi('Заказов', nfmt(r.orders), `средний чек ${money(r.avg_check)}`),
    kpi('Выручка', money(r.revenue), `прибыль ${money(r.profit)}`, num(r.profit) >= 0 ? 'ok' : 'bad'),
    kpi('Маржа', pct(r.margin), `${nfmt(r.hours, 1)} ч печати · ${nfmt(r.grams)} г`),
    kpi('Денежный поток', money(r.cash_flow), `+${money(r.cash_in)} / −${money(r.cash_out)}`,
      num(r.cash_flow) >= 0 ? 'ok' : 'bad'),
  ].join('');

  const empty = (cols, text) => `<tr><td colspan="${cols}"><div class="empty compact"><span>${text}</span></div></td></tr>`;
  $('rep_customers').innerHTML = (r.customers || []).length
    ? r.customers.map((c) => `<tr><td class="strong">${esc(c.name)}</td>`
      + `<td class="right tnum">${nfmt(c.orders)}</td><td class="right tnum">${money(c.revenue)}</td>`
      + `<td class="right tnum ${num(c.profit) >= 0 ? 'pos' : 'neg'}">${money(c.profit)}</td></tr>`).join('')
    : empty(4, 'В этом периоде заказов не было.');
  $('rep_products').innerHTML = (r.products || []).length
    ? r.products.map((p) => `<tr><td class="strong">${esc(p.name)}</td>`
      + `<td class="right tnum">${nfmt(p.qty)}</td><td class="right tnum">${money(p.revenue)}</td>`
      + `<td class="right tnum ${num(p.profit) >= 0 ? 'pos' : 'neg'}">${money(p.profit)}</td></tr>`).join('')
    : empty(4, 'Нет проданных изделий за период.');
  $('rep_channels').innerHTML = (r.channels || []).length
    ? r.channels.map((c) => `<tr><td class="strong">${esc(c.name)}</td>`
      + `<td class="right tnum">${nfmt(c.orders)}</td><td class="right tnum">${money(c.revenue)}</td>`
      + `<td class="right tnum">${num(c.fee) ? '−' + money(c.fee) : '—'}</td>`
      + `<td class="right tnum ${num(c.profit) >= 0 ? 'pos' : 'neg'}">${money(c.profit)}</td></tr>`).join('')
    : empty(5, 'Каналы продаж пока не использовались.');

  const exps = r.expenses || [];
  const max = Math.max(1, ...exps.map((e) => num(e.amount)));
  const total = exps.reduce((a, e) => a + num(e.amount), 0);
  $('rep_expenses').innerHTML = exps.length
    ? exps.map((e) => `<div class="exp-bar"><div class="top"><b>${esc(e.name)}</b>`
      + `<span>${money(e.amount)} · ${pct(total ? num(e.amount) / total * 100 : 0)}</span></div>`
      + `<div class="bar"><i style="width:${clamp(num(e.amount) / max * 100, 2, 100)}%"></i></div></div>`).join('')
    : '<div class="empty compact"><span>Расходов за период не было.</span></div>';
}

/* ============================================== справочники в настройках */
function renderDirectories() {
  if ($('set_accounts')) {
    const list = PF.state.accounts || [];
    const state = (PF.state.money && PF.state.money.accounts && PF.state.money.accounts.accounts) || [];
    $('set_accounts').innerHTML = list.length ? list.map((a) => {
      const st = state.find((x) => x.id === a.id) || {};
      return `<div class="mini-row"><div class="mbody"><b>${esc(a.name)}</b>`
        + `<small>${esc(kindName(a.kind))}${num(a.fee_percent) ? ` · комиссия ${nfmt(a.fee_percent, 1)}%` : ''}`
        + `${num(a.archived) ? ' · в архиве' : ''}</small></div>`
        + `<span class="mval">${money(st.balance || a.opening_balance)}</span>`
        + `<button class="icon-btn sm" type="button" data-acc-edit="${esc(a.id)}">✎</button></div>`;
    }).join('') : '<div class="empty compact"><span>Кассы не заданы.</span></div>';
  }
  if ($('set_channels')) {
    const list = PF.state.channels || [];
    $('set_channels').innerHTML = list.length ? list.map((c) => `<div class="mini-row">`
      + `<span class="dot ${num(c.active) ? 'on' : ''}"></span>`
      + `<div class="mbody"><b>${esc(c.name)}</b><small>`
      + `${num(c.fee_percent) ? `комиссия ${nfmt(c.fee_percent, 1)}%` : 'без комиссии'}`
      + `${num(c.fee_fixed) ? ` + ${money(c.fee_fixed)}` : ''}`
      + `${num(c.ads_per_order) ? ` · реклама ${money(c.ads_per_order)}/заказ` : ''}`
      + ` · ${c.payer === 'company' ? 'юрлица' : 'физлица'}</small></div>`
      + `<button class="icon-btn sm" type="button" data-ch-edit="${esc(c.id)}">✎</button></div>`).join('')
      : '<div class="empty compact"><span>Каналы не заданы.</span></div>';
  }
  if ($('set_fixed')) $('set_fixed').innerHTML = renderFixedList();
  if ($('set_categories')) {
    const grp = { variable: 'переменные', fixed: 'постоянные', invest: 'вложения', tax: 'налоги', owner: 'вывод себе' };
    $('set_categories').innerHTML = (PF.state.expenseCategories || []).map((c) => `<div class="mini-row">`
      + `<div class="mbody"><b>${esc(c.name)}</b><small>${esc(grp[c.grp] || c.grp)}</small></div>`
      + `<button class="icon-btn sm" type="button" data-cat2-edit="${esc(c.id)}">✎</button></div>`).join('')
      || '<div class="empty compact"><span>Статей нет.</span></div>';
  }
  fillSelects();
}

/** Заполняет выпадающие списки касс, статей и каналов во всех модалках. */
function fillSelects() {
  const accOpts = (PF.state.accounts || []).filter((a) => !num(a.archived))
    .map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
  const catOpts = (PF.state.expenseCategories || [])
    .map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  const chOpts = '<option value="">— без канала —</option>' + (PF.state.channels || [])
    .filter((c) => num(c.active)).map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  const incomeCats = '<option value="sale">Продажа</option><option value="order">Заказ</option>'
    + '<option value="prepay">Предоплата</option><option value="other">Прочий доход</option>';
  const txCat = $('tf_category');
  if (txCat) {
    txCat.dataset.income = incomeCats;
    txCat.dataset.expense = catOpts;
  }
  [['tf_account', accOpts], ['ff_account', accOpts], ['pf_account', accOpts],
    ['tf_category', txCat && txCat.dataset.kind === 'income' ? incomeCats : catOpts],
    ['ff_category', catOpts], ['tf_channel', chOpts]].forEach(([id, html]) => {
    const el = $(id);
    if (!el || !html) return;
    const keep = el.value;
    el.innerHTML = html;
    if (keep && [...el.options].some((o) => o.value === keep)) el.value = keep;
  });
}

/* =============================================================== модалки */
function openAccount(id) {
  const a = (PF.state.accounts || []).find((x) => x.id === id) || {};
  editingAccount = a.id || null;
  $('af_name').value = a.name || '';
  $('af_kind').value = a.kind || 'cash';
  $('af_fee').value = num(a.fee_percent);
  $('af_opening').value = num(a.opening_balance);
  $('af_note').value = a.note || '';
  $('account_delete').hidden = !a.id;
  openModal('account_modal');
}
function openChannel(id) {
  const c = (PF.state.channels || []).find((x) => x.id === id) || {};
  editingChannel = c.id || null;
  $('chf_name').value = c.name || '';
  $('chf_fee').value = num(c.fee_percent);
  $('chf_fixed').value = num(c.fee_fixed);
  $('chf_ads').value = num(c.ads_per_order);
  $('chf_payer').value = c.payer || 'person';
  $('chf_active').checked = c.id ? !!num(c.active) : true;
  $('channel_delete').hidden = !c.id;
  openModal('channel_modal');
}
function openFixed(id) {
  fillSelects();
  const f = (PF.state.fixedCosts || []).find((x) => x.id === id) || {};
  editingFixed = f.id || null;
  $('ff_name').value = f.name || '';
  $('ff_amount').value = f.id ? num(f.amount) : '';
  $('ff_period').value = f.period || 'month';
  $('ff_day').value = num(f.day, 1) || 1;
  if (f.category) $('ff_category').value = f.category;
  else if ($('ff_category').querySelector('[value="rent"]')) $('ff_category').value = 'rent';
  if (f.account_id) $('ff_account').value = f.account_id;
  $('ff_note').value = f.note || '';
  $('ff_active').checked = f.id ? !!num(f.active) : true;
  $('ff_deductible').checked = f.id ? !!num(f.deductible) : true;
  $('fixed_delete').hidden = !f.id;
  openModal('fixed_modal');
}
function openExpCat(id) {
  const c = (PF.state.expenseCategories || []).find((x) => x.id === id) || {};
  editingCat = c.id || null;
  $('ecf_name').value = c.name || '';
  $('ecf_grp').value = c.grp || 'variable';
  $('ecf_is_fixed').checked = !!num(c.is_fixed);
  $('expcat_delete').hidden = !c.id;
  openModal('expcat_modal');
}
function syncPaymentKind() {
  const kind = $('pf_kind').value || 'payment';
  const limit = kind === 'refund' ? payingPaid : payingDebt;
  $('pf_amount').max = limit > 0 ? String(limit) : '0';
  if (num($('pf_amount').value) > limit) $('pf_amount').value = limit || '';
  const title = kind === 'refund' ? 'Сумма возврата' : kind === 'prepay' ? 'Сумма предоплаты' : 'Сумма оплаты';
  const label = $('pf_amount').closest('.field')?.querySelector('span');
  if (label) label.textContent = `${title}, ₽`;
  const info = $('pay_info').lastElementChild;
  if (info) {
    info.textContent = kind === 'refund'
      ? `Получено ранее ${money(payingPaid)} · доступный возврат до ${money(payingPaid)}.`
      : `${kind === 'prepay' ? 'Предоплата' : 'Оплата'}: доступно до ${money(payingDebt)}.`;
  }
}

function openPayment(orderId) {
  fillSelects();
  const debts = (PF.state.money && PF.state.money.debts && PF.state.money.debts.rows) || [];
  const d = debts.find((x) => x.id === orderId)
    || (PF.state.orders || []).find((o) => o.id === orderId) || {};
  payingOrder = orderId;
  payingDebt = num(d.debt) || Math.max(0, num(d.price) - Math.max(num(d.paid), num(d.prepaid)));
  payingPaid = Math.max(num(d.paid), num(d.prepaid));
  payingOrderUpdatedAt = String(d.updated_at || '');
  paymentRequestId = (window.crypto && window.crypto.randomUUID)
    ? window.crypto.randomUUID() : `payment-${orderId}-${Date.now()}`;
  $('pay_title').textContent = `Платёж по заказу №${d.number || ''}`.trim();
  $('pay_info').lastElementChild.textContent = payingDebt
    ? `${d.customer || d.customer_name || 'Клиент'} · цена ${money(d.price)}, оплачено ${money(payingPaid)}, остаток ${money(payingDebt)}.`
    : `Заказ оплачен полностью · получено ${money(payingPaid)}. Можно оформить возврат.`;
  $('pf_kind').value = payingDebt ? 'payment' : 'refund';
  $('pf_kind').disabled = false;
  $('pf_amount').value = payingDebt || payingPaid || '';
  $('pf_method').value = '';
  $('pf_note').value = '';
  syncPaymentKind();
  openModal('payment_modal');
}

/* =============================================================== события */
function bindTabs(hostId, prefix, onSwitch) {
  const host = $(hostId);
  if (!host) return;
  host.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-pane]');
    if (!btn) return;
    $$(`#${hostId} button`).forEach((b) => b.classList.toggle('on', b === btn));
    $$(`[id^="${prefix}-"]`).forEach((p) => p.classList.toggle('on', p.id === `${prefix}-${btn.dataset.pane}`));
    if (onSwitch) onSwitch(btn.dataset.pane);
  });
}

function downloadCsv(filename, csv) {
  // BOM нужен, чтобы Excel открыл русский текст без «кракозябр».
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
  toast('Файл сохранён', filename);
}

function bind() {
  bindTabs('fin_tabs', 'finpane', (pane) => {
    if (pane === 'reports' && !PF.state.report) refreshReport();
  });

  const btn = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };

  btn('fin_export_tx', async () => {
    try {
      const r = await get('/api/export/transactions', { days: 365 });
      downloadCsv(r.filename, r.csv);
    } catch (e) { fail(e); }
  });
  btn('rep_export', async () => {
    try {
      const r = await get('/api/export/report', { period: repPeriod, offset: repOffset });
      downloadCsv(r.filename, r.csv);
    } catch (e) { fail(e); }
  });
  btn('rep_prev', () => { repOffset += 1; refreshReport(); });
  btn('rep_next', () => { repOffset = Math.max(0, repOffset - 1); refreshReport(); });
  const repSeg = $('rep_period');
  if (repSeg) {
    repSeg.addEventListener('click', (e) => {
      const b = e.target.closest('[data-period]');
      if (!b) return;
      $$('#rep_period button').forEach((x) => x.classList.toggle('on', x === b));
      repPeriod = b.dataset.period;
      repOffset = 0;
      refreshReport();
    });
  }
  btn('tax_go_settings', () => {
    PF.go('settings');
    const tab = document.querySelector('#set_tabs [data-pane="tax"]');
    if (tab) tab.click();
  });
  btn('tax_pay', async () => {
    const t = (PF.state.money && PF.state.money.tax) || {};
    const suggested = Math.max(0, num(t.total_due));
    const value = window.prompt('Сумма уплаченного налога, ₽:', String(Math.round(suggested)));
    if (value == null) return;
    const amount = num(value);
    if (amount <= 0) return fail(new Error('Сумма должна быть больше нуля'));
    try {
      await post('/api/tax/pay', { amount, title: 'Уплата налога', category: 'tax' });
      toast('Записано', money(amount));
      await refreshMoney();
      PF.refreshFinance();
    } catch (e) { fail(e); }
  });

  btn('acc_add', () => openAccount());
  btn('acc_add2', () => openAccount());
  btn('ch_add', () => openChannel());
  btn('fix_add', () => openFixed());
  btn('fix_add2', () => openFixed());
  btn('cat_expense_add', () => openExpCat());

  btn('account_save', async () => {
    const payload = {
      id: editingAccount || '', name: $('af_name').value.trim(), kind: $('af_kind').value,
      fee_percent: num($('af_fee').value), opening_balance: num($('af_opening').value),
      note: $('af_note').value.trim(),
    };
    if (!payload.name) return fail(new Error('Укажите название кассы'));
    try {
      await post('/api/account/save', payload);
      closeModal('account_modal');
      toast('Касса сохранена', payload.name);
      await refreshMoney();
      renderAll();
    } catch (e) { fail(e); }
  });
  btn('account_delete', async () => {
    if (!editingAccount || !confirmDanger('Удалить кассу? Если по ней были операции, она уйдёт в архив.')) return;
    try {
      await post('/api/account/delete', { id: editingAccount });
      closeModal('account_modal');
      toast('Касса удалена');
      await refreshMoney();
      renderAll();
    } catch (e) { fail(e); }
  });

  btn('channel_save', async () => {
    const payload = {
      id: editingChannel || '', name: $('chf_name').value.trim(),
      fee_percent: num($('chf_fee').value), fee_fixed: num($('chf_fixed').value),
      ads_per_order: num($('chf_ads').value), payer: $('chf_payer').value,
      active: $('chf_active').checked ? 1 : 0,
    };
    if (!payload.name) return fail(new Error('Укажите название канала'));
    try {
      await post('/api/channel/save', payload);
      closeModal('channel_modal');
      toast('Канал сохранён', payload.name);
      await refreshMoney();
      renderAll();
    } catch (e) { fail(e); }
  });
  btn('channel_delete', async () => {
    if (!editingChannel || !confirmDanger('Удалить канал продаж?')) return;
    try {
      await post('/api/channel/delete', { id: editingChannel });
      closeModal('channel_modal');
      toast('Канал удалён');
      await refreshMoney();
      renderAll();
    } catch (e) { fail(e); }
  });

  btn('fixed_save', async () => {
    const payload = {
      id: editingFixed || '', name: $('ff_name').value.trim(), amount: num($('ff_amount').value),
      period: $('ff_period').value, day: clamp(num($('ff_day').value, 1), 1, 28),
      category: $('ff_category').value, account_id: $('ff_account').value,
      note: $('ff_note').value.trim(), active: $('ff_active').checked ? 1 : 0,
      deductible: $('ff_deductible').checked ? 1 : 0,
    };
    if (!payload.name) return fail(new Error('Укажите название расхода'));
    if (payload.amount <= 0) return fail(new Error('Сумма должна быть больше нуля'));
    try {
      await post('/api/fixed-cost/save', payload);
      closeModal('fixed_modal');
      toast('Постоянный расход сохранён', `${payload.name} · ${money(payload.amount)}`);
      await refreshMoney();
      renderAll();
      PF.refreshFinance();
    } catch (e) { fail(e); }
  });
  btn('fixed_delete', async () => {
    if (!editingFixed || !confirmDanger('Удалить постоянный расход? Уже созданные проводки останутся.')) return;
    try {
      await post('/api/fixed-cost/delete', { id: editingFixed });
      closeModal('fixed_modal');
      toast('Расход удалён');
      await refreshMoney();
      renderAll();
    } catch (e) { fail(e); }
  });

  btn('expcat_save', async () => {
    const payload = {
      id: editingCat || '', name: $('ecf_name').value.trim(), grp: $('ecf_grp').value,
      is_fixed: $('ecf_is_fixed').checked ? 1 : 0,
    };
    if (!payload.name) return fail(new Error('Укажите название статьи'));
    try {
      await post('/api/expense-category/save', payload);
      closeModal('expcat_modal');
      toast('Статья сохранена', payload.name);
      await refreshMoney();
      renderAll();
    } catch (e) { fail(e); }
  });
  btn('expcat_delete', async () => {
    if (!editingCat || !confirmDanger('Удалить статью расходов?')) return;
    try {
      await post('/api/expense-category/delete', { id: editingCat });
      closeModal('expcat_modal');
      toast('Статья удалена');
      await refreshMoney();
      renderAll();
    } catch (e) { fail(e); }
  });

  btn('payment_save', async () => {
    const kind = $('pf_kind').value || 'payment';
    const amount = num($('pf_amount').value);
    const limit = kind === 'refund' ? payingPaid : payingDebt;
    if (amount <= 0) return fail(new Error('Укажите сумму больше нуля'));
    if (amount > limit + 0.005) return fail(new Error(
      `${kind === 'refund' ? 'Можно вернуть' : 'Осталось получить'} только ${money(limit)}`));
    if (!$('pf_method').value) return fail(new Error('Выберите способ оплаты'));
    if (kind === 'refund' && !confirmDanger(`Подтвердить возврат ${money(amount)} по этому заказу?`)) return;
    try {
      const result = await post('/api/payment/save', {
        order_id: payingOrder, amount, kind,
        account_id: $('pf_account').value, method: $('pf_method').value,
        note: $('pf_note').value.trim(), request_id: paymentRequestId,
        expected_updated_at: payingOrderUpdatedAt,
      });
      const payment = result.payment || {};
      closeModal('payment_modal');
      toast(payment.already_recorded ? 'Операция уже была записана'
        : kind === 'refund' ? 'Возврат записан' : kind === 'prepay' ? 'Предоплата записана' : 'Оплата записана',
        money(payment.amount || amount));
      await Promise.all([refreshMoney(), PF.refreshCore()]);
      renderAll();
      PF.refreshFinance();
    } catch (e) { fail(e); }
  });

  const paymentKind = $('pf_kind');
  if (paymentKind) paymentKind.addEventListener('change', syncPaymentKind);

  btn('debt_reminder_copy', async () => {
    if (!reminderDraft || !reminderDraft.text) return;
    try {
      await navigator.clipboard.writeText(reminderDraft.text);
      toast('Текст скопирован', 'PrintFlow ничего не отправлял автоматически');
    } catch (e) { fail(new Error('Не удалось скопировать текст')); }
  });
  btn('debt_reminder_sent', async () => {
    if (!reminderDraft) return;
    let force = false;
    if (num(reminderDraft.cooldown_left_days) > 0) {
      force = confirmDanger(`Напоминание уже отмечали недавно. Отметить повторную отправку сейчас?`);
      if (!force) return;
    }
    try {
      const result = await post('/api/debt/remind/confirm', {
        id: reminderDraft.order_id, sent_confirmed: true, force,
      });
      reminderDraft = result;
      closeModal('debt_reminder_modal');
      toast('Отправка отмечена', `Заказ №${result.number} · PrintFlow сам сообщение не отправлял`);
      await refreshMoney();
      renderAll();
    } catch (e) { fail(e); }
  });

  document.addEventListener('click', (e) => {
    const acc = e.target.closest('[data-acc-edit]');
    if (acc) return openAccount(acc.dataset.accEdit);
    const ch = e.target.closest('[data-ch-edit]');
    if (ch) return openChannel(ch.dataset.chEdit);
    const fx = e.target.closest('[data-fix-edit]');
    if (fx) return openFixed(fx.dataset.fixEdit);
    const ct = e.target.closest('[data-cat2-edit]');
    if (ct) return openExpCat(ct.dataset.cat2Edit);
    const pay = e.target.closest('[data-pay-order]');
    if (pay) return openPayment(pay.dataset.payOrder);
    const remind = e.target.closest('[data-remind-order]');
    if (remind) return remindDebt(remind.dataset.remindOrder);
    return undefined;
  });

async function remindDebt(orderId) {
  try {
    const result = await post('/api/debt/remind', { id: orderId });
    reminderDraft = result;
    $('debt_reminder_title').textContent = `Напоминание · заказ №${result.number}`;
    $('debt_reminder_text').value = result.text || '';
    const cooldown = num(result.cooldown_left_days);
    $('debt_reminder_info').className = `verdict ${cooldown ? 'warn' : 'ok'}`;
    $('debt_reminder_info').innerHTML = `<b>Остаток ${money(result.debt)}</b>`
      + `<br>${esc(result.customer || 'Клиент')}`
      + `${result.messenger ? ` · ${esc(result.messenger)}` : result.phone ? ` · ${esc(result.phone)}` : ''}`
      + (cooldown ? `<br>⚠ Отправку уже отмечали недавно. Рекомендуемый повтор через ${nfmt(cooldown)} дн.` : '');
    openModal('debt_reminder_modal');
  } catch (e) { fail(e); }
}
}

function renderAll() {
  renderPnl();
  renderTax();
  renderCash();
  renderDirectories();
}

/* ======================================================== ABC-анализ */
let abcDays = 30;
async function loadAbc() {
  try {
    const data = await get('/api/abc', { days: abcDays });
    const host = $('abc_body');
    if (!host) return;
    const items = data.items || [];
    if (!items.length) {
      host.innerHTML = '<div class="empty compact"><span>Заказов за период ещё нет.</span></div>';
      return;
    }
    const CLS = { A: ['ok', 'A — ядро'], B: ['warn', 'B — поддержка'], C: ['', 'C — хвост'] };
    host.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>`
      + `<th>Класс</th><th>Изделие</th><th class="right">Штук</th><th class="right">Выручка</th>`
      + `<th class="right">Прибыль</th><th class="right">Доля</th></tr></thead><tbody>`
      + items.map((i) => `<tr><td><span class="chip ${CLS[i.cls][0]}">${esc(i.cls)}</span>`
        + `<small class="muted">${esc(CLS[i.cls][1])}</small></td>`
        + `<td><b>${esc(i.name)}</b><small class="muted">${i.orders} заказ(ов)</small></td>`
        + `<td class="right tnum">${nfmt(i.qty)}</td>`
        + `<td class="right tnum">${money(i.revenue)}</td>`
        + `<td class="right tnum ${num(i.profit) >= 0 ? 'pos' : 'neg'}">${money(i.profit)}</td>`
        + `<td class="right tnum">${pct(i.share)}</td></tr>`).join('')
      + `</tbody></table></div>`;
  } catch (e) { /* офлайн */ }
}

/* ================================================================ старт */
PF.on('ready', () => {
  bind();
  refreshMoney().then(() => { renderAll(); refreshReport(); });
  loadAbc();
  const days = $('abc_days');
  if (days) days.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-abc]');
    if (!btn) return;
    abcDays = +btn.dataset.abc;
    $$('#abc_days button').forEach((b) => b.classList.toggle('on', b === btn));
    loadAbc();
  });
});
PF.on('money', renderAll);
PF.on('finance', () => { if (PF.state.money) renderAll(); });
PF.on('view', (d) => {
  if (d.view === 'finance' || d.view === 'settings') refreshMoney();
});

PF.modules.finance = {
  refreshMoney, renderAll, openAccount, openChannel, openFixed, openExpCat,
  openPayment, fillSelects, MODE_HINTS,
};
})();
