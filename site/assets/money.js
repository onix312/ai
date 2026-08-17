/* PrintFlow 2.0 — деньги и материалы: финансовый отчёт, склад пластика,
   база изделий и калькулятор себестоимости. Расчёты — на сервере. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, clamp, money, nfmt, pct, hoursText, dateText,
  dateTimeText, debounce, toast, fail, openModal, closeModal, confirmDanger,
  drawChart, legend, store, catName } = U;
const { get, post } = PF.api;

let editingSpool = null, editingCatalog = null;

/* ============================================================ финансы */
function kpi(label, value, sub, kind, extra) {
  return `<div class="kpi ${kind || ''}"><span class="label">${esc(label)}</span>`
    + `<b class="value">${value}</b><span class="sub">${sub || ''}</span>${extra || ''}</div>`;
}

function renderFinance() {
  const data = PF.state.finance;
  if (!data) return;
  const s = data.summary || {};
  const target = num(PF.state.settings.target_profit_per_hour, 250);
  const perHour = num(s.profit_per_print_hour);
  $('fin_kpis').innerHTML = [
    kpi('Доход', money(s.income), `за ${s.period_days} дн.`, 'ok'),
    kpi('Расход', money(s.expense), 'пластик, энергия, закупки'),
    kpi('Прибыль', money(s.profit), `маржа ${pct(s.margin)}`, num(s.profit) >= 0 ? 'ok' : 'bad'),
    kpi('Прибыль за час печати', money(perHour), `норма от ${money(target)}`,
      perHour >= target ? 'ok' : perHour > 0 ? 'warn' : ''),
    kpi('Часы печати', nfmt(s.print_hours, 1), `${nfmt(s.jobs_done)} завершено · ${nfmt(s.jobs_failed)} брак`),
    kpi('Брак', pct(s.failure_rate), 'доля неудачных печатей', num(s.failure_rate) > 10 ? 'warn' : ''),
    kpi('Ждём оплату', money(s.pipeline), `${nfmt(s.active_orders)} активных заказов`),
    kpi('Склад пластика', nfmt(s.stock_grams) + ' г', `на ${money(s.stock_value)}`),
  ].join('');

  const keys = [
    { key: 'income', label: 'Доход', color: 'var(--ok)', type: 'bar', fmt: (v) => money(v) },
    { key: 'expense', label: 'Расход', color: 'var(--bad)', type: 'bar', opacity: .6, fmt: (v) => money(v) },
    { key: 'profit', label: 'Прибыль', color: 'var(--accent)', type: 'line', fmt: (v) => money(v) },
  ];
  drawChart($('fin_chart'), $('fin_tip'), data.series || [], keys, { height: 230, fmtAxis: (v) => nfmt(v) });
  legend($('fin_legend'), keys);

  const niches = (data.niches || []).slice().sort((a, b) => num(b.profit) - num(a.profit));
  const maxProfit = Math.max(1, ...niches.map((n) => Math.abs(num(n.profit))));
  $('fin_niches').innerHTML = niches.length ? niches.map((n) => `<div style="margin-bottom:12px">`
    + `<div class="res-row" style="border:0;padding:0 0 4px"><span class="lbl">${esc(n.icon || '◆')} ${esc(n.name)}</span>`
    + `<span class="val ${num(n.profit) >= 0 ? 'pos' : 'neg'}">${money(n.profit)}</span></div>`
    + `<div class="bar ${num(n.profit) >= 0 ? 'ok' : 'bad'}"><i style="width:${clamp(Math.abs(num(n.profit)) / maxProfit * 100, 2, 100)}%"></i></div>`
    + `<small class="muted">${nfmt(n.orders)} заказ(ов) · ${hoursText(n.hours)} · ${num(n.hours) ? money(n.profit_per_hour) + '/ч' : 'без печати'}</small></div>`).join('')
    : '<div class="empty compact"><span>Данных по нишам пока нет.</span></div>';

  const tx = data.transactions || [];
  $('fin_tx').innerHTML = tx.length ? tx.slice(0, 30).map((t) => `<div class="tx-row">`
    + `<span class="tx-ic ${esc(t.kind)}">${t.kind === 'income' ? '↑' : '↓'}</span>`
    + `<div class="tx-body"><b>${esc(t.title || t.category)}</b>`
    + `<small>${esc(dateTimeText(t.at))} · ${esc(catName(t.category))}${num(t.auto) ? ' · ⚙ авто' : ''}</small></div>`
    + `<span class="amt ${t.kind === 'income' ? 'pos' : 'neg'}">${t.kind === 'income' ? '+' : '−'}${money(Math.abs(num(t.amount)))}</span>`
    + (num(t.auto) ? '' : `<button class="icon-btn sm danger" type="button" data-tx-del="${esc(t.id)}">×</button>`)
    + '</div>').join('')
    : '<div class="empty compact"><span>Проводок пока нет. Они появятся сами при закрытии заказов.</span></div>';
}

