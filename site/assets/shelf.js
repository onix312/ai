/* PrintFlow 2.0 — стеллаж магазина: готовая продукция на полке.
   Остатки штук, приход с партии печати, продажи (вручную / по QR),
   инвентаризация, оборачиваемость, мёртвый сток, QR-ценники. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, money, nfmt, hoursText, dateText, dateTimeText,
  agoText, toast, fail, openModal, closeModal, confirmDanger } = U;
const { get, post } = PF.api;

let editingShelf = null;
let shelfData = { items: [], summary: {}, moves: [], tags: {}, forecast: [], head: null };
let stockGoods = [];

const KIND_LABEL = {
  produce: 'Приход', sale: 'Продажа', online: 'Продажа онлайн',
  writeoff: 'Списание', inventory: 'Инвентаризация',
};
const STATUS_LABEL = {
  ok: 'В наличии', low: 'Мало', dead: 'Мёртвый сток', empty: 'Пусто',
};
// Существующие записи с устаревшими classic/compact/minimal остаются печатабельными:
// API уже нормализует их, а этот fallback покрывает офлайн-кэш старого интерфейса.
const TAG_TEMPLATE_ALIASES = { classic: 'standard', compact: 'standard', minimal: 'standard' };
const TAG_FORMAT_LABEL = { standard: 'ценник 67 × 32 мм', promo: 'промостенд 67 × 57 мм' };
const TAG_VARIANT_LABEL = {
  clean: 'чистый', accent: 'акцент на цене', sale: 'акция', mono: 'монохром', photo: 'фото товара',
};
function normalizedTagTemplate(value) {
  const template = String(value || '').trim().toLowerCase();
  return TAG_TEMPLATE_ALIASES[template] || (TAG_FORMAT_LABEL[template] ? template : 'standard');
}
function normalizedTagVariant(value) {
  const variant = String(value || '').trim().toLowerCase();
  return TAG_VARIANT_LABEL[variant] ? variant : 'clean';
}
function tagFormatLabel(value) { return TAG_FORMAT_LABEL[normalizedTagTemplate(value)]; }
function tagVariantLabel(value) { return TAG_VARIANT_LABEL[normalizedTagVariant(value)]; }
function syncShelfTagVariant() {
  const format = normalizedTagTemplate($('shf_tag_template').value);
  const option = $('shf_tag_variant').querySelector('option[value="photo"]');
  if (option) option.disabled = format !== 'promo';
  // Keep the compact 67 × 32 label readable: it deliberately has no room for
  // a product photo and uses the price-focused look instead.
  if (format !== 'promo' && $('shf_tag_variant').value === 'photo') {
    $('shf_tag_variant').value = 'accent';
  }
}

/* ============================================================== загрузка */
async function refreshShelf() {
  try {
    const [data, moves, tags, forecast, head] = await Promise.all([
      get('/api/shelf'), get('/api/shelf/moves', { limit: 60 }),
      get('/api/shelf/tags').catch(() => ({ hit: [], new: [], last: [] })),
      get('/api/shelf/forecast', { days: 7 }).catch(() => ({ days: 7, items: [] })),
      get('/api/content/shelf-header', { days: 7 }).catch(() => null),
    ]);
    shelfData = {
      items: data.items || [], summary: data.summary || {}, moves: moves.moves || [],
      tags: tags || {}, forecast: forecast.items || [], head: head || null,
    };
    if (document.querySelector('#view-shelf.on')) renderShelf();
    updateNavTag();
    PF.emit('shelf', shelfData);
  } catch (e) { /* офлайн */ }
}
PF.refreshShelf = refreshShelf;

/* Короткая сводка для статус-бара цеха: «12 шт · 2 тревоги» */
function shelfSummaryText() {
  const s = shelfData.summary || {};
  if (!s.items) return null;
  const alerts = (s.low || 0) + (s.dead || 0);
  return `${nfmt(s.qty)} шт${alerts ? ` · ${alerts} ⚠` : ''}`;
}

function updateNavTag() {
  const tag = $('nav_shelf_tag');
  if (!tag) return;
  const low = shelfData.summary.low || 0;
  const dead = shelfData.summary.dead || 0;
  const n = low + dead;
  tag.hidden = !n;
  tag.textContent = String(n);
  tag.className = 'tag' + (dead ? ' warn' : '');
}

