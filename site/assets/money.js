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
      + `<small class="muted" style="margin-top:5px">израсходовано ${nfmt(s.used_grams)} г`
      + (s.last_dry ? ` · сушка ${esc(dateText(s.last_dry))}`
        + (num(s.last_dry_temp) ? ` ${nfmt(s.last_dry_temp)}°` : '')
        + (num(s.last_dry_min) ? ` ${nfmt(s.last_dry_min)} мин` : '') : '')
      + '</small>'
      + '<div class="acts">'
      + `<button class="btn sm" type="button" data-spool-restock="${esc(s.id)}">Пополнить</button>`
      + `<button class="btn sm" type="button" data-spool-consume="${esc(s.id)}">Списать</button>`
      + `<button class="btn sm ghost" type="button" data-spool-dry="${esc(s.id)}" title="Записать сушку">☀</button>`
      + `<button class="btn sm ghost" type="button" data-spool-qr="${esc(s.id)}" title="QR для наклейки">◫</button>`
      + `<button class="icon-btn sm" type="button" data-spool-edit="${esc(s.id)}">✎</button>`
      + '</div></div></article>';
  }).join('') : '<div class="empty"><span class="big">◍</span><b>Склад пуст</b><span>Добавьте катушки, чтобы расход списывался автоматически.</span></div>';
}

/* ============================================== статистика расхода */
async function loadFilamentStats() {
  try {
    const data = await get('/api/filament-stats', { days: 30 });
    const row = (d) => `<div class="tx-row"><div class="tx-body"><b>${esc(d.material || '—')}</b>`
      + `<small>${nfmt(d.uses)} списаний · ${money(d.cost)}</small></div>`
      + `<span class="amt">${nfmt(d.grams)} г</span></div>`;
    const mat = $('filament_by_mat');
    if (mat) mat.innerHTML = (data.by_material || []).length
      ? (data.by_material || []).slice(0, 8).map(row).join('')
      : '<div class="empty compact"><span>Расхода ещё не было.</span></div>';
    const col = $('filament_by_color');
    if (col) col.innerHTML = (data.by_color || []).length
      ? (data.by_color || []).slice(0, 8).map((d) => `<div class="tx-row"><div class="tx-body"><b>${esc(d.color)}</b>`
        + `<small>${esc(d.material || '—')} · ${nfmt(d.uses)} списаний · ${money(d.cost)}</small></div>`
        + `<span class="amt">${nfmt(d.grams)} г</span></div>`).join('')
      : '<div class="empty compact"><span>Расхода ещё не было.</span></div>';
  } catch (e) { /* офлайн */ }
}

/* ================================================== QR катушки и сушка */
async function openSpoolQr(spoolId) {
  const spool = (PF.state.spools || []).find((x) => x.id === spoolId);
  if (!spool) return;
  let url = '';
  let reachable = true;
  let source = '';
  try {
    const res = await get('/api/spool/qr-link', { id: spoolId });
    url = res.url || '';
    reachable = res.reachable !== false;
    source = res.source || '';
  } catch (e) {
    // офлайн: не подставляем localhost — телефон его не откроет
    url = '';
  }
  const code = $('spool_qr_code');
  code.innerHTML = (url && window.QR && window.QR.svg)
    ? window.QR.svg(url, { size: 240, dark: '#111827', light: '#ffffff' })
    : '<div class="empty compact"><span>QR недоступен</span></div>';
  const warn = !reachable || !url
    ? '<div class="notice warn" style="margin-top:10px"><span>⚠</span><span>'
      + 'Ссылка для телефона не собралась: нет LAN-адреса. '
      + 'Запустите PrintFlow с доступом по сети (python pf.py) и укажите IP в '
      + 'Настройки → Система → Адрес для QR, например http://192.168.1.50:8080</span></div>'
    : (source === 'lan'
      ? '<small class="muted" style="display:block;margin-top:6px">Телефон должен быть в той же Wi-Fi сети.</small>'
      : '');
  $('spool_qr_info').innerHTML = `<b>${esc(spool.material)} ${esc(spool.color_name)}</b>`
    + (url ? `<small class="muted" style="display:block">${esc(url)}</small>` : '')
    + `<small class="muted">Наклейте на катушку. При установке в AMS отсканируйте — слот привяжется сам.</small>`
    + warn;
  openModal('spool_qr_modal');
}
async function spoolDry(spoolId) {
  const spool = (PF.state.spools || []).find((x) => x.id === spoolId);
  if (!spool) return;
  const minutes = window.prompt('Сколько минут сушить?', '240');
  if (minutes == null) return;
  const temp = window.prompt('Температура сушки, °C (PLA 50, PETG 65, TPU 55)', '55');
  try {
    await post('/api/spool/dry', { id: spoolId, minutes: num(minutes), temp: num(temp || 0) });
    toast('Сушка записана', `${spool.material} ${spool.color_name} · ${minutes} мин`);
  } catch (e) { fail(e); }
}