/* ============================================================== склад */
function renderStock() {
  const spools = PF.state.spools || [];
  const totalG = spools.reduce((a, s) => a + num(s.remaining_grams), 0);
  const value = spools.reduce((a, s) => a + num(s.value), 0);
  const low = spools.filter((s) => num(s.percent) < num(PF.state.settings.filament_low_threshold, 15));
  const materials = new Set(spools.map((s) => s.material)).size;
  $('stock_kpis').innerHTML = [
    kpi('Остаток пластика', nfmt(totalG) + ' г', `${spools.length} катушек`),
    kpi('Стоимость запаса', money(value), 'по цене закупки'),
    kpi('Заканчиваются', String(low.length), `порог ${nfmt(PF.state.settings.filament_low_threshold, 0)}%`, low.length ? 'warn' : 'ok'),
    kpi('Материалов', String(materials), 'разных типов'),
  ].join('');

  const tag = $('nav_stock_tag');
  tag.hidden = !low.length;
  tag.textContent = String(low.length);

  $('spool_grid').innerHTML = spools.length ? spools.map((s) => {
    const p = clamp(num(s.percent), 0, 100);
    const cls = p <= 0 ? 'empty-spool' : p < num(PF.state.settings.filament_low_threshold, 15) ? 'low' : '';
    return `<article class="spool ${cls}" data-spool="${esc(s.id)}">`
      + `<div class="reel" style="--spool:${esc(s.color_hex || '#4b5563')}">${Math.round(p)}%</div>`
      + `<div class="body"><b>${esc(s.material)} ${esc(s.color_name)}</b>`
      + `<small>${esc(s.brand || 'без бренда')}${s.ams_slot !== '' && s.ams_slot != null ? ` · AMS слот ${esc(String(s.ams_slot))}` : ''}</small>`
      + `<div class="nums"><em>${nfmt(s.remaining_grams)}</em><span class="muted">/ ${nfmt(s.total_grams)} г · ${money(s.value)}</span></div>`
      + `<div class="bar ${p < 15 ? 'warn' : 'ok'}"><i style="width:${p}%"></i></div>`
      + `<small class="muted" style="margin-top:5px">израсходовано ${nfmt(s.used_grams)} г</small>`
      + '<div class="acts">'
      + `<button class="btn sm" type="button" data-spool-restock="${esc(s.id)}">Пополнить</button>`
      + `<button class="btn sm" type="button" data-spool-consume="${esc(s.id)}">Списать</button>`
      + `<button class="icon-btn sm" type="button" data-spool-edit="${esc(s.id)}">✎</button>`
      + '</div></div></article>';
  }).join('') : '<div class="empty"><span class="big">◍</span><b>Склад пуст</b><span>Добавьте катушки, чтобы расход списывался автоматически.</span></div>';
}