/* ================================================================ рендер */
function shelfKpi(label, value, sub, kind) {
  return `<div class="kpi ${kind || ''}"><span class="label">${esc(label)}</span>`
    + `<b class="value">${value}</b><span class="sub">${esc(sub || '')}</span></div>`;
}

function liveBadgeFor(id) {
  const t = shelfData.tags || {};
  if ((t.last || []).some((x) => x.id === id)) return '<span class="shelf-live last">Последний!</span>';
  if ((t.new || []).some((x) => x.id === id)) return '<span class="shelf-live new">Новинка</span>';
  if ((t.hit || []).some((x) => x.id === id)) return '<span class="shelf-live hit">Хит</span>';
  return '';
}

function renderShelf() {
  const s = shelfData.summary || {};
  const head = shelfData.head;
  const headHtml = head && head.text
    ? `<div class="notice" style="margin-bottom:12px"><span>✦</span><span>${esc(head.text)}${head.new_items && head.new_items.length ? ` Новинки: ${head.new_items.map((n) => '«' + esc(n) + '»').join(', ')}.` : ''}</span></div>`
    : '';
  const fc = shelfData.forecast || [];
  const maxFc = Math.max(1, ...fc.map((f) => num(f.qty) + num(f.gap)));
  const fcHtml = fc.length ? `
   <div class="shelf-forecast">
    <b>Прогноз полки на 7 дней</b> <span class="muted" style="font-size:11.5px">— при текущем темпе продаж</span>
    ${fc.slice(0, 6).map((f) =>
      `<div class="row"><span style="width:150px;flex:none">${esc(f.name)}</span>`
      + `<span class="bar"><i style="width:${Math.round(num(f.qty) / maxFc * 100)}%"></i>`
      + (f.gap ? `<i class="gap" style="width:${Math.round(num(f.gap) / maxFc * 100)}%"></i>` : '') + `</span>`
      + `<span style="width:130px;text-align:right;flex:none">${f.empty
        ? '<span class="gap-warn">закончится</span>'
        : `останется ${nfmt(f.projected)} шт${f.gap ? ` <span class="gap-warn">(не хватит ${nfmt(f.gap)})</span>` : ''}`}</span></div>`
    ).join('')}
   </div>` : '';
  $('shelf_kpis').innerHTML =
    headHtml + [
      shelfKpi('Штук на стеллаже', nfmt(s.qty), `${nfmt(s.items)} позиций`),
      shelfKpi('Остаток в рублях', money(s.value), 'по себестоимости (заморожено)'),
      shelfKpi('Продано за 7 дней', `${nfmt(s.sold_7)} шт`, `${money(s.sold_7_money)}`),
      shelfKpi('Мёртвый сток', String(s.dead || 0), s.dead_value ? `${money(s.dead_value)} заморожено` : 'нет позиций без продаж',
        s.dead ? 'bad' : 'ok'),
      shelfKpi('План пополнения', `${nfmt(s.plan_qty)} шт`, 'напечатать, чтобы хватило на 7 дней'),
    ].join('') + fcHtml;

  const items = shelfData.items || [];
  $('shelf_grid').innerHTML = items.length ? items.map((i) => {
    const st = i.status || 'ok';
    const days = i.days_left;
    const warn = st === 'dead' ? 'bad' : st === 'low' ? 'warn' : 'ok';
    return `<article class="shelf-card ${st}" data-shelf="${esc(i.id)}">`
      + `<div class="shead">`
      + (i.photo ? `<img class="sphoto" src="/api/shelf/photo.jpg?id=${esc(i.id)}&t=${esc(i.updated_at || '')}" alt="">`
        : `<span class="sphoto ph">◻</span>`)
      + `<div class="sinfo"><h3>${esc(i.name)}${liveBadgeFor(i.id)}</h3>`
      + `<small class="muted">${i.barcode ? `1С ✓ · ${esc(i.barcode)}` : '1С: код не задан'} · ${tagFormatLabel(i.tag_template)} · ${tagVariantLabel(i.tag_variant)}${i.tag_badge ? ' · ' + esc(i.tag_badge) : ''}</small>`
      + (i.note ? `<small class="muted">${esc(i.note)}</small>` : '') + `</div>`
      + `<button class="icon-btn sm" type="button" data-shelf-edit="${esc(i.id)}" title="Изменить">✎</button></div>`
      + `<div class="sbody">`
      + `<div class="sqty ${warn}"><b>${nfmt(i.qty)}</b><span>шт</span>`
      + `<div class="bar thin"><i style="width:${i.min_qty ? clamp(num(i.qty) / num(i.min_qty) * 50, 0, 100) : 100}%;background:var(--${warn === 'ok' ? 'ok' : warn})"></i></div></div>`
      + `<div class="sfacts">`
      + `<span>Цена <b>${money(i.price)}</b></span>`
      + `<span>Себест. <b>${money(i.cost_per_unit)}</b></span>`
      + `<span>Маржа <b class="${num(i.margin) >= 0 ? 'pos' : 'neg'}">${money(i.margin)}</b></span>`
      + `<span>Продано 7д <b>${nfmt(i.sold_7)} шт</b></span>`
      + (days != null ? `<span>Хватит на <b>${nfmt(days, 1)} дн.</b></span>` : `<span class="muted">продаж нет</span>`)
      + `</div></div>`
      + `<div class="sacts">`
      + `<span class="chip ${warn}">${STATUS_LABEL[st] || st}</span>`
      + `<span class="spacer"></span>`
      + (i.plan_qty ? `<span class="plan-hint">напечатать ${nfmt(i.plan_qty)} шт</span>` : '')
      + `<button class="btn sm ghost" type="button" data-shelf-card="${esc(i.id)}">▤ Карточка</button>`
      + `<button class="btn sm ghost" type="button" data-shelf-tag="${esc(i.id)}">▦ Ценник</button>`
      + `<button class="btn sm" type="button" data-shelf-sell="${esc(i.id)}">−1</button>`
      + `<button class="btn sm" type="button" data-shelf-prod="${esc(i.id)}">+</button>`
      + `</div></article>`;
  }).join('') : '<div class="empty" style="grid-column:1/-1"><span class="big">▤</span><b>На стеллаже пока пусто</b><span>Добавьте позицию и положите на неё первую партию печати.</span></div>';

  const moves = shelfData.moves || [];
  $('shelf_moves').innerHTML = moves.length ? moves.slice(0, 40).map((m) => {
    const positive = num(m.qty) > 0;
    return `<div class="tx-row">`
      + `<span class="tx-ic ${positive ? 'income' : 'expense'}">${positive ? '↑' : '↓'}</span>`
      + `<div class="tx-body"><b>${esc(m.item_name || 'Позиция')} · ${esc(KIND_LABEL[m.kind] || m.kind)}</b>`
      + `<small>${esc(dateTimeText(m.at))}${m.note ? ' · ' + esc(m.note) : ''}</small></div>`
      + `<span class="amt ${positive ? 'pos' : 'neg'}">${positive ? '+' : ''}${nfmt(m.qty)} шт${num(m.price) ? ' · ' + money(num(m.price) * Math.abs(num(m.qty))) : ''}</span>`
      + `</div>`;
  }).join('') : '<div class="empty compact"><span>Движений пока нет.</span></div>';
}