/* ===================================================== история цен */
async function openPriceHistory(product) {
  if (!product) return fail(new Error('Сначала сохраните позицию'));
  try {
    const data = await get('/api/price-history', { product });
    const rows = data.history || [];
    $('price_body').innerHTML = rows.length ? rows.map((r) => `<div class="tx-row">`
      + `<span class="tx-ic income">₽</span>`
      + `<div class="tx-body"><b>${money(r.price)}</b><small>${esc(dateTimeText(r.at))}</small></div>`
      + `</div>`).join('') : '<div class="empty compact"><span>Истории цен пока нет — она пишется при сохранении заказов.</span></div>';
    openModal('price_modal');
  } catch (e) { fail(e); }
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
const CALC_KEY = 'pf_calc_v3';
let calcMaterials = [], calcProfiles = [];

async function loadCalcMaterials() {
  try {
    const data = await get('/api/calc/materials');
    calcMaterials = data.materials || [];
    calcProfiles = data.profiles || [];
    const sel = $('calc_material');
    if (sel && calcMaterials.length) {
      const keep = sel.value;
      sel.innerHTML = calcMaterials.map((m) =>
        `<option value="${esc(m.key)}">${esc(m.name)} · ${nfmt(m.price_per_kg)} ₽/кг</option>`).join('');
      if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
    }
  } catch (e) { /* офлайн — используем дефолт */ }
}

function calcInputs() {
  const prepStages = {
    find: num($('calc_prep_find') ? $('calc_prep_find').value : 0),
    orient: num($('calc_prep_orient') ? $('calc_prep_orient').value : 0),
    duplicate: num($('calc_prep_dupe') ? $('calc_prep_dupe').value : 0),
    profile: num($('calc_prep_profile') ? $('calc_prep_profile').value : 0),
    supports: num($('calc_prep_supports') ? $('calc_prep_supports').value : 0),
    slice: num($('calc_prep_slice') ? $('calc_prep_slice').value : 0),
  };
  const modelPrep = Object.values(prepStages).reduce((a, b) => a + b, 0);
  return {
    plate_grams: num($('calc_plate_grams').value),
    plate_hours: num($('calc_plate_hours').value),
    qty: Math.max(1, num($('calc_qty').value, 1)),
    fit: Math.max(1, num($('calc_fit').value, 1)),
    warmup: num($('calc_warmup').value),
    material: $('calc_material').value || 'PLA',
    quality: $('calc_quality').value || 'standard',
    supports_pct: num($('calc_supports').value),
    color_swaps: num($('calc_color_swaps').value),
    spool_price: num($('calc_spool_price').value),
    spool_weight: num($('calc_spool_weight').value),
    remove: num($('calc_remove').value),
    sand: num($('calc_sand').value),
    paint: num($('calc_paint').value),
    design: num($('calc_design').value),
    markup: num($('calc_markup').value, 150),
    fee: num($('calc_fee').value),
    fix: num($('calc_fix').value),
    delivery: num($('calc_delivery').value),
    model_prep_minutes: modelPrep,
    prep_stages: prepStages,
    complexity: $('calc_complexity') ? $('calc_complexity').value : 'simple',
    dim_x: num($('calc_dim_x') ? $('calc_dim_x').value : 0),
    dim_y: num($('calc_dim_y') ? $('calc_dim_y').value : 0),
  };
}

function updateMaterialInfo() {
  const mat = calcMaterials.find((m) => m.key === ($('calc_material').value || 'PLA'));
  const info = $('calc_material_info');
  if (!info || !mat) return;
  info.innerHTML = `<span>💡 ${esc(mat.strengths)}<br>⚠ ${esc(mat.weaknesses)}<br>`
    + `<small class="muted">Теплостойкость ${mat.heat_resistance}°C · Скорость ×${mat.speed_factor} · `
    + `${mat.abrasive ? '⚠ абразивный — нужно закалённое сопло' : 'не абразивный'}</small></span>`;
}

async function runCalc() {
  const v = calcInputs();
  store.set(CALC_KEY, JSON.stringify(v));
  updateMaterialInfo();

  // Подсказка «вес/время на штуку»
  const plateInfo = $('calc_plate_info');
  if (v.plate_grams > 0 && v.fit > 0) {
    const perUnit = (v.plate_grams / v.fit).toFixed(1);
    const perUnitH = (v.plate_hours / v.fit * 60).toFixed(0);
    plateInfo.innerHTML = `<span>📐 На штуку: <b>${perUnit} г</b> · <b>${perUnitH} мин</b> принтерного времени `
      + `(${v.fit} шт делят плиту)</span>`;
    plateInfo.hidden = false;
  } else {
    plateInfo.hidden = true;
  }

  const plates = Math.ceil(v.qty / v.fit);
  const totalHours = v.plate_hours * plates + (v.warmup / 60) * plates;
  const totalGrams = v.plate_grams * plates;
  $('calc_batch_sub').textContent = `${nfmt(v.qty)} шт · ${plates} плит(а/ы) · `
    + `${hoursText(totalHours)} · ${nfmt(totalGrams)} г`;

  let br;
  try {
    br = await post('/api/calc/cost', {
      plate_grams: v.plate_grams, plate_hours: v.plate_hours,
      fit_per_plate: v.fit, qty: v.qty,
      warmup_minutes: v.warmup,
      material: v.material, quality: v.quality,
      supports_pct: v.supports_pct,
      color_swaps: v.color_swaps,
      spool_price: v.spool_price, spool_weight: v.spool_weight,
      remove_minutes: v.remove, sand_minutes: v.sand,
      paint_minutes: v.paint, design_minutes: v.design,
      delivery: v.delivery,
      model_prep_minutes: v.model_prep_minutes,
    });
  } catch (e) {
    $('calc_rows').innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
    return;
  }

  // Обновить подготовку модели
  updatePrepTotal(v);
  // Раскладка на плите
  updateLayout(v);

  const rows = [
    ['Пластик (включая поддержку и продувку)', br.filament],
    ['Электричество', br.energy],
    ['Амортизация принтера', br.amortization],
    ['Обслуживание', br.maintenance],
    ['Упаковка', br.packaging],
  ];
  if (num(br.model_prep_cost)) rows.push(['Подготовка модели (' + nfmt(br.model_prep, 0) + ' мин)', br.model_prep_cost]);
  if (num(br.delivery)) rows.push(['Доставка', br.delivery]);
  if (num(br.overhead)) rows.push(['Доля постоянных расходов', br.overhead]);
  if (br.labor_counted) {
    rows.push(['Ручная работа', br.labor]);
    if (num(br.design)) rows.push(['Моделирование', br.design]);
  }
  rows.push(['Резерв на брак', br.failure_reserve]);

  // Детальная разбивка
  const detailRows = [];
  if (num(br.support_grams)) detailRows.push(`Поддержки: ${nfmt(br.support_grams, 0)} г`);
  if (num(br.purge_grams)) detailRows.push(`Продувка AMS: ${nfmt(br.purge_grams, 0)} г`);
  if (br.plates > 1) detailRows.push(`${br.plates} плит × ${br.fit_per_plate} шт`);
  const detail = detailRows.length
    ? `<div class="res-row muted-row"><span class="lbl">${detailRows.join(' · ')}</span><span class="val"></span></div>` : '';

  const extra = !br.labor_counted && num(br.labor)
    ? `<div class="res-row muted-row"><span class="lbl">Ваша работа (снятие + обработка)`
      + '<i class="hint-i" title="Не считается расходом — остаётся вашей прибылью">?</i>'
      + `</span><span class="val">${money(br.labor, 2)} · вне себестоимости</span></div>` : '';

  $('calc_rows').innerHTML = rows.map(([l, val]) =>
    `<div class="res-row"><span class="lbl">${esc(l)}</span><span class="val">${money(val, 2)}</span></div>`).join('')
    + detail + extra
    + `<div class="res-row total"><span class="lbl">Себестоимость партии</span><span class="val">${money(br.total, 2)}</span></div>`
    + `<div class="res-row"><span class="lbl">За штуку</span><span class="val">${money(br.per_unit, 2)}</span></div>`
    + `<div class="res-row muted-row"><span class="lbl">Электричества</span><span class="val">${nfmt(br.energy_kwh, 2)} кВт·ч</span></div>`
    + `<div class="res-row muted-row"><span class="lbl">Материал: ${esc(br.material)} · ${esc(br.quality)}</span><span class="val"></span></div>`;

  const unitCost = br.per_unit;
  const feeK = v.fee < 100 ? 1 - v.fee / 100 : 1;
  const price = Math.ceil((unitCost * (1 + v.markup / 100) + v.fix) / feeK / 10) * 10;
  const feeAmt = price * v.fee / 100 + v.fix;
  const net = price - feeAmt;
  const profit = (net - unitCost) * v.qty;
  const totalH = br.total_hours;
  const perHour = totalH ? profit / totalH : 0;
  const target = num(PF.state.settings.target_profit_per_hour, 250);
  const taxAmt = PF.taxOf(price * v.qty, profit, 'person');
  const afterTax = profit - taxAmt;
  const taxMode = PF.taxLabel();
  const taxRow = taxMode ? `Прибыль после налога (${taxMode})` : 'Прибыль после налога';

  $('calc_price').textContent = money(price);
  $('calc_profit_rows').innerHTML = [
    ['Выручка за партию', money(price * v.qty)],
    ['Комиссия и издержки', feeAmt ? '−' + money(feeAmt * v.qty) : money(0)],
    ['Прибыль до налога', money(profit)],
    ['Налог с этой сделки', taxAmt ? '−' + money(taxAmt) : money(0)],
    [taxRow, money(afterTax)],
    ['Прибыль за час печати', totalH ? money(perHour) : '—'],
    ['Минимальная разумная цена', money(Math.ceil((unitCost * 1.4 + v.fix) / feeK / 10) * 10)],
  ].map(([l, val]) => `<div class="res-row"><span class="lbl">${esc(l)}</span><span class="val">${val}</span></div>`).join('');

  const verdict = $('calc_verdict');
  if (!v.plate_grams || !v.plate_hours) {
    verdict.className = 'verdict';
    verdict.textContent = 'Введите вес и время плиты из слайсера.';
  } else if (perHour >= target) {
    verdict.className = 'verdict ok';
    verdict.innerHTML = `<b>✅ Выгодно.</b> ${money(perHour)} чистыми за час при норме ${money(target)}. `
      + `${br.plates > 1 ? `Партия ${v.qty} шт на ${br.plates} плитах — эффективно.` : ''}`;
  } else if (perHour >= target * 0.4) {
    verdict.className = 'verdict warn';
    const tips = [];
    if (v.fit < 4) tips.push('увеличьте число штук на плите');
    if (v.quality === 'detail') tips.push('переключитесь на «Стандарт»');
    if (v.supports_pct > 20) tips.push('уменьшите поддержки (поверните модель)');
    if (v.qty < 5) tips.push('печатайте большей партией');
    verdict.innerHTML = `<b>⚠ Слабовато.</b> ${money(perHour)} за час против нормы ${money(target)}. `
      + (tips.length ? `Советы: ${tips.join('; ')}.` : 'Поднимите цену или уменьшите время.');
  } else {
    verdict.className = 'verdict bad';
    verdict.innerHTML = `<b>❌ Невыгодно.</b> Всего ${money(perHour)} за час. `
      + `Поднимите цену, смените канал, печатайте большей партией или откажитесь от заказа.`;
  }

  // Кнопка "минимальная партия" и "сценарии" используют последние данные
  runMinBatch(v);
  runPayback(v, profit, v.qty);
}

// ------------------------------------------------------- минимальная партия
async function runMinBatch(v) {
  if (!v.plate_grams || !v.plate_hours) { $('calc_min_batch').innerHTML = ''; return; }
  try {
    const data = await post('/api/calc/min-batch', {
      plate_grams: v.plate_grams, plate_hours: v.plate_hours,
      fit_per_plate: v.fit, material: v.material, quality: v.quality,
      supports_pct: v.supports_pct, target_per_hour: num(PF.state.settings.target_profit_per_hour, 250),
      spool_price: v.spool_price, markup: v.markup,
    });
    const target = num(PF.state.settings.target_profit_per_hour, 250);
    const rows = (data.table || []).filter((r) => r.qty <= 20);
    if (!rows.length) { $('calc_min_batch').innerHTML = ''; return; }
    const maxPPH = Math.max(1, ...rows.map((r) => Math.abs(r.profit_per_hour)));
    $('calc_min_batch').innerHTML = (data.min_qty
      ? `<div class="notice ok"><span>Минимальная рентабельная партия: <b>${data.min_qty} шт</b> `
        + `(${data.min_plates} плит(а/ы)) для нормы ${money(target)}/ч</span></div>` : '')
      + '<div class="batch-bars">' + rows.map((r) => {
        const pct = clamp(Math.abs(r.profit_per_hour) / maxPPH * 100, 2, 100);
        const cls = r.ok ? 'ok' : 'warn';
        return `<div class="batch-row"><span class="lbl">${r.qty} шт (${r.plates} пл.)</span>`
          + `<div class="bar ${cls}"><i style="width:${pct}%"></i></div>`
          + `<span class="val">${money(r.profit_per_hour)}/ч · ${money(r.cost_unit)}/шт</span></div>`;
      }).join('') + '</div>';
  } catch (e) { /* тихо */ }
}

// ----------------------------------------------------- подготовка модели
function updatePrepTotal(v) {
  const el = $('calc_prep_total');
  if (!el) return;
  const total = v.model_prep_minutes;
  const perUnit = v.qty > 0 ? (total / v.qty).toFixed(1) : '—';
  el.innerHTML = `<span>⏱ Подготовка: <b>${nfmt(total, 0)} мин</b> на всю партию`
    + ` (${perUnit} мин/шт) · Сложность: ${v.complexity}</span>`;
}

const PREP_DEFAULTS = {
  simple:  { find: 5,  orient: 2,  duplicate: 2, profile: 2,  supports: 2,  slice: 3 },
  medium:  { find: 10, orient: 5,  duplicate: 3, profile: 5,  supports: 10, slice: 5 },
  complex: { find: 20, orient: 10, duplicate: 5, profile: 10, supports: 20, slice: 10 },
};

// Автооценка по сложности
function autoPrepEstimate() {
  const complexity = $('calc_complexity') ? $('calc_complexity').value : 'simple';
  const defaults = PREP_DEFAULTS[complexity] || PREP_DEFAULTS.simple;
  Object.entries(defaults).forEach(([key, val]) => {
    const el = $('calc_prep_' + (key === 'duplicate' ? 'dupe' : key));
    if (el) el.value = val;
  });
  runCalc();
}

// --------------------------------------------------------- таймер подготовки
let prepTimerStart = null;
function togglePrepTimer() {
  const btn = $('calc_prep_timer');
  if (!btn) return;
  if (!prepTimerStart) {
    prepTimerStart = Date.now();
    btn.textContent = '⏱ Остановить';
    btn.classList.add('primary');
    toast('Таймер запущен', 'Засекаем время подготовки модели');
  } else {
    const elapsed = Math.round((Date.now() - prepTimerStart) / 60000 * 10) / 10;
    prepTimerStart = null;
    btn.textContent = '⏱ Таймер';
    btn.classList.remove('primary');
    // Распределяем время по этапам пропорционально дефолтам
    const complexity = $('calc_complexity') ? $('calc_complexity').value : 'simple';
    const defaults = PREP_DEFAULTS[complexity] || PREP_DEFAULTS.simple;
    const totalDefault = Object.values(defaults).reduce((a, b) => a + b, 0);
    const ratio = elapsed / totalDefault;
    Object.entries(defaults).forEach(([key, val]) => {
      const el = $('calc_prep_' + (key === 'duplicate' ? 'dupe' : key));
      if (el) el.value = Math.round(val * ratio * 10) / 10;
    });
    toast('Таймер остановлен', `${nfmt(elapsed, 1)} мин — распределено по этапам`);
    runCalc();
  }
}

// ------------------------------------------------------ раскладка на плите
async function updateLayout(v) {
  const el = $('calc_layout_result');
  if (!el || !v.dim_x || !v.dim_y) { if (el) el.innerHTML = ''; return; }
  try {
    const data = await get('/api/calc/plate-layout', { dim_x: v.dim_x, dim_y: v.dim_y });
    el.innerHTML = `<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">`
      + `<div style="flex:0 0 200px">${data.svg}</div>`
      + `<div><b>${data.fit_per_plate} шт</b> влезает на плиту<br>`
      + `<small class="muted">${data.cols}×${data.rows} · ${data.rotated ? 'повёрнуты' : 'прямо'} · `
      + `заполнение ${nfmt(data.utilization_pct, 0)}%</small><br>`
      + `<button class="btn sm" type="button" onclick="document.getElementById('calc_fit').value=${data.fit_per_plate};runCalc();">`
      + `Подставить в калькулятор</button></div></div>`;
  } catch (e) { /* тихо */ }
}

// ------------------------------------------------------------ сценарии
async function runScenarios(variants, title) {
  const v = calcInputs();
  const base = {
    plate_grams: v.plate_grams, plate_hours: v.plate_hours,
    fit_per_plate: v.fit, qty: v.qty,
    warmup_minutes: v.warmup,
    supports_pct: v.supports_pct,
    spool_price: v.spool_price, spool_weight: v.spool_weight,
    remove_minutes: v.remove, sand_minutes: v.sand,
    paint_minutes: v.paint, design_minutes: v.design,
    delivery: v.delivery,
    markup: v.markup,
  };
  try {
    const data = await post('/api/calc/scenarios', { base, variants });
    const scenarios = data.scenarios || [];
    if (!scenarios.length) return;
    const maxProfit = Math.max(1, ...scenarios.map((s) => Math.abs(s.profit_per_hour)));
    const target = num(PF.state.settings.target_profit_per_hour, 250);
    $('calc_scenarios').innerHTML = `<h3 style="margin:0 0 8px">${esc(title || 'Сравнение')}</h3>`
      + scenarios.map((s) => {
        const pct = clamp(Math.abs(s.profit_per_hour) / maxProfit * 100, 2, 100);
        const cls = s.profit_per_hour >= target ? 'ok' : s.profit_per_hour >= target * 0.4 ? 'warn' : 'bad';
        return `<div class="scenario-row"><div class="scenario-head">`
          + `<b>${esc(s.label)}</b>`
          + `<span class="${cls === 'ok' ? 'pos' : cls === 'bad' ? 'neg' : ''}">${money(s.profit_per_hour)}/ч</span>`
          + `</div><div class="bar ${cls}"><i style="width:${pct}%"></i></div>`
          + `<small class="muted">${money(s.breakdown.per_unit)}/шт себестоимость · `
          + `${money(s.price.price)} цена · ${nfmt(s.breakdown.total_hours, 1)} ч · `
          + `маржа ${nfmt(s.margin, 0)}% · прибыль ${money(s.profit)}</small></div>`;
      }).join('');
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------- окупаемость
async function runPayback(v, profit, qty) {
  const modelCost = num($('calc_model_cost').value);
  const designHours = num($('calc_design_hours').value);
  const salesWeek = num($('calc_sales_week').value, 3);
  const profitPerUnit = qty > 0 ? profit / qty : 0;
  if (modelCost <= 0 && designHours <= 0) {
    $('calc_payback').innerHTML = '<small class="muted">Укажите стоимость модели или часы моделирования</small>';
    return;
  }
  try {
    const data = await post('/api/calc/payback', {
      model_cost: modelCost, design_hours: designHours,
      profit_per_unit: profitPerUnit, sales_per_week: salesWeek,
    });
    if (data.total_invest <= 0) {
      $('calc_payback').innerHTML = '';
      return;
    }
    $('calc_payback').innerHTML = `<div class="notice ${data.weeks_to_payback <= 4 ? 'ok' : data.weeks_to_payback <= 12 ? 'warn' : 'bad'}">`
      + `<span>Вложения: <b>${money(data.total_invest)}</b>`
      + (data.model_cost ? ` (модель ${money(data.model_cost)})` : '')
      + (data.design_cost ? ` + моделирование ${money(data.design_cost)}` : '')
      + `<br>Прибыль со штуки: ${money(data.profit_per_unit)}<br>`
      + `Окупится за <b>${data.units_needed} продаж</b>`
      + ` (${salesWeek}/нед → <b>${nfmt(data.weeks_to_payback, 1)} нед</b> · `
      + `${nfmt(data.days_to_payback, 0)} дн.)</span></div>`;
  } catch (e) { /* тихо */ }
}

// ------------------------------------------------------- реальная статистика
async function showRealStats() {
  const preset = ($('calc_preset').value || '');
  const item = PF.state.catalog.find((c) => c.id === preset);
  const product = item ? item.name : '';
  const material = $('calc_material').value || '';
  try {
    const data = await get('/api/calc/real-stats', { product, material, days: 60 });
    if (!data.found) {
      toast('Нет данных', 'Похожих печатей за 60 дней не найдено');
      return;
    }
    $('calc_plate_grams').value = data.median_grams;
    $('calc_plate_hours').value = data.median_hours;
    toast('Подставлено из журнала',
      `Медиана: ${nfmt(data.median_grams)} г, ${nfmt(data.median_hours, 1)} ч (${data.count} печатей)`);
    runCalc();
  } catch (e) { fail(e); }
}

function restoreCalc() {
  let saved = {};
  try { saved = JSON.parse(store.get(CALC_KEY, '{}')) || {}; } catch (e) { saved = {}; }
  const map = {
    plate_grams: 'calc_plate_grams', plate_hours: 'calc_plate_hours',
    qty: 'calc_qty', fit: 'calc_fit', warmup: 'calc_warmup',
    material: 'calc_material', quality: 'calc_quality',
    supports_pct: 'calc_supports', color_swaps: 'calc_color_swaps',
    spool_price: 'calc_spool_price', spool_weight: 'calc_spool_weight',
    remove: 'calc_remove', sand: 'calc_sand', paint: 'calc_paint',
    design: 'calc_design', markup: 'calc_markup', fee: 'calc_fee',
    fix: 'calc_fix', delivery: 'calc_delivery',
    dim_x: 'calc_dim_x', dim_y: 'calc_dim_y',
  };
  Object.entries(map).forEach(([k, id]) => { if (saved[k] != null && saved[k] !== '' && $(id)) $(id).value = saved[k]; });
  if (!$('calc_spool_price').value) $('calc_spool_price').value = num(PF.state.settings.default_spool_price, 1600);
  if (!$('calc_spool_weight').value) $('calc_spool_weight').value = num(PF.state.settings.default_spool_weight, 1000);
  // Подготовка модели: восстановить этапы
  if (saved.prep_stages) {
    Object.entries(saved.prep_stages).forEach(([key, val]) => {
      const el = $('calc_prep_' + (key === 'duplicate' ? 'dupe' : key));
      if (el) el.value = val;
    });
  }
  if (saved.complexity && $('calc_complexity')) $('calc_complexity').value = saved.complexity;
}

/* ============================================================ каталог */
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

  const spoolLabels = $('spool_labels');
  if (spoolLabels) spoolLabels.addEventListener('click', () => {
    window.open('/labels.html?kind=spool', '_blank', 'noopener');
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
  const phBtn = $('cf_price_history');
  if (phBtn) phBtn.addEventListener('click', () => openPriceHistory($('cf_name').value.trim()));
  $('catalog_delete').addEventListener('click', async () => {
    if (!editingCatalog || !confirmDanger('Удалить позицию из базы изделий?')) return;
    try {
      await post('/api/catalog/delete', { id: editingCatalog });
      closeModal('catalog_modal');
      toast('Позиция удалена');
      PF.refreshCore();
    } catch (e) { fail(e); }
  });

  const spoolQrPrint = $('spool_qr_print');
  if (spoolQrPrint) spoolQrPrint.addEventListener('click', () => window.print());

  document.addEventListener('click', async (e) => {
    const dry = e.target.closest('[data-spool-dry]');
    if (dry) { spoolDry(dry.dataset.spoolDry); return; }
    const qr = e.target.closest('[data-spool-qr]');
    if (qr) { openSpoolQr(qr.dataset.spoolQr); return; }
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

  ['calc_plate_grams', 'calc_plate_hours', 'calc_qty', 'calc_fit', 'calc_warmup',
    'calc_material', 'calc_quality', 'calc_supports', 'calc_color_swaps',
    'calc_spool_price', 'calc_spool_weight', 'calc_remove', 'calc_sand', 'calc_paint',
    'calc_design', 'calc_markup', 'calc_fee', 'calc_fix', 'calc_delivery',
    'calc_model_cost', 'calc_design_hours', 'calc_sales_week',
    'calc_prep_find', 'calc_prep_orient', 'calc_prep_dupe',
    'calc_prep_profile', 'calc_prep_supports', 'calc_prep_slice',
    'calc_dim_x', 'calc_dim_y']
    .forEach((id) => { const el = $(id); if (el) el.addEventListener('input', runCalcDebounced); });

  // Сложность → автооценка подготовки
  const complexityEl = $('calc_complexity');
  if (complexityEl) complexityEl.addEventListener('change', autoPrepEstimate);

  // Кнопка таймера
  const timerBtn = $('calc_prep_timer');
  if (timerBtn) timerBtn.addEventListener('click', togglePrepTimer);

  // Кнопка автооценки
  const estBtn = $('calc_prep_estimate');
  if (estBtn) estBtn.addEventListener('click', autoPrepEstimate);

  // При смене материала — подставляем цену катушки из справочника
  const matSel = $('calc_material');
  if (matSel) matSel.addEventListener('change', () => {
    const mat = calcMaterials.find((m) => m.key === matSel.value);
    if (mat && mat.price_per_kg) {
      $('calc_spool_price').value = mat.price_per_kg;
    }
    updateMaterialInfo();
    runCalc();
  });

  $('calc_preset').addEventListener('change', (e) => {
    const item = PF.state.catalog.find((c) => c.id === e.target.value);
    if (!item) return;
    if (item.grams) $('calc_plate_grams').value = item.grams;
    if (item.hours) $('calc_plate_hours').value = item.hours;
    $('calc_fit').value = item.fit_per_plate || 1;
    if (item.material) {
      const m = calcMaterials.find((x) => x.name.toUpperCase() === (item.material || '').toUpperCase());
      if (m) { $('calc_material').value = m.key; $('calc_spool_price').value = m.price_per_kg; }
    }
    if (item.price) $('calc_markup').value = item.grams || item.hours ? $('calc_markup').value : 150;
    runCalc();
  });

  const realBtn = $('calc_real_stats');
  if (realBtn) realBtn.addEventListener('click', showRealStats);

  const exportBtn = $('calc_export');
  if (exportBtn) exportBtn.addEventListener('click', exportCalc);

  $('calc_from_cat').addEventListener('click', () => PF.go('inventory'));

  // Сценарии
  const scenMat = $('calc_scenario_materials');
  if (scenMat) scenMat.addEventListener('click', () => {
    const variants = ['PLA', 'PETG', 'ASA', 'TPU'].map((m) => ({
      material: m, quality: $('calc_quality').value, label: m,
    }));
    runScenarios(variants, 'Сравнение материалов');
  });
  const scenBatch = $('calc_scenario_batches');
  if (scenBatch) scenBatch.addEventListener('click', () => {
    const variants = [1, 3, 5, 10, 20, 50].map((q) => ({
      qty: q, material: $('calc_material').value,
      quality: $('calc_quality').value, label: `${q} шт`,
    }));
    runScenarios(variants, 'Сравнение партий');
  });
  const scenQ = $('calc_scenario_quality');
  if (scenQ) scenQ.addEventListener('click', () => {
    const variants = [
      { quality: 'draft', material: $('calc_material').value, label: 'Черновой' },
      { quality: 'standard', material: $('calc_material').value, label: 'Стандарт' },
      { quality: 'detail', material: $('calc_material').value, label: 'Детальный' },
      { quality: 'strong', material: $('calc_material').value, label: 'Прочный' },
    ];
    runScenarios(variants, 'Сравнение качества');
  });

  $('calc_to_order').addEventListener('click', () => {
    const v = calcInputs();
    const preset = PF.state.catalog.find((c) => c.id === $('calc_preset').value);
    PF.modules.ops.openOrder().then(() => {
      $('of_product').value = preset ? preset.name : '';
      // Передаём вес/время на штуку (пересчитанные из плиты)
      const unitGrams = v.fit > 0 ? (v.plate_grams / v.fit) : v.plate_grams;
      const unitHours = v.fit > 0 ? (v.plate_hours / v.fit) : v.plate_hours;
      $('of_grams').value = unitGrams.toFixed(1);
      $('of_hours').value = unitHours.toFixed(2);
      $('of_qty').value = v.qty;
      $('of_manual_minutes').value = v.remove + v.sand + v.paint;
      $('of_price').value = (($('calc_price').textContent || '').replace(/[^\d]/g, '') || '');
      if (preset && preset.niche_id) $('of_niche_id').value = preset.niche_id;
      if (preset && preset.file) $('of_file').value = preset.file;
      PF.modules.ops.openOrder && $('of_grams').dispatchEvent(new Event('input'));
    });
  });
}

/* =============================================================== старт */
PF.on('ready', () => { loadFilamentStats(); loadCalcMaterials(); bind(); restoreCalc(); });
PF.on('data', () => { renderStock(); renderCatalog(); });
PF.on('finance', renderFinance);
PF.on('view', (d) => { if (d.view === 'calc') runCalc(); });

// ------------------------------------------------------- экспорт расчёта
function exportCalc() {
  const v = calcInputs();
  const price = ($('calc_price').textContent || '').replace(/\s/g, '');
  const lines = [
    `PrintFlow — Расчёт себестоимости`,
    `Дата: ${new Date().toLocaleDateString('ru-RU')}`,
    ``,
    `Материал: ${v.material} · Качество: ${v.quality}`,
    `Плита: ${v.plate_grams} г × ${v.plate_hours} ч (${v.fit} шт/плита)`,
    `Партия: ${v.qty} шт (${Math.ceil(v.qty / v.fit)} плит)`,
    `Поддержка: ${v.supports_pct}% · Смен цвета: ${v.color_swaps}`,
    ``,
    `Себестоимость за штуку: ${($('calc_rows').querySelector('.res-row:nth-last-child(2) .val') || {}).textContent || '—'}`,
    `Рекомендованная цена: ${price}`,
    ``,
    `Подготовка модели: ${nfmt(v.model_prep_minutes, 0)} мин`,
    `Наценка: ${v.markup}% · Комиссия: ${v.fee}%`,
  ];
  const text = lines.join('\n');
  navigator.clipboard.writeText(text).then(
    () => toast('Скопировано', 'Расчёт в буфере обмена'),
    () => toast('Ошибка', 'Не удалось скопировать'));
}

// ------------------------------------------------------- быстрые пресеты
const CALC_PRESETS = {
  'Адресник PLA': { plate_grams: 35, plate_hours: 0.8, fit: 6, qty: 6,
    material: 'PLA', quality: 'standard', supports_pct: 0, complexity: 'simple',
    remove: 1, sand: 1, paint: 0, dim_x: 30, dim_y: 20 },
  'Табличка PETG': { plate_grams: 80, plate_hours: 2.5, fit: 4, qty: 4,
    material: 'PETG', quality: 'standard', supports_pct: 10, complexity: 'medium',
    remove: 2, sand: 3, paint: 0, dim_x: 60, dim_y: 40 },
  'Органайзер': { plate_grams: 150, plate_hours: 5, fit: 2, qty: 2,
    material: 'PETG', quality: 'standard', supports_pct: 20, complexity: 'medium',
    remove: 3, sand: 5, paint: 0, dim_x: 100, dim_y: 80 },
  'QR-стойка': { plate_grams: 120, plate_hours: 4, fit: 2, qty: 2,
    material: 'PLA_MATTE', quality: 'detail', supports_pct: 5, complexity: 'medium',
    remove: 2, sand: 5, paint: 5, dim_x: 60, dim_y: 40 },
  'Корпус ABS': { plate_grams: 200, plate_hours: 6, fit: 1, qty: 1,
    material: 'ABS', quality: 'strong', supports_pct: 25, complexity: 'complex',
    remove: 3, sand: 10, paint: 10, dim_x: 120, dim_y: 80 },
  'Ножки TPU': { plate_grams: 40, plate_hours: 3, fit: 8, qty: 8,
    material: 'TPU', quality: 'standard', supports_pct: 0, complexity: 'simple',
    remove: 1, sand: 0, paint: 0, dim_x: 20, dim_y: 20 },
};

function applyPreset(name) {
  const p = CALC_PRESETS[name];
  if (!p) return;
  const map = {
    plate_grams: 'calc_plate_grams', plate_hours: 'calc_plate_hours',
    fit: 'calc_fit', qty: 'calc_qty', supports_pct: 'calc_supports',
    remove: 'calc_remove', sand: 'calc_sand', paint: 'calc_paint',
    dim_x: 'calc_dim_x', dim_y: 'calc_dim_y',
  };
  Object.entries(map).forEach(([k, id]) => { if (p[k] != null && $(id)) $(id).value = p[k]; });
  if (p.material && $('calc_material')) $('calc_material').value = p.material;
  if (p.quality && $('calc_quality')) $('calc_quality').value = p.quality;
  if (p.complexity && $('calc_complexity')) {
    $('calc_complexity').value = p.complexity;
    autoPrepEstimate();
  }
  runCalc();
  toast('Пресет', name);
}

PF.modules.money = { openSpool, openCatalog, openTx, runCalc, renderFinance, exportCalc, applyPreset };
})();