/* ============================================================ каталог */
function renderCatalog() {
  const list = PF.state.catalog || [];
  $('cat_tbody').innerHTML = list.length ? list.map((c) => {
    const e = c.economics || {};
    return `<tr class="clickable" data-cat="${esc(c.id)}">`
      + `<td class="strong">${esc(c.name)}</td>`
      + `<td>${esc(c.material || 'PLA')}</td>`
      + `<td class="right tnum">${nfmt(c.grams)}</td>`
      + `<td class="right tnum">${nfmt(c.hours, 1)}</td>`
      + `<td class="right tnum">${nfmt(c.fit_per_plate)}</td>`
      + `<td class="right tnum">${money(c.price)}</td>`
      + `<td class="right tnum ${num(e.profit_per_hour) >= num(PF.state.settings.target_profit_per_hour, 250) ? 'pos' : ''}">${num(c.hours) ? money(e.profit_per_hour) : '—'}</td>`
      + `<td class="right"><button class="icon-btn sm" type="button" data-cat-edit="${esc(c.id)}">✎</button></td></tr>`;
  }).join('') : '<tr><td colspan="8"><div class="empty compact"><span>База изделий пуста. Добавьте позиции, чтобы быстро считать заказы.</span></div></td></tr>';

  const sel = $('calc_preset');
  const keep = sel.value;
  sel.innerHTML = '<option value="">— свой расчёт —</option>' + list
    .map((c) => `<option value="${esc(c.id)}">${esc(c.name)} · ${nfmt(c.grams)} г · ${nfmt(c.hours, 1)} ч</option>`).join('');
  if (keep) sel.value = keep;
}