function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

/* ================================== готовые товары для новой позиции */
async function loadStockGoods() {
  try {
    const data = await get('/api/shelf/stock-available', { goods: 1 });
    stockGoods = data.items || [];
  } catch (e) { stockGoods = []; }
  return stockGoods;
}

function fillStockGoodsSelect() {
  const sel = $('shf_stock_item');
  sel.innerHTML = '<option value="">Заполню вручную</option>'
    + stockGoods.map((i, idx) =>
      `<option value="${idx}">${esc(i.name)} · ${esc(i.warehouse_name)} · ${nfmt(i.qty)} ${esc(i.unit || 'шт')}</option>`).join('');
  $('shf_stock_info').hidden = true;
}

function selectedStockGood() {
  const v = $('shf_stock_item').value;
  if (v === '' || v == null) return null;
  return stockGoods[Number(v)] || null;
}

function onStockGoodChange() {
  const row = selectedStockGood();
  const info = $('shf_stock_info');
  const qtyEl = $('shf_qty');
  if (!row) {
    info.hidden = true;
    qtyEl.disabled = false;
    return;
  }
  // Подставляем данные товара в пустые поля — владелец может поправить.
  if (!$('shf_name').value.trim()) $('shf_name').value = row.name || '';
  if (!$('shf_nom_id').value) $('shf_nom_id').value = row.nom_id || '';
  if (!num($('shf_price').value)) $('shf_price').value = num(row.price);
  if (!num($('shf_cost').value)) $('shf_cost').value = num(row.avg_cost);
  // Остаток придёт переносом со склада, а не «начальным остатком».
  qtyEl.disabled = true;
  qtyEl.value = '';
  const unit = row.unit || 'шт';
  const max = pieceUnit(unit) ? Math.floor(num(row.qty)) : num(row.qty);
  const q = $('shf_stock_qty');
  q.min = pieceUnit(unit) ? '1' : '0';
  q.step = pieceUnit(unit) ? '1' : '0.1';
  q.max = max;
  if (num(q.value) > max) q.value = max;
  $('shf_stock_info_text').textContent =
    `На «${row.warehouse_name}» доступно ${nfmt(row.qty)} ${unit}`
    + (num(row.avg_cost) ? ` · себестоимость ~${money(row.avg_cost)}/${pieceUnit(unit) ? 'шт' : unit}` : '')
    + (num(row.price) ? ` · цена ${money(row.price)}` : '');
  info.hidden = false;
}