/* ========================================================= калькулятор */
const CALC_KEY = 'pf_calc_v2';
function calcInputs() {
  return {
    grams: num($('calc_grams').value), hours: num($('calc_hours').value),
    qty: Math.max(1, num($('calc_qty').value, 1)), fit: Math.max(1, num($('calc_fit').value, 1)),
    minutes: num($('calc_minutes').value), spool_price: num($('calc_spool_price').value),
    spool_weight: num($('calc_spool_weight').value), markup: num($('calc_markup').value),
    fee: num($('calc_fee').value), fix: num($('calc_fix').value),
  };
}
async function runCalc() {
  const v = calcInputs();
  store.set(CALC_KEY, JSON.stringify(v));
  const plates = Math.ceil(v.qty / v.fit);
  // Печать партией: на плите греется и калибруется один раз, поэтому часы
  // считаются по числу запусков, а не по числу изделий.
  const totalGrams = v.grams * v.qty;
  const totalHours = v.hours * v.qty;
  $('calc_batch_sub').textContent = `${nfmt(v.qty)} шт · ${plates} запуск(ов) по ${Math.min(v.fit, v.qty)} шт · ${hoursText(totalHours)} · ${nfmt(totalGrams)} г`;

  let br;
  try {
    br = await post('/api/calc/cost', {
      grams: totalGrams, hours: totalHours, manual_minutes: v.minutes * v.qty,
      spool_price: v.spool_price, spool_weight: v.spool_weight,
    });
  } catch (e) {
    $('calc_rows').innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
    return;
  }
  // Строки, которые реально входят в итог. Своя работа показывается отдельно
  // и в сумму не попадает, пока в настройках не включено «считать её расходом».
  const rows = [
    ['Пластик', br.filament], ['Электричество', br.energy],
    ['Амортизация', br.amortization], ['Обслуживание', br.maintenance],
    ['Упаковка', br.packaging],
  ];
  if (num(br.delivery)) rows.push(['Доставка', br.delivery]);
  if (num(br.overhead)) rows.push(['Доля постоянных расходов', br.overhead]);
  if (br.labor_counted) {
    rows.push(['Ручная работа', br.labor]);
    if (num(br.design)) rows.push(['Моделирование', br.design]);
  }
  rows.push(['Резерв на брак', br.failure_reserve]);
  const extra = !br.labor_counted && num(br.labor)
    ? `<div class="res-row muted-row"><span class="lbl">Ваша работа ${nfmt(v.minutes * v.qty, 0)} мин `
      + '<i class="hint-i" title="В настройках выключено «Считать свою работу расходом» — эти деньги остаются вашей прибылью">?</i>'
      + `</span><span class="val">${money(br.labor, 2)} · вне себестоимости</span></div>`
    : '';
  $('calc_rows').innerHTML = rows.map(([l, val]) =>
    `<div class="res-row"><span class="lbl">${esc(l)}</span><span class="val">${money(val, 2)}</span></div>`).join('')
    + extra
    + `<div class="res-row total"><span class="lbl">Себестоимость партии</span><span class="val">${money(br.total, 2)}</span></div>`
    + `<div class="res-row"><span class="lbl">За штуку</span><span class="val">${money(br.total / v.qty, 2)}</span></div>`
    + `<div class="res-row muted-row"><span class="lbl">Расход электричества</span><span class="val">${nfmt(br.energy_kwh, 2)} кВт·ч</span></div>`;

  const unitCost = num(br.total) / v.qty;
  const feeK = v.fee < 100 ? 1 - v.fee / 100 : 1;
  const price = Math.ceil((unitCost * (1 + v.markup / 100) + v.fix) / feeK / 10) * 10;
  const feeAmt = price * v.fee / 100 + v.fix;
  const net = price - feeAmt;
  const profit = (net - unitCost) * v.qty;
  const perHour = totalHours ? profit / totalHours : 0;
  const target = num(PF.state.settings.target_profit_per_hour, 250);
  // налог считаем по выбранному режиму, а не по «запасной» ставке из настроек
  const taxAmt = PF.taxOf(price * v.qty, profit, 'person');
  const afterTax = profit - taxAmt;
  const taxMode = PF.taxLabel();
  const taxRow = taxMode
    ? `Прибыль после налога (${taxMode})`
    : 'Прибыль после налога (режим не выбран)';

  $('calc_price').textContent = money(price);
  $('calc_profit_rows').innerHTML = [
    ['Выручка за партию', money(price * v.qty)],
    ['Комиссия и издержки', feeAmt ? '−' + money(feeAmt * v.qty) : money(0)],
    ['Прибыль до налога', money(profit)],
    ['Налог с этой сделки', taxAmt ? '−' + money(taxAmt) : money(0)],
    [taxRow, money(afterTax)],
    ['Прибыль за час печати', totalHours ? money(perHour) : '—'],
    ['Минимальная разумная цена', money(Math.ceil((unitCost * 1.4 + v.fix) / feeK / 10) * 10)],
  ].map(([l, val]) => `<div class="res-row"><span class="lbl">${esc(l)}</span><span class="val">${val}</span></div>`).join('');

  const verdict = $('calc_verdict');
  if (!totalHours || !totalGrams) {
    verdict.className = 'verdict';
    verdict.textContent = 'Введите вес и время печати из слайсера.';
  } else if (perHour >= target) {
    verdict.className = 'verdict ok';
    verdict.innerHTML = `<b>Выгодно.</b> ${money(perHour)} чистыми за час печати при норме ${money(target)}. Можно брать и масштабировать.`;
  } else if (perHour >= target * 0.4) {
    verdict.className = 'verdict warn';
    verdict.innerHTML = `<b>Слабовато.</b> ${money(perHour)} за час против нормы ${money(target)}. Поднимите цену, уменьшите время печати или печатайте большей партией.`;
  } else {
    verdict.className = 'verdict bad';
    verdict.innerHTML = `<b>Невыгодно.</b> Всего ${money(perHour)} за час работы принтера. Поднимите цену, смените канал или откажитесь от заказа.`;
  }
}
const runCalcDebounced = debounce(runCalc, 320);

function restoreCalc() {
  let saved = {};
  try { saved = JSON.parse(store.get(CALC_KEY, '{}')) || {}; } catch (e) { saved = {}; }
  const map = {
    grams: 'calc_grams', hours: 'calc_hours', qty: 'calc_qty', fit: 'calc_fit',
    minutes: 'calc_minutes', spool_price: 'calc_spool_price', spool_weight: 'calc_spool_weight',
    markup: 'calc_markup', fee: 'calc_fee', fix: 'calc_fix',
  };
  Object.entries(map).forEach(([k, id]) => { if (saved[k] != null && saved[k] !== '') $(id).value = saved[k]; });
  if (!$('calc_spool_price').value) $('calc_spool_price').value = num(PF.state.settings.default_spool_price, 1600);
  if (!$('calc_spool_weight').value) $('calc_spool_weight').value = num(PF.state.settings.default_spool_weight, 1000);
}

/* ============================================================= диалоги */
function openSpool(id) {
  editingSpool = id || null;
  const s = id ? PF.state.spools.find((x) => x.id === id) : null;
  const d = s || {
    material: 'PLA', brand: '', color_name: '', color_hex: '#333333',
    total_grams: 1000, remaining_grams: 1000,
    price: num(PF.state.settings.default_spool_price, 1600), ams_slot: '', printer_id: '',
  };
  ['material', 'brand', 'color_name', 'color_hex', 'total_grams', 'remaining_grams', 'price', 'ams_slot']
    .forEach((k) => { $('sf_' + k).value = d[k] ?? ''; });
  $('sf_printer_id').innerHTML = '<option value="">Не закреплена</option>' + PF.state.printers
    .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  $('sf_printer_id').value = d.printer_id || '';
  $('spool_modal_title').textContent = id ? 'Катушка: ' + (d.material + ' ' + d.color_name).trim() : 'Новая катушка';
  $('spool_delete').hidden = !id;
  openModal('spool_modal');
}
function openCatalog(id) {
  editingCatalog = id || null;
  const c = id ? PF.state.catalog.find((x) => x.id === id) : null;
  const d = c || { name: '', niche_id: '', material: 'PLA', grams: '', hours: '', fit_per_plate: 1, price: '', file: '', notes: '' };
  ['name', 'material', 'grams', 'hours', 'fit_per_plate', 'price', 'file', 'notes']
    .forEach((k) => { $('cf_' + k).value = d[k] ?? ''; });
  PF.modules.ops && PF.modules.ops.fillSelectors();
  $('cf_niche_id').value = d.niche_id || '';
  $('catalog_modal_title').textContent = id ? 'Позиция: ' + d.name : 'Новая позиция';
  $('catalog_delete').hidden = !id;
  openModal('catalog_modal');
}
/** Показывает поля, относящиеся только к доходу (канал и плательщик). */
function syncTxKind() {
  const income = $('tf_kind').value === 'income';
  const cat = $('tf_category');
  if (cat && cat.dataset.kind !== (income ? 'income' : 'expense')) {
    const html = income ? cat.dataset.income : cat.dataset.expense;
    if (html) {
      cat.dataset.kind = income ? 'income' : 'expense';
      const keep = cat.value;
      cat.innerHTML = html;
      if (keep && [...cat.options].some((o) => o.value === keep)) cat.value = keep;
    }
  }
  $('tf_channel_wrap').hidden = !income;
  $('tf_payer_wrap').hidden = !income;
  $('tf_taxable').closest('.chk').hidden = !income;
  $('tf_deductible').closest('.chk').hidden = income;
}

function openTx(kind) {
  PF.modules.finance && PF.modules.finance.fillSelects();
  $('tx_modal_title').textContent = kind === 'income' ? 'Новый доход' : 'Новая проводка';
  $('tf_kind').value = kind === 'income' ? 'income' : 'expense';
  syncTxKind();
  const defCat = kind === 'income' ? 'sale' : 'filament';
  if ($('tf_category').querySelector(`[value="${defCat}"]`)) $('tf_category').value = defCat;
  $('tf_amount').value = '';
  $('tf_title').value = '';
  $('tf_note').value = '';
  $('tf_at').value = U.todayISO();
  $('tf_taxable').checked = true;
  $('tf_deductible').checked = true;
  $('tf_payer').value = 'person';
  const def = PF.state.settings.default_account || 'cash';
  if ($('tf_account').querySelector(`[value="${def}"]`)) $('tf_account').value = def;
  syncTxKind();
  openModal('tx_modal');
}