/* ============================================================== позиция */
function fillShelfSelectors(keep) {
  const items = shelfData.items || [];
  const opts = items.map((i) => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('');
  const set = (id, val) => { const el = $(id); if (el) { el.innerHTML = val; if (keep) el.value = keep; } };
  set('spf_item', opts);
  set('sif_item', opts);
  if (keep) { $('spf_item').value = keep; $('sif_item').value = keep; }
}

function openShelf(id) {
  editingShelf = id || null;
  const i = id ? (shelfData.items || []).find((x) => x.id === id) : null;
  const d = i || {
    name: '', catalog_id: '', nom_id: '', price: '', cost_per_unit: '', qty: 0,
    min_qty: '', note: '', barcode: '', sku: '', tag_template: 'standard', tag_variant: 'clean',
    tag_badge: '', tag_color: '#4f46e5', tag_note: '', tag_old_price: 0,
  };
  $('shf_name').value = d.name || '';
  $('shf_nom_id').value = d.nom_id || '';
  $('shf_price').value = d.price ?? '';
  $('shf_cost').value = d.cost_per_unit ?? '';
  $('shf_qty').value = d.qty ?? '';
  $('shf_min').value = d.min_qty ?? '';
  $('shf_note').value = d.note || '';
  $('shf_barcode').value = d.barcode || '';
  $('shf_sku').value = d.sku || '';
  $('shf_tag_template').value = normalizedTagTemplate(d.tag_template);
  $('shf_tag_variant').value = normalizedTagVariant(d.tag_variant);
  syncShelfTagVariant();
  $('shf_tag_badge').value = d.tag_badge || '';
  $('shf_tag_color').value = /^#[0-9a-f]{6}$/i.test(d.tag_color || '') ? d.tag_color : '#4f46e5';
  $('shf_tag_note').value = d.tag_note || '';
  $('shf_tag_old_price').value = num(d.tag_old_price) || '';
  $('shf_catalog_id').innerHTML = '<option value="">Без привязки</option>'
    + (PF.state.catalog || []).map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  $('shf_catalog_id').value = d.catalog_id || '';
  const img = $('shf_photo_preview');
  img.hidden = !d.photo;
  if (d.photo) img.src = `/api/shelf/photo.jpg?id=${esc(d.id)}&t=${esc(d.updated_at || '')}`;
  // Перенос готовых товаров со склада — только для новой позиции.
  if (!id) {
    $('shf_stock_section').hidden = false;
    $('shf_stock_item').value = '';
    $('shf_stock_qty').value = 1;
    $('shf_qty').disabled = false;
    $('shf_stock_info').hidden = true;
    loadStockGoods().then(fillStockGoodsSelect);
  } else {
    $('shf_stock_section').hidden = true;
    $('shf_stock_item').value = '';
  }
  $('shelf_delete').hidden = !id;
  $('shf_tag_open').hidden = !id;
  $('shf_tag_open').dataset.item = id || '';
  $('shelf_modal_title').textContent = id ? 'Позиция: ' + d.name : 'Новая позиция стеллажа';
  openModal('shelf_modal');
}

async function saveShelf() {
  const payload = {
    id: editingShelf || '',
    name: $('shf_name').value.trim(),
    catalog_id: $('shf_catalog_id').value,
    nom_id: $('shf_nom_id').value,
    price: num($('shf_price').value),
    cost_per_unit: num($('shf_cost').value),
    qty: num($('shf_qty').value),
    min_qty: num($('shf_min').value),
    note: $('shf_note').value.trim(),
    barcode: $('shf_barcode').value.trim(),
    sku: $('shf_sku').value.trim(),
    tag_template: $('shf_tag_template').value,
    tag_variant: $('shf_tag_variant').value,
    tag_badge: $('shf_tag_badge').value.trim(),
    tag_color: $('shf_tag_color').value,
    tag_note: $('shf_tag_note').value.trim(),
    tag_old_price: num($('shf_tag_old_price').value),
  };
  if (!payload.name) return fail(new Error('Укажите название позиции'));

  const stockRow = editingShelf ? null : selectedStockGood();
  const stockQty = num($('shf_stock_qty').value);
  const viaStock = !!(stockRow && stockQty > 0);
  if (viaStock) {
    if (pieceUnit(stockRow.unit || 'шт') && stockQty !== Math.round(stockQty)) {
      return fail(new Error('Штучные товары переносятся целыми штуками'));
    }
    if (stockQty > num(stockRow.qty) + 1e-9) {
      return fail(new Error(`На складе только ${nfmt(stockRow.qty)} ${stockRow.unit || 'шт'}`));
    }
  }

  let savedItemId = '';
  try {
    const res = viaStock
      ? await post('/api/shelf/save-from-stock', {
          ...payload,
          nom_id: stockRow.nom_id, warehouse_id: stockRow.warehouse_id, qty: stockQty,
        })
      : await post('/api/shelf/save', payload);
    savedItemId = res.item && res.item.id;
  } catch (e) { return fail(e); }

  closeModal('shelf_modal');
  // фото, если выбрали
  const file = $('shf_photo_file').files[0];
  if (file && savedItemId) {
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        await post('/api/shelf/photo', { id: savedItemId, data: reader.result });
        await refreshShelf();
        toast('Позиция сохранена', payload.name);
      } catch (e) { fail(e); }
    };
    reader.readAsDataURL(file);
  } else {
    await refreshShelf();
    toast(viaStock ? 'Позиция создана' : 'Позиция сохранена',
      viaStock ? `«${payload.name}» +${nfmt(stockQty)} шт со склада` : payload.name);
  }
}

/* ============================================================== приход */
function fillJobs() {
  const jobs = (PF.state.jobs.history || []).filter((j) => j.state === 'done').slice(0, 30);
  $('spf_job').innerHTML = '<option value="">Вручную (без задания)</option>' + jobs.map((j) =>
    `<option value="${esc(j.id)}">${esc(dateText(j.finished_at))} · ${esc(j.name || j.file || 'печать')} · ${money(j.cost)}</option>`).join('');
}

function openProduce(itemId) {
  fillShelfSelectors(itemId || ($('spf_item') && $('spf_item').value) || '');
  fillJobs();
  $('spf_qty').value = 1;
  $('spf_cost').value = '';
  $('spf_note').value = '';
  const item = (shelfData.items || []).find((x) => x.id === (itemId || ''));
  if (item) $('spf_cost').value = item.cost_per_unit || '';
  openModal('shelf_produce_modal');
}

async function saveProduce() {
  const item_id = $('spf_item').value;
  if (!item_id) return fail(new Error('Выберите позицию'));
  try {
    const res = await post('/api/shelf/produce', {
      item_id, qty: num($('spf_qty').value), job_id: $('spf_job').value,
      note: $('spf_note').value.trim(), cost_per_unit: num($('spf_cost').value),
    });
    closeModal('shelf_produce_modal');
    await refreshShelf();
    toast('Приход записан', `+${nfmt(num($('spf_qty').value))} шт`);
  } catch (e) { fail(e); }
}

/* ============================================== перемещение со склада */
let stockAvail = [];