/* ============================================================= события */
function bind() {
  $('fin_period').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-days]');
    if (!btn) return;
    $$('#fin_period button').forEach((b) => b.classList.toggle('on', b === btn));
    await PF.refreshFinance(+btn.dataset.days);
  });
  $('fin_add').addEventListener('click', () => openTx());
  $('tf_kind').addEventListener('change', syncTxKind);
  $('tx_save').addEventListener('click', async () => {
    const amount = num($('tf_amount').value);
    if (amount <= 0) return fail(new Error('Укажите сумму больше нуля'));
    const income = $('tf_kind').value === 'income';
    const at = $('tf_at').value;
    try {
      await post('/api/transaction/save', {
        kind: $('tf_kind').value, category: $('tf_category').value, amount,
        title: $('tf_title').value.trim() || $('tf_category').value,
        note: $('tf_note').value.trim(),
        account_id: $('tf_account').value,
        at: at ? at + 'T12:00:00' : '',
        channel: income ? $('tf_channel').value : '',
        payer: income ? $('tf_payer').value : '',
        taxable: income ? ($('tf_taxable').checked ? 1 : 0) : 1,
        deductible: income ? 1 : ($('tf_deductible').checked ? 1 : 0),
      });
      closeModal('tx_modal');
      toast('Проводка добавлена', money(amount));
      PF.refreshFinance();
      PF.refreshMoney && PF.refreshMoney();
    } catch (e) { fail(e); }
  });

  $('spool_add').addEventListener('click', () => openSpool());
  $('spool_save').addEventListener('click', async () => {
    const payload = {
      id: editingSpool || '',
      material: $('sf_material').value.trim() || 'PLA',
      brand: $('sf_brand').value.trim(),
      color_name: $('sf_color_name').value.trim(),
      color_hex: $('sf_color_hex').value,
      total_grams: num($('sf_total_grams').value, 1000),
      remaining_grams: num($('sf_remaining_grams').value),
      price: num($('sf_price').value),
      ams_slot: $('sf_ams_slot').value.trim(),
      printer_id: $('sf_printer_id').value,
    };
    try {
      await post('/api/spool/save', payload);
      closeModal('spool_modal');
      toast('Катушка сохранена', `${payload.material} ${payload.color_name}`);
      await PF.refreshCore();
      PF.refreshFinance();
    } catch (e) { fail(e); }
  });
  $('spool_delete').addEventListener('click', async () => {
    if (!editingSpool || !confirmDanger('Удалить катушку со склада? История расхода сохранится.')) return;
    try {
      await post('/api/spool/delete', { id: editingSpool });
      closeModal('spool_modal');
      toast('Катушка удалена');
      PF.refreshCore();
    } catch (e) { fail(e); }
  });

  $('cat_add').addEventListener('click', () => openCatalog());
  $('catalog_save').addEventListener('click', async () => {
    const payload = {
      id: editingCatalog || '',
      name: $('cf_name').value.trim(),
      niche_id: $('cf_niche_id').value,
      material: $('cf_material').value.trim() || 'PLA',
      grams: num($('cf_grams').value),
      hours: num($('cf_hours').value),
      fit_per_plate: Math.max(1, num($('cf_fit_per_plate').value, 1)),
      price: num($('cf_price').value),
      file: $('cf_file').value.trim(),
      notes: $('cf_notes').value.trim(),
    };
    if (!payload.name) return fail(new Error('Укажите название позиции'));
    try {
      await post('/api/catalog/save', payload);
      closeModal('catalog_modal');
      toast('Позиция сохранена', payload.name);
      PF.refreshCore();
    } catch (e) { fail(e); }
  });
  $('catalog_delete').addEventListener('click', async () => {
    if (!editingCatalog || !confirmDanger('Удалить позицию из базы изделий?')) return;
    try {
      await post('/api/catalog/delete', { id: editingCatalog });
      closeModal('catalog_modal');
      toast('Позиция удалена');
      PF.refreshCore();
    } catch (e) { fail(e); }
  });

  document.addEventListener('click', async (e) => {
    const edit = e.target.closest('[data-spool-edit]');
    if (edit) return openSpool(edit.dataset.spoolEdit);
    const restock = e.target.closest('[data-spool-restock]');
    if (restock) {
      const grams = window.prompt('Сколько граммов добавить на катушку?', '1000');
      if (grams == null) return;
      const price = window.prompt('Цена закупки, ₽ (0 — не записывать расход):',
        String(num(PF.state.settings.default_spool_price, 1600)));
      if (price == null) return;
      try {
        await post('/api/spool/restock', { id: restock.dataset.spoolRestock, grams: num(grams), price: num(price) });
        toast('Катушка пополнена', nfmt(grams) + ' г');
        await PF.refreshCore();
        PF.refreshFinance();
      } catch (err) { fail(err); }
      return;
    }
    const consume = e.target.closest('[data-spool-consume]');
    if (consume) {
      const grams = window.prompt('Сколько граммов списать вручную?', '50');
      if (grams == null) return;
      try {
        await post('/api/spool/consume', { id: consume.dataset.spoolConsume, grams: num(grams), note: 'ручное списание' });
        toast('Списано', nfmt(grams) + ' г');
        PF.refreshCore();
      } catch (err) { fail(err); }
      return;
    }
    const catEdit = e.target.closest('[data-cat-edit]') || e.target.closest('[data-cat]');
    if (catEdit) {
      const id = catEdit.dataset.catEdit || catEdit.dataset.cat;
      if (id) openCatalog(id);
      return;
    }
    const txDel = e.target.closest('[data-tx-del]');
    if (txDel) {
      if (!confirmDanger('Удалить проводку?')) return;
      try {
        await post('/api/transaction/delete', { id: txDel.dataset.txDel });
        toast('Проводка удалена');
        PF.refreshFinance();
      } catch (err) { fail(err); }
    }
  });

  ['calc_grams', 'calc_hours', 'calc_qty', 'calc_fit', 'calc_minutes', 'calc_spool_price',
    'calc_spool_weight', 'calc_markup', 'calc_fee', 'calc_fix']
    .forEach((id) => $(id).addEventListener('input', runCalcDebounced));
  $('calc_preset').addEventListener('change', (e) => {
    const item = PF.state.catalog.find((c) => c.id === e.target.value);
    if (!item) return;
    $('calc_grams').value = item.grams;
    $('calc_hours').value = item.hours;
    $('calc_fit').value = item.fit_per_plate || 1;
    if (item.price) $('calc_markup').value = item.grams || item.hours ? $('calc_markup').value : 150;
    runCalc();
  });
  $('calc_from_cat').addEventListener('click', () => PF.go('inventory'));
  $('calc_to_order').addEventListener('click', () => {
    const v = calcInputs();
    const preset = PF.state.catalog.find((c) => c.id === $('calc_preset').value);
    PF.modules.ops.openOrder().then(() => {
      $('of_product').value = preset ? preset.name : '';
      $('of_grams').value = v.grams;
      $('of_hours').value = v.hours;
      $('of_qty').value = v.qty;
      $('of_manual_minutes').value = v.minutes;
      $('of_price').value = (($('calc_price').textContent || '').replace(/[^\d]/g, '') || '');
      if (preset && preset.niche_id) $('of_niche_id').value = preset.niche_id;
      if (preset && preset.file) $('of_file').value = preset.file;
      PF.modules.ops.openOrder && $('of_grams').dispatchEvent(new Event('input'));
    });
  });
}

/* =============================================================== старт */
PF.on('ready', () => { bind(); restoreCalc(); });
PF.on('data', () => { renderStock(); renderCatalog(); });
PF.on('finance', renderFinance);
PF.on('view', (d) => { if (d.view === 'calc') runCalc(); });

PF.modules.money = { openSpool, openCatalog, openTx, runCalc, renderFinance };
})();