async function openTransfer() {
  try {
    const data = await get('/api/shelf/stock-available');
    stockAvail = data.items || [];
  } catch (e) { return fail(e); }
  if (!stockAvail.length) {
    return fail(new Error('На учётных складах нет товара с остатком от 1 шт — перемещать нечего.'));
  }
  $('stf_item').innerHTML = stockAvail.map((i, idx) =>
    `<option value="${idx}">${esc(i.name)} · ${esc(i.warehouse_name)} · ${nfmt(i.qty)} шт</option>`).join('');
  $('stf_shelf_item').innerHTML = '<option value="">Найти по названию / создать автоматически</option>'
    + (shelfData.items || []).map((i) => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('');
  $('stf_qty').value = 1;
  $('stf_note').value = '';
  updateTransferInfo();
  openModal('shelf_transfer_modal');
}

function pieceUnit(unit) { return ['шт','шт.','piece','pcs'].includes(String(unit || 'шт').toLowerCase()); }

function updateTransferInfo() {
  const row = stockAvail[Number($('stf_item').value)] || null;
  const info = $('stf_info');
  if (!row) { info.hidden = true; return; }
  const unit = row.unit || 'шт';
  const max = pieceUnit(unit) ? Math.floor(num(row.qty)) : num(row.qty);
  $('stf_qty').max = max;
  $('stf_qty').min = pieceUnit(unit) ? '1' : '0';
  $('stf_qty').step = pieceUnit(unit) ? '1' : '0.1';
  if (num($('stf_qty').value) > max) $('stf_qty').value = max;
  $('stf_info_text').textContent = `Доступно на «${row.warehouse_name}»: ${nfmt(row.qty)} ${unit}`
    + (row.avg_cost ? ` · себестоимость ~${money(row.avg_cost)}/${pieceUnit(unit) ? 'шт' : unit}` : '');
  info.hidden = false;
}

async function saveTransfer() {
  const row = stockAvail[Number($('stf_item').value)] || null;
  if (!row) return fail(new Error('Выберите товар'));
  const unit = row.unit || 'шт';
  const qty = num($('stf_qty').value);
  if (qty <= 0) return fail(new Error('Количество должно быть больше нуля'));
  if (pieceUnit(unit) && qty !== Math.round(qty)) return fail(new Error('Штучные товары перемещаются целыми штуками'));
  if (qty > num(row.qty) + 1e-9) return fail(new Error(`На складе только ${nfmt(row.qty)} ${unit}`));
  try {
    await post('/api/shelf/transfer', {
      nom_id: row.nom_id, warehouse_id: row.warehouse_id, qty,
      item_id: $('stf_shelf_item').value, note: $('stf_note').value.trim(),
    });
    closeModal('shelf_transfer_modal');
    await refreshShelf();
    toast('Перемещено на стеллаж', `${esc(row.name)} +${nfmt(qty)} шт`);
  } catch (e) { fail(e); }
}

/* ============================================================== продажи */
function openSales() {
  const items = (shelfData.items || []).filter((i) => num(i.qty) > 0);
  if (!items.length) return fail(new Error('На стеллаже нет товара'));
  $('ssf_channel').value = 'shelf';
  $('ssf_rows').innerHTML = items.map((i) => `<div class="sale-row">`
    + `<div class="sinfo"><b>${esc(i.name)}</b><small>на стеллаже ${nfmt(i.qty)} шт · ${money(i.price)}/шт</small></div>`
    + `<input type="number" min="0" max="${Math.max(0, Math.round(num(i.qty)))}" step="1" value="" placeholder="0" data-sale-qty="${esc(i.id)}">`
    + `</div>`).join('');
  openModal('shelf_sale_modal');
}

async function saveSales() {
  const rows = $$('[data-sale-qty]').map((el) => ({
    item_id: el.dataset.saleQty, qty: num(el.value),
  })).filter((r) => r.qty > 0);
  if (!rows.length) return fail(new Error('Введите хотя бы одно количество'));
  try {
    const res = await post('/api/shelf/sales', { rows, channel: $('ssf_channel').value });
    closeModal('shelf_sale_modal');
    await refreshShelf();
    PF.refreshFinance();
    const n = (res.results || []).length;
    toast('Продажи записаны', `${n} позиций`);
  } catch (e) { fail(e); }
}

/* ========================================================= инвентаризация */
function openInventory(itemId) {
  fillShelfSelectors(itemId || '');
  const item = (shelfData.items || []).find((x) => x.id === (itemId || ''));
  if (!item) return fail(new Error('Выберите позицию'));
  $('sif_item').value = item.id;
  $('sif_expected').textContent = nfmt(item.qty);
  $('sif_actual').value = item.qty;
  $('sif_note').value = '';
  openModal('shelf_inventory_modal');
}

async function saveInventory() {
  const item_id = $('sif_item').value;
  if (!item_id) return fail(new Error('Выберите позицию'));
  try {
    const res = await post('/api/shelf/inventory', {
      item_id, actual: num($('sif_actual').value), note: $('sif_note').value.trim(),
    });
    closeModal('shelf_inventory_modal');
    await refreshShelf();
    toast(res.diff ? `Расхождение ${res.diff > 0 ? '+' : ''}${nfmt(res.diff)} шт` : 'Всё сошлось',
      res.diff ? 'Остаток скорректирован' : 'Остаток подтверждён', res.diff ? 'warn' : 'ok');
  } catch (e) { fail(e); }
}

/* ============================================================ QR-ценник */
async function openQr(itemId) {
  const item = (shelfData.items || []).find((x) => x.id === itemId);
  if (!item) return;
  try {
    const res = await get('/api/shelf/qr-link', { id: itemId });
    const url = res.url || '';
    const img = $('shelf_qr_code');
    img.innerHTML = '';
    // рисуем QR через имеющийся генератор (qr.js)
    if (window.QR && window.QR.svg) {
      img.innerHTML = window.QR.svg(url, { size: 260, dark: '#111827', light: '#ffffff' });
    } else {
      img.innerHTML = '<div class="empty compact"><span>QR недоступен</span></div>';
    }
    const warn = res.reachable === false
      ? '<div class="notice warn" style="margin-top:10px"><span>⚠</span><span>'
        + 'В QR попал localhost — с телефона не откроется. Укажите LAN-адрес в '
        + 'Настройки → Система → Адрес для QR.</span></div>'
      : '';
    $('shelf_qr_info').innerHTML = `<b>${esc(item.name)}</b>`
      + `<div class="qr-price">${money(item.price)}</div>`
      + `<small class="muted">${esc(url)}</small>`
      + warn;
    openModal('shelf_qr_modal');
  } catch (e) { fail(e); }
}

/* Карточка товара для полки + штрихкод (идеи 101, 106) */
async function openShelfCard(itemId) {
  const item = (shelfData.items || []).find((x) => x.id === itemId);
  if (!item) return;
  const out = $('shelf_card_out');
  out.innerHTML = '<span class="muted">Собираем карточку…</span>';
  openModal('shelf_card_modal');
  $('shelf_card_title').textContent = item.name;
  try {
    const bc = item.barcode
      ? await get('/api/labels/code128', { text: item.barcode }).catch(() => null)
      : null;
    const url = await get('/api/shelf/qr-link', { id: itemId }).then((r) => r.url || '').catch(() => '');
    const qr = (window.QR && url) ? window.QR.svg(url, { size: 120 }) : '';
    out.innerHTML = `
     <div style="display:flex;gap:14px;flex-wrap:wrap">
      <div style="flex:1;min-width:220px">
       <div style="font-size:17px;font-weight:700">${esc(item.name)}</div>
       <div style="font-size:26px;font-weight:800;margin:6px 0">${money(item.price)}</div>
       <div class="muted" style="font-size:12.5px">
        ${item.catalog_id ? 'Из каталога' : 'Позиция полки'} · остаток ${nfmt(item.qty)} шт${item.min_qty ? ` · минимум ${nfmt(item.min_qty)}` : ''}
       </div>
       <div class="muted" style="font-size:12.5px;margin-top:6px">Продано за 7 дней: ${nfmt(item.sold_7)} шт · за 30: ${nfmt(item.sold_30)} шт</div>
       ${bc ? bc.svg : ''}
       ${bc ? `<div class="muted" style="font-size:11px">Code 128 · ${esc(bc.text)} — тот же код ищется в 1С</div>` : '<div class="notice warn" style="margin-top:8px"><span>!</span><span>Добавьте штрихкод 1С в карточке позиции.</span></div>'}
      </div>
      <div style="text-align:center">${qr}<small class="muted">QR → страница позиции</small></div>
     </div>`;
  } catch (e) {
    out.innerHTML = `<span style="color:#ef4444">${esc(e.message)}</span>`;
  }
}

/* =============================================================== события */
function bind() {
  $('shelf_add').addEventListener('click', () => openShelf());
  $('shelf_save').addEventListener('click', saveShelf);
  $('shelf_delete').addEventListener('click', async () => {
    if (!editingShelf || !confirmDanger('Удалить позицию стеллажа? История движений останется.')) return;
    try {
      await post('/api/shelf/delete', { id: editingShelf });
      closeModal('shelf_modal');
      await refreshShelf();
      toast('Позиция удалена');
    } catch (e) { fail(e); }
  });
  $('shf_photo_btn').addEventListener('click', () => $('shf_photo_file').click());
  $('shf_stock_item').addEventListener('change', onStockGoodChange);
  $('shf_tag_template').addEventListener('change', syncShelfTagVariant);
  $('shf_tag_open').addEventListener('click', () => {
    const item = $('shf_tag_open').dataset.item;
    if (item) window.open(`/price-tags.html?item=${encodeURIComponent(item)}`, '_blank', 'noopener');
  });
  $('shf_photo_file').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { $('shf_photo_preview').src = reader.result; $('shf_photo_preview').hidden = false; };
    reader.readAsDataURL(file);
  });

  $('shelf_produce_btn').addEventListener('click', () => openProduce());
  $('shelf_produce_save').addEventListener('click', saveProduce);
  $('spf_job').addEventListener('change', (e) => {
    const job = (PF.state.jobs.history || []).find((j) => j.id === e.target.value);
    if (job && num(job.cost)) {
      const qty = Math.max(1, num($('spf_qty').value, 1));
      $('spf_cost').value = round2(num(job.cost) / qty);
    }
  });
  $('spf_qty').addEventListener('input', () => {
    const job = (PF.state.jobs.history || []).find((j) => j.id === $('spf_job').value);
    if (job && num(job.cost)) {
      const qty = Math.max(1, num($('spf_qty').value, 1));
      $('spf_cost').value = round2(num(job.cost) / qty);
    }
  });

  $('shelf_transfer_btn').addEventListener('click', openTransfer);
  $('shelf_transfer_save').addEventListener('click', saveTransfer);
  $('stf_item').addEventListener('change', updateTransferInfo);
  $('stf_qty').addEventListener('input', updateTransferInfo);

  $('shelf_sale_btn').addEventListener('click', openSales);
  $('shelf_sale_save').addEventListener('click', saveSales);
  $('shelf_inventory_btn').addEventListener('click', () => openInventory());
  $('shelf_inventory_save').addEventListener('click', saveInventory);
  $('sif_item').addEventListener('change', (e) => {
    const item = (shelfData.items || []).find((x) => x.id === e.target.value);
    if (item) { $('sif_expected').textContent = nfmt(item.qty); $('sif_actual').value = item.qty; }
  });

  $('shelf_qr_print').addEventListener('click', () => window.print());
  $('shelf_grid').addEventListener('click', (e) => {
    const edit = e.target.closest('[data-shelf-edit]');
    if (edit) { openShelf(edit.dataset.shelfEdit); return; }
    const tag = e.target.closest('[data-shelf-tag]');
    if (tag) {
      window.open(`/price-tags.html?item=${encodeURIComponent(tag.dataset.shelfTag)}`, '_blank', 'noopener');
      return;
    }
    const card = e.target.closest('[data-shelf-card]');
    if (card) { openShelfCard(card.dataset.shelfCard); return; }
    const sell = e.target.closest('[data-shelf-sell]');
    if (sell) {
      const item = (shelfData.items || []).find((x) => x.id === sell.dataset.shelfSell);
      if (!item) return;
      if (!confirmDanger(`Продать 1 шт «${item.name}»${num(item.price) ? ' за ' + money(item.price) : ' (без цены)'}?`)) return;
      post('/api/shelf/sale', { item_id: item.id, qty: 1, channel: 'shelf', note: 'быстрое списание' })
        .then(async () => { await refreshShelf(); PF.refreshFinance(); toast('Продано', item.name); })
        .catch(fail);
      return;
    }
    const prod = e.target.closest('[data-shelf-prod]');
    if (prod) { openProduce(prod.dataset.shelfProd); }
  });
}

function round2(v) { return Math.round(num(v) * 100) / 100; }

/* =============================================================== старт */
PF.on('ready', () => { bind(); refreshShelf(); });
PF.on('data', () => { if (document.querySelector('#view-shelf.on')) refreshShelf(); });
PF.on('view', (d) => { if (d.view === 'shelf') refreshShelf(); });
setInterval(() => { if (document.querySelector('#view-shelf.on')) refreshShelf(); }, 30000);

PF.modules.shelf = { refreshShelf, openShelf, openProduce, openSales, openInventory,
  get shelfSummary() { return shelfSummaryText(); } };
})();
