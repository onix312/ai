/* PrintFlow 3.0 — номенклатура, склады, документы и партии печати.
   Учёт в духе 1С: остаток берётся из регистра движений, документы проводятся
   и распроводятся, партия печати сама приходует готовое на склад. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, money, nfmt, hoursText, dateText, dateTimeText,
  toast, fail, openModal, closeModal, confirmDanger, debounce } = U;
const { get, post } = PF.api;

let data = { items: [], summary: {}, groups: [], warehouses: [], priceTypes: [] };
let docsData = [];
let batchData = [];
let editingNom = null;
let editingNomUpdatedAt = '';
let editingDoc = null;
let editingWh = null;
let specRows = [];
let docRows = [];
let planRows = [];
let viewMode = 'cards';
let batchPlan = null;
let mixedRows = [{ nom_id: '', qty: 1 }];

/* ===================== смешанная плита: разные товары на одном столе ==== */
function renderMixedRows() {
  const host = $('bf_item_rows');
  if (!host) return;
  const noms = data.items.filter((i) => !num(i.archived));
  if (!noms.length) {
    host.innerHTML = '<small class="muted">Сначала добавьте товары в номенклатуру.</small>';
    return;
  }
  const options = (selected) => noms.map((n) =>
    `<option value="${esc(n.id)}"${n.id === selected ? ' selected' : ''}>`
    + `${esc(n.name)}${num(n.grams) ? ` · ${nfmt(n.grams)} г` : ''}</option>`).join('');
  host.innerHTML = mixedRows.map((r, i) => `<div class="of-spool-row">`
    + `<select data-mixed-nom><option value="">— товар —</option>${options(r.nom_id)}</select>`
    + `<input type="number" min="1" step="1" data-mixed-qty value="${num(r.qty) || 1}" title="Сколько штук этого товара на одной плите" placeholder="шт">`
    + `<button class="icon-btn sm danger" type="button" data-mixed-del="${i}" title="Убрать">×</button></div>`).join('');
}
function collectMixedRows() {
  mixedRows = $$('#bf_item_rows .of-spool-row').map((row) => ({
    nom_id: (row.querySelector('[data-mixed-nom]') || {}).value || '',
    qty: Math.max(0, Math.round(num((row.querySelector('[data-mixed-qty]') || {}).value))),
  }));
  return mixedRows.filter((r) => r.nom_id && r.qty > 0);
}
function toggleBatchMode() {
  const mixed = $('bf_mode').value === 'mixed';
  $('bf_mixed_wrap').hidden = !mixed;
  $('bf_plates_wrap').hidden = !($('bf_mode').value === 'manual' || mixed);
  const nomField = $('bf_nom').closest('.field');
  const qtyField = $('bf_qty').closest('.field');
  if (nomField) nomField.hidden = mixed;
  if (qtyField) qtyField.hidden = mixed;
  if (mixed) renderMixedRows();
}

const KIND_LABEL = { product: 'Товар', semi: 'Полуфабрикат', kit: 'Комплект',
  material: 'Материал', service: 'Услуга' };
const STATUS_LABEL = { ok: 'В наличии', low: 'Мало', empty: 'Кончился',
  dead: 'Мёртвый сток', none: 'Только модель' };
const STATUS_CLASS = { ok: 'ok', low: 'warn', empty: 'bad', dead: 'bad', none: '' };
const DOC_KIND = { receipt: 'Приход', sale: 'Продажа', move: 'Перемещение',
  writeoff: 'Списание', inventory: 'Инвентаризация', production: 'Производство',
  return: 'Возврат', pricing: 'Установка цен' };

/* ============================================================== загрузка */
async function refresh() {
  try {
    const q = { warehouse_id: ($('prod_warehouse') || {}).value || '' };
    const res = await get('/api/nomenclature', q);
    data = {
      items: res.items || [], summary: res.summary || {}, groups: res.groups || [],
      warehouses: res.warehouses || [], priceTypes: res.price_types || [],
    };
    fillSelectors();
    if (document.querySelector('#view-products.on')) render();
    updateTags();
    PF.emit('nomenclature', data);
  } catch (e) { /* офлайн */ }
}
PF.refreshProducts = refresh;

async function refreshDocs() {
  try {
    const res = await get('/api/documents', {
      kind: ($('doc_filter_kind') || {}).value || '',
      state: ($('doc_filter_state') || {}).value || '',
      search: ($('doc_search') || {}).value || '',
    });
    docsData = res.documents || [];
    if (document.querySelector('#view-documents.on')) renderDocs();
    updateTags();
  } catch (e) { /* офлайн */ }
}

async function refreshBatches() {
  try {
    const state = (document.querySelector('#batch_filter button.on') || {}).dataset;
    const res = await get('/api/batches', { state: (state && state.state) || '' });
    batchData = res.batches || [];
    if (document.querySelector('#view-batches.on')) renderBatches();
    updateTags();
  } catch (e) { /* офлайн */ }
}

function updateTags() {
  const s = data.summary || {};
  const tag = $('nav_products_tag');
  if (tag) {
    const n = (s.low || 0) + (s.empty || 0) + (s.dead || 0);
    tag.hidden = !n;
    tag.textContent = String(n);
    tag.className = 'tag' + (s.empty || s.dead ? ' warn' : '');
  }
  const bt = $('nav_batches_tag');
  if (bt) {
    const active = batchData.filter((b) => b.state === 'printing' || b.state === 'planned').length;
    bt.hidden = !active;
    bt.textContent = String(active);
    bt.className = 'tag' + (batchData.some((b) => b.state === 'printing') ? ' live' : '');
  }
  const dt = $('nav_docs_tag');
  if (dt) {
    const drafts = docsData.filter((d) => d.state === 'draft').length;
    dt.hidden = !drafts;
    dt.textContent = String(drafts);
    dt.className = 'tag warn';
  }
}

function fillSelectors() {
  const whOpts = data.warehouses.map((w) =>
    `<option value="${esc(w.id)}">${esc(w.name)}</option>`).join('');
  const grpOpts = data.groups.map((g) =>
    `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('');
  const set = (id, html, keep) => {
    const el = $(id); if (!el) return;
    const prev = keep === undefined ? el.value : keep;
    el.innerHTML = html;
    if (prev && [...el.options].some((o) => o.value === prev)) el.value = prev;
  };
  set('prod_warehouse', '<option value="">Все склады</option>' + whOpts);
  set('prod_group', '<option value="">Все группы</option>' + grpOpts);
  set('turn_warehouse', '<option value="">Все склады</option>' + whOpts);
  set('nf_group_id', '<option value="">Без группы</option>' + grpOpts);
  set('bf_warehouse', whOpts);
  set('df_warehouse', whOpts);
  set('df_warehouse_to', whOpts);
  set('qs_warehouse', whOpts);
  const niches = (PF.state.niches || []).map((n) =>
    `<option value="${esc(n.id)}">${esc(n.name)}</option>`).join('');
  set('nf_niche_id', '<option value="">—</option>' + niches);
  const channels = (PF.state.channels || []).map((c) =>
    `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  set('df_channel', channels || '<option value="shop">Витрина</option>');
  set('qs_channel', channels || '<option value="shop">Витрина</option>');
  const accounts = (PF.state.accounts || []).map((a) =>
    `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
  set('df_account', accounts);
  set('qs_account', accounts);
  const ptypes = data.priceTypes.map((p) =>
    `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  set('df_price_type', ptypes);
  const noms = data.items.filter((i) => i.kind !== 'service').map((i) =>
    `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('');
  set('bf_nom', noms);
  const printers = (PF.state.printers || []).map((p) =>
    `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  set('bf_printer', '<option value="">Любой свободный</option>' + printers);
  const spools = (PF.state.spools || []).map((s) =>
    `<option value="${esc(s.id)}">${esc(s.material)} ${esc(s.color_name || '')} · ${Math.round(num(s.remaining_grams))} г</option>`).join('');
  set('bf_spool', '<option value="">Определить автоматически</option>' + spools);
}

/* ================================================================ товары */
function kpi(label, value, sub, kind) {
  return `<div class="kpi ${kind || ''}"><span class="label">${esc(label)}</span>`
    + `<b class="value">${value}</b><span class="sub">${esc(sub || '')}</span></div>`;
}

function filtered() {
  const q = (($('prod_search') || {}).value || '').toLowerCase().trim();
  const group = ($('prod_group') || {}).value || '';
  const kind = ($('prod_kind') || {}).value || '';
  const status = ($('prod_status') || {}).value || '';
  return data.items.filter((i) => {
    if (group && i.group_id !== group) return false;
    if (kind && i.kind !== kind) return false;
    if (status === 'unprofitable' && i.profitable !== false) return false;
    if (status && status !== 'unprofitable' && i.status !== status) return false;
    if (q) {
      const hay = `${i.name} ${i.sku || ''} ${i.code || ''} ${i.barcode || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function render() {
  const s = data.summary || {};
  $('prod_kpis').innerHTML = [
    kpi('Позиций', nfmt(s.goods), `${nfmt(s.items)} всего в справочнике`),
    kpi('На складах', `${nfmt(s.qty)} шт`, `${nfmt(s.reserved)} шт в резерве`),
    kpi('Запас в рублях', money(s.value), 'по себестоимости'),
    kpi('Продано за 7 дней', `${nfmt(s.sold_7)} шт`, money(s.sold_7_money)),
    kpi('Нужно напечатать', `${nfmt(s.plan_qty)} шт`, `${s.low || 0} мало · ${s.empty || 0} кончилось`,
      (s.low || s.empty) ? 'warn' : 'ok'),
    kpi('Мёртвый сток', String(s.dead || 0), s.dead_value ? `${money(s.dead_value)} заморожено` : 'нет',
      s.dead ? 'bad' : 'ok'),
  ].join('');

  const list = filtered();
  $('prod_grid').hidden = viewMode !== 'cards';
  $('prod_table').hidden = viewMode !== 'table';
  if (viewMode === 'cards') renderCards(list); else renderTable(list);
}

function renderCards(list) {
  $('prod_grid').innerHTML = list.length ? list.map((i) => {
    const st = i.status || 'ok';
    const cls = STATUS_CLASS[st] || '';
    const unprofit = i.profitable === false;
    return `<article class="prod-card ${st}" data-nom="${esc(i.id)}">`
      + '<div class="phead">'
      + (i.photo ? `<img class="pphoto" src="/api/nomenclature/photo.jpg?id=${esc(i.id)}&t=${esc(i.updated_at || '')}" alt="">`
        : `<span class="pphoto ph">${i.kind === 'kit' ? '◫' : i.kind === 'material' ? '◍' : '◻'}</span>`)
      + `<div class="pinfo"><h3>${esc(i.name)}</h3>`
      + `<small class="muted">${esc(i.code || '')}${i.sku ? ' · ' + esc(i.sku) : ''} · ${esc(KIND_LABEL[i.kind] || 'Товар')}</small></div>`
      + `<button class="icon-btn sm" type="button" data-nom-edit="${esc(i.id)}" title="Открыть карточку">✎</button></div>`
      + '<div class="pbody">'
      + `<div class="pqty ${cls}"><b>${nfmt(i.qty)}</b><span>${esc(i.unit || 'шт')}</span>`
      + (num(i.reserved) ? `<small class="muted">резерв ${nfmt(i.reserved)}</small>` : '')
      + '</div>'
      + '<div class="pfacts">'
      + `<span>Цена <b>${money(i.price)}</b></span>`
      + `<span>С/с <b>${money(i.cost)}</b></span>`
      + `<span>Маржа <b class="${num(i.margin) >= 0 ? 'pos' : 'neg'}">${money(i.margin)}</b></span>`
      + `<span>₽/час <b class="${unprofit ? 'neg' : 'pos'}">${num(i.hours) ? money(i.profit_per_hour) : '—'}</b></span>`
      + `<span>7 дн. <b>${nfmt(i.sold_7)} шт</b></span>`
      + (i.days_left != null ? `<span>Хватит <b>${nfmt(i.days_left, 1)} дн.</b></span>`
        : '<span class="muted">продаж нет</span>')
      + '</div></div>'
      + '<div class="pacts">'
      + `<span class="chip ${cls}">${esc(STATUS_LABEL[st] || st)}</span>`
      + (unprofit ? '<span class="chip bad" title="Прибыль за час печати ниже нормы">убыточный</span>' : '')
      + '<span class="spacer"></span>'
      + (num(i.plan_qty) ? `<span class="plan-hint">напечатать ${nfmt(i.plan_qty)}</span>` : '')
      + `<button class="btn sm" type="button" data-nom-recalc="${esc(i.id)}" title="Пересчитать цену только этого товара">↻</button>`
      + `<button class="btn sm" type="button" data-nom-batch="${esc(i.id)}" title="Напечатать партию">⎙</button>`
      + `<button class="btn sm" type="button" data-nom-sell="${esc(i.id)}" title="Продать 1 шт">−1</button>`
      + '</div></article>';
  }).join('') : emptyBox('▩', 'Номенклатура пуста',
    'Добавьте товар — модель, нормативы печати и цену. Остальное система посчитает сама.');
}

function renderTable(list) {
  $('prod_tbody').innerHTML = list.length ? list.map((i) => {
    const group = data.groups.find((g) => g.id === i.group_id);
    const unprofit = i.profitable === false;
    return `<tr class="clickable" data-nom-edit="${esc(i.id)}">`
      + `<td class="tnum muted">${esc(i.code || '')}</td>`
      + `<td class="strong">${esc(i.name)}${i.sku ? `<small class="muted"> · ${esc(i.sku)}</small>` : ''}</td>`
      + `<td>${esc(group ? group.name : '—')}</td>`
      + `<td class="right tnum">${nfmt(i.qty)}</td>`
      + `<td class="right tnum ${num(i.reserved) ? 'warn-text' : 'muted'}">${nfmt(i.reserved)}</td>`
      + `<td class="right tnum">${nfmt(i.free)}</td>`
      + `<td class="right tnum">${money(i.price)}</td>`
      + `<td class="right tnum">${money(i.cost)}</td>`
      + `<td class="right tnum ${num(i.margin) >= 0 ? 'pos' : 'neg'}">${money(i.margin)}</td>`
      + `<td class="right tnum ${unprofit ? 'neg' : ''}">${num(i.hours) ? money(i.profit_per_hour) : '—'}</td>`
      + `<td class="right tnum">${nfmt(i.sold_7)}</td>`
      + `<td><span class="chip ${STATUS_CLASS[i.status] || ''}">${esc(STATUS_LABEL[i.status] || i.status)}</span></td>`
      + `<td class="right"><button class="btn sm" type="button" data-nom-recalc="${esc(i.id)}" title="Пересчитать цену этого товара">↻</button> <button class="btn sm" type="button" data-nom-batch="${esc(i.id)}">⎙</button></td></tr>`;
  }).join('') : `<tr><td colspan="13">${emptyBox('▩', 'Ничего не найдено', 'Измените фильтры или добавьте товар.')}</td></tr>`;
}

function emptyBox(icon, title, text) {
  return `<div class="empty" style="grid-column:1/-1"><span class="big">${icon}</span>`
    + `<b>${esc(title)}</b><span>${esc(text)}</span></div>`;
}

/* ======================================================= карточка товара */
function renderNomSummary(item) {
  const host = $('nf_card_summary');
  if (!host) return;
  if (!item || !item.id) {
    host.className = 'notice info';
    host.innerHTML = '<span>ⓘ</span><span>Заполните и сохраните карточку — сводка появится после расчёта нормативов.</span>';
    return;
  }
  const low = item.status === 'low' || item.status === 'out';
  const unprofitable = item.profitable === false;
  const price = num(item.price);
  const cost = num(item.cost);
  const margin = price - cost;
  const statusText = STATUS_LABEL[item.status] || item.status || 'без статуса';
  host.className = `notice ${low || unprofitable ? 'warn' : 'ok'}`;
  host.innerHTML = '<span>' + (low || unprofitable ? '⚠' : '✓') + '</span><span>'
    + `<b>Цена ${money(price)}</b> · с/с ${money(cost)} · маржа ${money(margin)}`
    + (price ? ` (${nfmt(margin / price * 100, 1)}%)` : '')
    + ` · прибыль/час ${num(item.hours) ? money(item.profit_per_hour) : '—'}`
    + ` · запас: ${nfmt(item.free)} ${esc(item.unit || 'шт')}`
    + (num(item.reserved) ? `, резерв ${nfmt(item.reserved)}` : '')
    + ` · ${esc(statusText)}`
    + (item.days_left != null ? ` · хватит на ${nfmt(item.days_left, 1)} дн.` : '')
    + (unprofitable ? '<br><small>Цена ниже целевой прибыли за час — используйте пересчёт или проверьте нормативы.</small>' : '')
    + '</span>';
}

function nomRecalcId(nomId) {
  // addEventListener передаёт PointerEvent первым аргументом — его нельзя
  // отправлять как nom_id, иначе сервер ищет «[object PointerEvent]».
  return typeof nomId === 'string' && nomId.trim() ? nomId.trim() : '';
}

async function recalcNomPrices(nomId) {
  const targetId = nomRecalcId(nomId) || editingNom;
  if (!targetId) return fail(new Error('Сначала сохраните товар'));
  if (!confirmDanger('Пересчитать цены только для этого товара? Старые значения останутся в истории.')) return;
  try {
    const result = await post('/api/nomenclature/recalc-price', { nom_id: targetId });
    const changed = Object.values(result.prices || {}).filter((p) => p.changed).length;
    const currentPane = [...$$('#nom_tabs button')].find((b) => b.classList.contains('on'))?.dataset.pane || 'price';
    await refresh();
    if (editingNom === targetId && $('nom_modal')?.open) {
      await openNom(targetId);
      switchPane('nom_tabs', 'nompane', currentPane);
    }
    toast(changed ? 'Цена товара пересчитана' : 'Цена товара уже актуальна',
      changed ? `Изменено типов цен: ${changed}` : (result.reason || 'Изменений нет'));
  } catch (e) { fail(e); }
}

async function openNom(id) {
  editingNom = id || null;
  let item = null;
  if (id) {
    try { item = await get('/api/nomenclature/item', { id }); } catch (e) { return fail(e); }
  }
  const d = item || { name: '', kind: 'product', unit: 'шт', fit_per_plate: 1, code: '' };
  editingNomUpdatedAt = id ? String(d.updated_at || '') : '';
  const setv = (key, value) => { const el = $('nf_' + key); if (el) el.value = value ?? ''; };
  ['name', 'code', 'sku', 'barcode', 'kind', 'unit', 'min_qty', 'max_qty', 'vat', 'note',
    'material', 'grams', 'hours', 'fit_per_plate', 'post_minutes', 'file',
    'model_url', 'license'].forEach((k) => setv(k, d[k]));
  fillSelectors();
  $('nf_group_id').value = d.group_id || '';
  $('nf_niche_id').value = d.niche_id || '';
  $('nf_marked').checked = !!num(d.marked);
  const img = $('nf_photo_preview');
  img.hidden = !d.photo;
  if (d.photo) img.src = `/api/nomenclature/photo.jpg?id=${esc(d.id)}&t=${esc(d.updated_at || '')}`;
  $('nf_photo_file').value = '';

  // цены
  $('nf_prices').innerHTML = data.priceTypes.map((p) => {
    const value = (d.prices || {})[p.id];
    return `<div class="price-row"><span class="lbl">${esc(p.name)}`
      + (num(p.markup) ? `<small class="muted"> · наценка ${nfmt(p.markup)}%</small>` : '')
      + `</span><input type="number" min="0" step="any" data-price="${esc(p.id)}" value="${value != null ? esc(value) : ''}" placeholder="0"></div>`;
  }).join('');
  $('nf_price_history').innerHTML = (d.price_history || []).length
    ? d.price_history.map((h) => `<div class="tx-row"><span class="tx-ic income">₽</span>`
      + `<div class="tx-body"><b>${money(h.price)}</b><small>${esc(h.type_name || '')} · ${esc(dateTimeText(h.at))}${h.note ? ' · ' + esc(h.note) : ''}</small></div></div>`).join('')
    : '<div class="empty compact"><span>Цены ещё не устанавливались.</span></div>';

  // состав
  specRows = ((d.spec || {}).items || []).map((s) => ({
    nom_id: s.nom_id, qty: num(s.qty, 1), name: s.nom_name }));
  renderSpec();

  // остатки
  const wh = d.warehouses || [];
  $('nf_stock_box').innerHTML = (id ? (wh.length
    ? '<div class="wh-rows">' + wh.map((w) => `<div class="wh-row"><span>${esc(w.name)}</span>`
      + `<b>${nfmt(w.qty)}</b><small class="muted">${money(w.value)}</small></div>`).join('') + '</div>'
    : '<div class="empty compact"><span>Остатков нет ни на одном складе.</span></div>')
    : '<div class="empty compact"><span>Сохраните карточку, чтобы вести остатки.</span></div>')
    + (d.fact && d.fact.batches ? `<div class="notice" style="margin-top:10px"><span>ⓘ</span><span>`
      + `Партий: ${d.fact.batches} · выпущено ${nfmt(d.fact.produced)} шт · брак ${nfmt(d.fact.scrap)} шт (${nfmt(d.fact.scrap_pct, 1)}%)`
      + `</span></div>` : '');
  $('nf_moves').innerHTML = (d.moves || []).length
    ? d.moves.slice(0, 30).map(moveRow).join('')
    : '<div class="empty compact"><span>Движений пока нет.</span></div>';

  $('nom_modal_title').textContent = id ? d.name : 'Новый товар';
  $('nom_modal_sub').textContent = id
    ? `${d.code || ''} · остаток ${nfmt(d.qty)} ${d.unit || 'шт'} · себестоимость ${money(d.cost)}`
    : 'Нормативы производства, цены и остатки';
  renderNomSummary(d);
  $('nom_delete').hidden = !id;
  $('nom_batch').hidden = !id;
  switchPane('nom_tabs', 'nompane', 'main');
  openModal('nom_modal');
  updateNomCost();
}

const updateNomCost = debounce(async () => {
  const hint = $('nf_norm_hint');
  if (!hint) return;
  const grams = num(($('nf_grams') || {}).value);
  const hours = num(($('nf_hours') || {}).value);
  const fit = Math.max(1, Math.round(num(($('nf_fit_per_plate') || {}).value, 1)) || 1);
  const post = num(($('nf_post_minutes') || {}).value);
  const material = (($('nf_material') || {}).value || '').trim();
  if (!grams && !hours) {
    hint.innerHTML = '<span>ⓘ</span><span>Нормативы используются калькулятором партии: они определяют плиты, пластик и время.</span>';
    return;
  }
  try {
    const payload = {
      grams, hours, material,
      manual_minutes: post,
      fit_per_plate: fit,
      qty: 1,
    };
    // Нормативы карточки — на штуку: считаем полную плиту и берём с/с штуки.
    if (grams > 0 && hours > 0) {
      payload.plate_grams = grams * fit;
      payload.plate_hours = hours * fit;
      payload.qty = fit;
      payload.warmup_minutes = 0;
    }
    const br = await post('/api/calc/cost', payload);
    const cost = num(br.per_unit) || num(br.total);
    hint.innerHTML = `<span>ⓘ</span><span>Себестоимость ≈ <b>${money(cost)}</b>/шт`
      + (material ? ` · ${esc(material)}` : '')
      + '. Сохранится в карточке при записи.</span>';
    const retail = $('nf_prices') && $('nf_prices').querySelector('[data-price="retail"]');
    if (retail && retail.value === '' && cost > 0) {
      try {
        const pr = await post('/api/calc/price', { cost });
        if (num(pr.price)) retail.placeholder = String(pr.price);
      } catch (e) { /* цена — подсказка */ }
    }
  } catch (e) { /* офлайн — подсказку не ломаем */ }
}, 300);

function moveRow(m) {
  const positive = num(m.qty) > 0;
  return '<div class="tx-row">'
    + `<span class="tx-ic ${positive ? 'income' : 'expense'}">${positive ? '↑' : '↓'}</span>`
    + `<div class="tx-body"><b>${esc(DOC_KIND[m.doc_kind] || m.doc_kind || 'Движение')}`
    + (m.doc_number ? ` · ${esc(m.doc_number)}` : '') + '</b>'
    + `<small>${esc(dateTimeText(m.at))} · ${esc(m.warehouse_name || '')}${m.note ? ' · ' + esc(m.note) : ''}</small></div>`
    + `<span class="amt ${positive ? 'pos' : 'neg'}">${positive ? '+' : ''}${nfmt(m.qty)}</span></div>`;
}

function renderSpec() {
  const opts = data.items.map((i) => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('');
  $('nf_spec_rows').innerHTML = specRows.length ? specRows.map((r, index) =>
    `<div class="spec-row" data-spec-row="${index}">`
    + `<select data-spec-nom="${index}"><option value="">— выберите —</option>${opts}</select>`
    + `<input type="number" min="0" step="any" data-spec-qty="${index}" value="${esc(r.qty)}">`
    + `<button class="icon-btn sm danger" type="button" data-spec-del="${index}">×</button></div>`).join('')
    : '<div class="empty compact"><span>Состав не задан — изделие печатается целиком.</span></div>';
  specRows.forEach((r, index) => {
    const sel = $('nf_spec_rows').querySelector(`[data-spec-nom="${index}"]`);
    if (sel) sel.value = r.nom_id || '';
  });
}

async function saveNom() {
  const payload = {
    id: editingNom || '',
    ...(editingNomUpdatedAt ? { expected_updated_at: editingNomUpdatedAt } : {}),
    name: $('nf_name').value.trim(),
    sku: $('nf_sku').value.trim(), barcode: $('nf_barcode').value.trim(),
    kind: $('nf_kind').value, unit: $('nf_unit').value.trim() || 'шт',
    group_id: $('nf_group_id').value, niche_id: $('nf_niche_id').value,
    material: $('nf_material').value.trim(),
    grams: num($('nf_grams').value), hours: num($('nf_hours').value),
    fit_per_plate: Math.max(1, num($('nf_fit_per_plate').value, 1)),
    post_minutes: num($('nf_post_minutes').value),
    file: $('nf_file').value.trim(), model_url: $('nf_model_url').value.trim(),
    license: $('nf_license').value.trim(),
    min_qty: num($('nf_min_qty').value), max_qty: num($('nf_max_qty').value),
    vat: num($('nf_vat').value), marked: $('nf_marked').checked ? 1 : 0,
    note: $('nf_note').value.trim(),
    prices: {},
  };
  if (!payload.name) return fail(new Error('Укажите наименование'));
  $$('[data-price]', $('nf_prices')).forEach((el) => {
    if (el.value !== '') payload.prices[el.dataset.price] = num(el.value);
  });
  try {
    const res = await post('/api/nomenclature/save', payload);
    editingNomUpdatedAt = String(res.item.updated_at || '');
    const id = res.item.id;
    // состав
    const rows = specRows.filter((r) => r.nom_id);
    if (rows.length) await post('/api/spec/save', { nom_id: id, items: rows });
    // фото
    const file = $('nf_photo_file').files[0];
    if (file) {
      const reader = new FileReader();
      await new Promise((resolve) => {
        reader.onload = async () => {
          try { await post('/api/nomenclature/photo', { id, data: reader.result }); }
          catch (e) { /* фото не критично */ }
          resolve();
        };
        reader.readAsDataURL(file);
      });
    }
    closeModal('nom_modal');
    await refresh();
    toast('Сохранено', payload.name);
  } catch (e) { fail(e); }
}

/* ============================================================== партии */
function renderBatches() {
  const active = batchData.filter((b) => b.state === 'printing' || b.state === 'planned');
  const done = batchData.filter((b) => b.state === 'done');
  const planned = active.reduce((sum, b) => sum + num(b.qty_planned) - num(b.qty_done), 0);
  const scrap = batchData.reduce((sum, b) => sum + num(b.qty_scrap), 0);
  $('batch_kpis').innerHTML = [
    kpi('Активных партий', String(active.length), `${nfmt(planned)} шт осталось напечатать`),
    kpi('Готово', String(done.length), `${nfmt(done.reduce((s, b) => s + num(b.qty_done), 0))} шт выпущено`),
    kpi('Брак', `${nfmt(scrap)} шт`, scrap ? 'проверьте модель и профиль' : 'брака нет', scrap ? 'warn' : 'ok'),
    kpi('Себестоимость выпуска', money(batchData.reduce((s, b) => s + num(b.cost), 0)), 'по завершённым партиям'),
  ].join('');
  $('batch_sub').textContent = `${batchData.length} партий`;

  $('batch_list').innerHTML = batchData.length ? batchData.map((b) => {
    const cls = { planned: '', printing: 'accent', partial: 'warn', done: 'ok', cancelled: '' }[b.state] || '';
    const label = { planned: 'План', printing: 'Печатает', partial: 'Частично',
      done: 'Готова', cancelled: 'Отменена' }[b.state] || b.state;
    const rest = Math.max(0, num(b.qty_planned) - num(b.qty_done));
    const mixedList = (b.items_list || []);
    const mixedChips = mixedList.length
      ? `<div style="display:flex;flex-wrap:wrap;gap:5px;margin:6px 0">` + mixedList.map((r) =>
          `<span class="chip outline">${esc(r.name || '')} ×${nfmt(r.qty_per_plate)}/плита</span>`).join('') + `</div>`
      : '';
    return `<div class="batch-item ${b.state}">`
      + `<div class="bhead"><b>${esc(b.nom_name || b.name)}</b>`
      + `<span class="chip ${cls}">${esc(label)}</span>`
      + `<small class="muted">${esc(b.number || '')} · ${esc(dateTimeText(b.at))}</small></div>`
      + mixedChips
      + `<div class="bbar"><i style="width:${Math.min(100, num(b.progress))}%"></i></div>`
      + '<div class="bfacts">'
      + `<span>Готово <b>${nfmt(b.qty_done)} / ${nfmt(b.qty_planned)} шт</b></span>`
      + `<span>Запусков <b>${nfmt(b.plates)}</b></span>`
      + (num(b.qty_scrap) ? `<span>Брак <b class="neg">${nfmt(b.qty_scrap)} шт</b></span>` : '')
      + (num(b.cost) ? `<span>С/с <b>${money(b.cost)}</b></span>` : '')
      + `<span>Склад <b>${esc(b.warehouse_name || '—')}</b></span>`
      + '</div>'
      + '<div class="bacts">'
      + (rest > 0 && b.state !== 'cancelled'
        ? `<button class="btn sm" type="button" data-batch-receive="${esc(b.id)}">✓ Принять</button>` : '')
      + (b.state === 'partial' || b.state === 'done'
        ? `<button class="btn sm" type="button" data-batch-repeat="${esc(b.id)}">↻ Повторить</button>` : '')
      + (b.state === 'planned' || b.state === 'printing'
        ? `<button class="btn sm danger" type="button" data-batch-cancel="${esc(b.id)}">Отменить</button>` : '')
      + '</div></div>';
  }).join('') : emptyBox('⎙', 'Партий пока нет',
    'Нажмите «Напечатать партию» — система разложит нужное количество на запуски принтера.');
}

async function openBatch(nomId) {
  fillSelectors();
  if (nomId) $('bf_nom').value = nomId;
  const item = data.items.find((i) => i.id === ($('bf_nom').value));
  $('bf_qty').value = item && num(item.plan_qty) ? Math.round(num(item.plan_qty)) : 10;
  $('bf_mode').value = 'full';
  $('bf_note').value = '';
  $('bf_start').checked = false;
  $('bf_file').value = '';
  mixedRows = [{ nom_id: '', qty: 1 }];
  toggleBatchMode();
  const retail = data.warehouses.find((w) => num(w.retail));
  if (retail) $('bf_warehouse').value = retail.id;
  openModal('batch_modal');
  runPlan();
}

const runPlan = debounce(async () => {
  if ($('bf_mode').value === 'mixed') return runMixedPlan();
  const nom_id = $('bf_nom').value;
  if (!nom_id) return;
  try {
    const res = await post('/api/batch/plan', {
      nom_id, qty: num($('bf_qty').value), mode: $('bf_mode').value,
      plates: num($('bf_plates').value), printer_id: $('bf_printer').value,
      spool_id: $('bf_spool').value,
    });
    batchPlan = res;
    const c = res.cost || {};
    $('bf_calc_sub').textContent =
      `${res.plates} запуск(ов) × ${res.fit_per_plate} шт = ${res.qty_real} шт`
      + (res.qty_extra > 0 ? ` (сверх плана ${nfmt(res.qty_extra)})` : '');
    $('bf_calc_rows').innerHTML = [
      ['Время печати', hoursText(res.hours)],
      ['Финиш примерно', res.eta ? dateTimeText(res.eta) : '—'],
      ['Пластик', `${nfmt(res.grams)} г`],
      ['Себестоимость партии', money(c.total, 2)],
      ['За штуку', money(res.cost_per_unit, 2)],
      ['Цена продажи', money(res.price)],
      ['Выручка', money(res.revenue)],
      ['Прибыль', money(res.profit)],
      ['Прибыль за час печати', res.hours ? money(res.profit_per_hour) : '—'],
    ].map(([l, v]) => `<div class="res-row"><span class="lbl">${esc(l)}</span><span class="val">${v}</span></div>`).join('');

    const verdict = $('bf_verdict');
    const map = {
      ok: ['verdict ok', `<b>Выгодно.</b> ${money(res.profit_per_hour)} чистыми за час печати при норме ${money(res.target_per_hour)}.`],
      warn: ['verdict warn', `<b>Слабовато.</b> ${money(res.profit_per_hour)} за час против нормы ${money(res.target_per_hour)}. Поднимите цену или печатайте большей партией.`],
      bad: ['verdict bad', `<b>Невыгодно.</b> Всего ${money(res.profit_per_hour)} за час работы принтера.`],
      unknown: ['verdict', 'Заполните вес и время печати в карточке товара.'],
    };
    const [cls, html] = map[res.verdict] || map.unknown;
    verdict.className = cls;
    verdict.innerHTML = html;
    $('bf_warnings').innerHTML = (res.warnings || []).map((w) =>
      `<div class="notice ${w.level === 'bad' ? 'bad' : w.level === 'info' ? '' : 'warn'}">`
      + `<span>${w.level === 'bad' ? '✕' : w.level === 'info' ? 'ⓘ' : '⚠'}</span><span>${esc(w.text)}</span></div>`).join('');
  } catch (e) {
    $('bf_calc_rows').innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
  }
}, 300);

async function runMixedPlan() {
  const items = collectMixedRows();
  if (!items.length) {
    $('bf_calc_rows').innerHTML = '';
    $('bf_calc_sub').textContent = 'добавьте товары в состав плиты';
    $('bf_verdict').className = 'verdict';
    $('bf_verdict').innerHTML = 'Выберите хотя бы один товар и количество.';
    $('bf_warnings').innerHTML = '';
    return;
  }
  try {
    const res = await post('/api/batch/plan', {
      items, plates: num($('bf_plates').value, 1), file: $('bf_file').value.trim(),
      printer_id: $('bf_printer').value, spool_id: $('bf_spool').value,
    });
    batchPlan = res;
    const c = res.cost || {};
    $('bf_calc_sub').textContent =
      `${res.plates} плит × ${res.units_per_plate} шт = ${res.qty_real} шт (${res.items.length} товар(ов))`;
    const rows = res.items.map((r) =>
      `<div class="res-row"><span class="lbl">${esc(r.name)} ×${r.qty_per_plate}/плита</span>`
      + `<span class="val">${nfmt(r.grams)} г${r.hours ? ` · ${hoursText(r.hours)}` : ''}</span></div>`).join('');
    $('bf_calc_rows').innerHTML = rows + [
      ['Время печати всего', hoursText(res.hours)],
      ['Финиш примерно', res.eta ? dateTimeText(res.eta) : '—'],
      ['Пластик', `${nfmt(res.grams)} г`],
      ['Себестоимость партии', money(c.total, 2)],
      ['Выручка', money(res.revenue)],
      ['Прибыль', money(res.profit)],
      ['Прибыль за час печати', res.hours ? money(res.profit_per_hour) : '—'],
    ].map(([l, v]) => `<div class="res-row"><span class="lbl">${esc(l)}</span><span class="val">${v}</span></div>`).join('');
    const verdict = $('bf_verdict');
    const map = {
      ok: ['verdict ok', `<b>Выгодно.</b> ${money(res.profit_per_hour)} чистыми за час печати при норме ${money(res.target_per_hour)}.`],
      warn: ['verdict warn', `<b>Слабовато.</b> ${money(res.profit_per_hour)} за час против нормы ${money(res.target_per_hour)}.`],
      bad: ['verdict bad', `<b>Невыгодно.</b> Всего ${money(res.profit_per_hour)} за час работы принтера.`],
      unknown: ['verdict', 'Заполните время печати в карточках товаров.'],
    };
    const [cls, html] = map[res.verdict] || map.unknown;
    verdict.className = cls;
    verdict.innerHTML = html;
    $('bf_warnings').innerHTML = (res.warnings || []).map((w) =>
      `<div class="notice ${w.level === 'bad' ? 'bad' : w.level === 'info' ? '' : 'warn'}">`
      + `<span>${w.level === 'bad' ? '✕' : w.level === 'info' ? 'ⓘ' : '⚠'}</span><span>${esc(w.text)}</span></div>`).join('');
  } catch (e) {
    $('bf_calc_rows').innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
  }
}

async function createBatch() {
  const startNow = $('bf_start').checked;
  if (startNow && !confirmDanger('Партия будет физически запущена сразу после создания. Пройти Preflight и начать печать?')) return;
  if ($('bf_mode').value === 'mixed') {
    const items = collectMixedRows();
    if (!items.length) return fail(new Error('Добавьте хотя бы один товар в состав плиты'));
    if (!$('bf_file').value.trim()) return fail(new Error('Укажите файл плиты на принтере'));
    try {
      const res = await post('/api/batch/create', {
        items, plates: num($('bf_plates').value, 1), file: $('bf_file').value.trim(),
        warehouse_id: $('bf_warehouse').value,
        printer_id: $('bf_printer').value, spool_id: $('bf_spool').value,
        priority: num($('bf_priority').value), note: $('bf_note').value.trim(),
        start_now: startNow, confirmed: startNow,
        preflight_acknowledged: startNow,
      });
      closeModal('batch_modal');
      await Promise.all([refreshBatches(), refresh()]);
      PF.refreshCore && PF.refreshCore();
      toast('Смешанная партия создана', `${res.batch.number} · ${nfmt(res.batch.qty_planned)} шт · ${nfmt(res.batch.plates)} плиты`);
    } catch (e) { fail(e); }
    return;
  }
  const nom_id = $('bf_nom').value;
  if (!nom_id) return fail(new Error('Выберите товар'));
  try {
    const res = await post('/api/batch/create', {
      nom_id, qty: num($('bf_qty').value), mode: $('bf_mode').value,
      plates: num($('bf_plates').value), warehouse_id: $('bf_warehouse').value,
      printer_id: $('bf_printer').value, spool_id: $('bf_spool').value,
      priority: num($('bf_priority').value), note: $('bf_note').value.trim(),
      start_now: startNow, confirmed: startNow,
      preflight_acknowledged: startNow,
    });
    closeModal('batch_modal');
    await Promise.all([refreshBatches(), refresh()]);
    PF.refreshCore && PF.refreshCore();
    toast('Партия создана', `${res.batch.number} · ${nfmt(res.batch.qty_planned)} шт`);
  } catch (e) { fail(e); }
}

/* ====================================================== план пополнения */
async function openPlan() {
  try {
    const res = await get('/api/replenishment', {
      warehouse_id: ($('prod_warehouse') || {}).value || '' });
    planRows = res.rows || [];
    if (!planRows.length) {
      return toast('Всё в порядке', 'Дефицита нет — печатать нечего', 'ok');
    }
    $('plan_rows').innerHTML = planRows.map((r, index) =>
      `<div class="plan-row"><label class="check"><input type="checkbox" data-plan-on="${index}" checked></label>`
      + `<div class="pinfo"><b>${esc(r.name)}</b>`
      + `<small class="muted">на складе ${nfmt(r.qty)} шт`
      + (r.days_left != null ? ` · хватит на ${nfmt(r.days_left, 1)} дн.` : '')
      + ` · ${r.plates} запуск(ов) · ${hoursText(r.hours)}</small></div>`
      + `<input type="number" min="1" step="1" data-plan-qty="${index}" value="${Math.round(num(r.qty_real) || num(r.plan_qty))}"></div>`).join('');
    updatePlanTotal();
    openModal('plan_modal');
  } catch (e) { fail(e); }
}

function updatePlanTotal() {
  let qty = 0, hours = 0;
  planRows.forEach((r, index) => {
    const on = $('plan_rows').querySelector(`[data-plan-on="${index}"]`);
    if (!on || !on.checked) return;
    const input = $('plan_rows').querySelector(`[data-plan-qty="${index}"]`);
    const n = num(input && input.value);
    qty += n;
    hours += num(r.hours) / Math.max(1, num(r.qty_real)) * n;
  });
  $('plan_total').textContent = `Итого ${nfmt(qty)} шт · ${hoursText(hours)} печати`;
}

async function createFromPlan() {
  const rows = [];
  planRows.forEach((r, index) => {
    const on = $('plan_rows').querySelector(`[data-plan-on="${index}"]`);
    if (!on || !on.checked) return;
    const input = $('plan_rows').querySelector(`[data-plan-qty="${index}"]`);
    const qty = num(input && input.value);
    if (qty > 0) rows.push({ nom_id: r.nom_id, qty });
  });
  if (!rows.length) return fail(new Error('Не выбрано ни одной позиции'));
  try {
    const res = await post('/api/batch/from-plan', {
      rows, warehouse_id: ($('prod_warehouse') || {}).value || '' });
    closeModal('plan_modal');
    await Promise.all([refreshBatches(), refresh()]);
    PF.refreshCore && PF.refreshCore();
    toast('Партии созданы', `${(res.batches || []).length} шт поставлено в очередь`);
  } catch (e) { fail(e); }
}

/* ============================================================ документы */
function renderDocs() {
  $('doc_tbody').innerHTML = docsData.length ? docsData.map((d) => {
    const posted = d.state === 'posted';
    return `<tr class="clickable" data-doc="${esc(d.id)}">`
      + `<td class="strong tnum">${esc(d.number || '')}</td>`
      + `<td>${esc(DOC_KIND[d.kind] || d.kind)}</td>`
      + `<td>${esc(dateTimeText(d.at))}</td>`
      + `<td>${esc(d.warehouse_name || '—')}${d.warehouse_to_name ? ' → ' + esc(d.warehouse_to_name) : ''}</td>`
      + `<td class="right tnum">${nfmt(d.lines)}</td>`
      + `<td class="right tnum">${nfmt(d.qty_total)}</td>`
      + `<td class="right tnum">${num(d.amount) ? money(d.amount) : '—'}</td>`
      + `<td><span class="chip ${posted ? 'ok' : 'warn'}">${posted ? 'Проведён' : 'Черновик'}</span></td>`
      + `<td class="right"><button class="icon-btn sm" type="button" data-doc-open="${esc(d.id)}">→</button></td></tr>`;
  }).join('') : `<tr><td colspan="9">${emptyBox('▤', 'Документов нет', 'Создайте приход, продажу или инвентаризацию.')}</td></tr>`;
}

function docKindSetup(kind) {
  const show = (id, on) => { const el = $(id); if (el) el.hidden = !on; };
  show('df_wh2_wrap', kind === 'move');
  show('df_channel_wrap', kind === 'sale');
  show('df_account_wrap', kind === 'sale' || kind === 'return');
  show('df_ptype_wrap', kind === 'pricing');
  show('df_reason_wrap', kind === 'writeoff' || kind === 'return');
  show('df_th_fact', kind === 'inventory');
  const priceTh = $('df_th_price');
  if (priceTh) priceTh.textContent = (kind === 'receipt' || kind === 'production') ? 'Себест.' : 'Цена';
  $('df_wh_label').textContent = kind === 'move' ? 'Склад-источник' : 'Склад';
  $('doc_modal_kind').textContent = DOC_KIND[kind] || 'Документ';
  $('df_items_sub').textContent = kind === 'inventory'
    ? 'Введите фактическое количество — расхождение спишется или оприходуется'
    : 'Позиции документа';
}

async function openDoc(id, kind) {
  editingDoc = id || null;
  fillSelectors();
  let doc = null;
  if (id) {
    try { doc = await get('/api/document', { id }); } catch (e) { return fail(e); }
  }
  const k = (doc && doc.kind) || kind || 'receipt';
  docKindSetup(k);
  $('doc_modal').dataset.kind = k;
  $('df_number').value = (doc && doc.number) || 'присвоится при записи';
  const at = doc && doc.at ? new Date(doc.at) : new Date();
  $('df_at').value = new Date(at.getTime() - at.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 16);
  if (doc) {
    $('df_warehouse').value = doc.warehouse_id || '';
    $('df_warehouse_to').value = doc.warehouse_to_id || '';
    $('df_channel').value = doc.channel || '';
    $('df_account').value = doc.account_id || '';
    $('df_price_type').value = doc.price_type_id || '';
    $('df_reason').value = doc.reason || '';
    $('df_note').value = doc.note || '';
  } else {
    const retail = data.warehouses.find((w) => num(w.retail));
    if (retail) $('df_warehouse').value = retail.id;
    $('df_reason').value = '';
    $('df_note').value = '';
  }
  docRows = ((doc && doc.items) || []).map((i) => ({
    nom_id: i.nom_id, qty: num(i.qty), qty_fact: num(i.qty_fact),
    price: num(i.price), cost: num(i.cost) }));
  if (!docRows.length) docRows = [{ nom_id: '', qty: 1, price: 0, qty_fact: 0 }];
  renderDocRows();

  const posted = doc && doc.state === 'posted';
  $('doc_modal_title').textContent = doc
    ? `${DOC_KIND[k]} ${doc.number}` : `Новый документ: ${DOC_KIND[k]}`;
  $('doc_modal_sub').textContent = posted
    ? 'Документ проведён — движения выполнены. Для правки отмените проведение.'
    : 'Черновик не меняет остатки — движения появятся при проведении';
  $('doc_post').hidden = !!posted;
  $('doc_save').hidden = !!posted;
  $('doc_unpost').hidden = !posted;
  $('doc_delete').hidden = !id || posted;
  $$('#doc_modal input, #doc_modal select').forEach((el) => {
    if (el.id !== 'df_number') el.disabled = !!posted;
  });
  $('df_moves_box').innerHTML = posted && (doc.moves || []).length
    ? '<div class="card-head" style="margin-top:12px"><div><h3>Движения по регистру</h3>'
      + '<p>Что документ сделал со складом</p></div></div>'
      + doc.moves.map(moveRow).join('')
    : '';
  openModal('doc_modal');
}

function renderDocRows() {
  const kind = $('doc_modal').dataset.kind || 'receipt';
  const opts = data.items.map((i) =>
    `<option value="${esc(i.id)}">${esc(i.name)}${i.code ? ' · ' + esc(i.code) : ''}</option>`).join('');
  $('df_tbody').innerHTML = docRows.map((r, index) =>
    `<tr><td><select data-row-nom="${index}"><option value="">— выберите —</option>${opts}</select></td>`
    + `<td class="right"><input type="number" min="0" step="any" data-row-qty="${index}" value="${esc(r.qty)}"></td>`
    + (kind === 'inventory'
      ? `<td class="right"><input type="number" min="0" step="any" data-row-fact="${index}" value="${esc(r.qty_fact)}"></td>` : '')
    + `<td class="right"><input type="number" min="0" step="any" data-row-price="${index}" value="${esc(r.price)}"></td>`
    + `<td class="right tnum" data-row-sum="${index}">${money(num(r.qty) * num(r.price))}</td>`
    + `<td class="right"><button class="icon-btn sm danger" type="button" data-row-del="${index}">×</button></td></tr>`).join('');
  docRows.forEach((r, index) => {
    const sel = $('df_tbody').querySelector(`[data-row-nom="${index}"]`);
    if (sel) sel.value = r.nom_id || '';
  });
  updateDocTotal();
}

function updateDocTotal() {
  const qty = docRows.reduce((s, r) => s + num(r.qty), 0);
  const sum = docRows.reduce((s, r) => s + num(r.qty) * num(r.price), 0);
  $('df_total').innerHTML = `<span>Строк: <b>${docRows.length}</b></span>`
    + `<span>Количество: <b>${nfmt(qty)}</b></span>`
    + `<span>Сумма: <b>${money(sum)}</b></span>`;
}

function docPayload() {
  const kind = $('doc_modal').dataset.kind || 'receipt';
  const items = docRows.filter((r) => r.nom_id).map((r) => ({
    nom_id: r.nom_id, qty: num(r.qty), qty_fact: num(r.qty_fact),
    price: num(r.price),
    cost: (kind === 'receipt' || kind === 'production') ? num(r.price) : num(r.cost),
  }));
  return {
    id: editingDoc || '', kind,
    at: $('df_at').value ? localISO(new Date($('df_at').value)) : undefined,
    warehouse_id: $('df_warehouse').value,
    warehouse_to_id: kind === 'move' ? $('df_warehouse_to').value : '',
    channel: kind === 'sale' ? $('df_channel').value : '',
    account_id: $('df_account').value,
    price_type_id: kind === 'pricing' ? $('df_price_type').value : '',
    reason: $('df_reason').value.trim(), note: $('df_note').value.trim(),
    items,
  };
}

async function saveDoc(thenPost) {
  const payload = docPayload();
  if (!payload.items.length) return fail(new Error('Добавьте хотя бы одну строку'));
  try {
    const res = await post('/api/document/save', payload);
    editingDoc = res.document.id;
    if (thenPost) {
      await post('/api/document/post', { id: editingDoc });
      toast('Документ проведён', res.document.number);
    } else {
      toast('Записано', res.document.number);
    }
    closeModal('doc_modal');
    await Promise.all([refreshDocs(), refresh()]);
    PF.refreshFinance && PF.refreshFinance();
  } catch (e) { fail(e); }
}

/* ============================================================== склады */
const WH_KIND = { shelf: 'Полка магазина', home: 'Домашний склад', window: 'Витрина',
  defect: 'Брак', transit: 'В пути', material: 'Материалы' };

async function renderWarehouses() {
  let res = { warehouses: [], reserves: [], reserved: 0 };
  try { res = await get('/api/warehouses', {}); } catch (e) { /* офлайн */ }
  const list = res.warehouses || [];
  const totalQty = list.reduce((s, w) => s + num(w.qty), 0);
  const totalValue = list.reduce((s, w) => s + num(w.value), 0);
  $('wh_kpis').innerHTML = [
    kpi('Складов', String(list.length), 'мест хранения'),
    kpi('Всего штук', nfmt(totalQty), 'по всем складам'),
    kpi('Запас в рублях', money(totalValue), 'замороженный капитал'),
    kpi('В резерве', `${nfmt(res.reserved)} шт`, `${(res.reserves || []).length} резерв(ов) под заказы`),
  ].join('');

  $('wh_grid').innerHTML = list.length ? list.map((w) =>
    `<article class="wh-card" data-wh="${esc(w.id)}">`
    + `<div class="whead"><h3>${esc(w.name)}</h3>`
    + (num(w.retail) ? '<span class="chip ok">розница</span>' : '')
    + `<button class="icon-btn sm" type="button" data-wh-edit="${esc(w.id)}" title="Изменить">✎</button></div>`
    + `<div class="wbody"><div class="wnum"><b>${nfmt(w.qty)}</b><span>шт</span></div>`
    + `<div class="wval">${money(w.value)}</div></div>`
    + `<small class="muted">${esc(w.address || WH_KIND[w.kind] || '')} · ${nfmt(w.positions)} позиц.</small>`
    + '</article>').join('')
    : emptyBox('▦', 'Складов нет', 'Добавьте место хранения — полку магазина или домашний склад.');

  const reserves = res.reserves || [];
  $('wh_reserves').innerHTML = reserves.length ? reserves.map((r) =>
    '<div class="tx-row"><span class="tx-ic">⛨</span>'
    + `<div class="tx-body"><b>${esc(r.nom_name || '')}</b>`
    + `<small>${esc(dateTimeText(r.at))}${r.order_number ? ' · заказ ' + esc(r.order_number) : ''}`
    + `${r.note ? ' · ' + esc(r.note) : ''}</small></div>`
    + `<span class="amt">${nfmt(r.qty)} шт</span>`
    + `<button class="btn sm" type="button" data-reserve-release="${esc(r.id)}">Снять</button></div>`).join('')
    : '<div class="empty compact"><span>Активных резервов нет.</span></div>';

  loadTurnover();
}

async function loadTurnover() {
  const days = num((document.querySelector('#turn_period button.on') || {}).dataset?.days, 30);
  const from = localISO(new Date(Date.now() - days * 86400000));
  try {
    const res = await get('/api/stock/turnover', {
      from, warehouse_id: ($('turn_warehouse') || {}).value || '' });
    const rows = res.rows || [];
    $('turn_sub').textContent = `За ${days} дней · ${rows.length} позиций`;
    $('turn_tbody').innerHTML = rows.length ? rows.map((r) =>
      `<tr><td class="tnum muted">${esc(r.code || '')}</td><td class="strong">${esc(r.name)}</td>`
      + `<td class="right tnum">${nfmt(r.start_qty)}</td><td class="right tnum">${money(r.start_value)}</td>`
      + `<td class="right tnum pos">${r.in_qty ? '+' + nfmt(r.in_qty) : '—'}</td>`
      + `<td class="right tnum neg">${r.out_qty ? '−' + nfmt(r.out_qty) : '—'}</td>`
      + `<td class="right tnum strong">${nfmt(r.end_qty)}</td>`
      + `<td class="right tnum">${money(r.end_value)}</td></tr>`).join('')
      : `<tr><td colspan="8">${emptyBox('▥', 'Движений за период нет', 'Проведите документы прихода или продажи.')}</td></tr>`;
  } catch (e) { /* офлайн */ }
}

function openWh(id) {
  editingWh = id || null;
  const w = id ? data.warehouses.find((x) => x.id === id) : null;
  const d = w || { name: '', kind: 'shelf', address: '', retail: 0, note: '' };
  $('wf_name').value = d.name || '';
  $('wf_kind').value = d.kind || 'shelf';
  $('wf_address').value = d.address || '';
  $('wf_retail').checked = !!num(d.retail);
  $('wf_note').value = d.note || '';
  $('wh_modal_title').textContent = id ? 'Склад: ' + d.name : 'Новый склад';
  $('wh_delete').hidden = !id;
  openModal('wh_modal');
}

/* ========================================================= быстрая продажа */
function openQuickSale() {
  fillSelectors();
  const retail = data.warehouses.find((w) => num(w.retail));
  if (retail) $('qs_warehouse').value = retail.id;
  $('qs_discount').value = 0;
  $('qs_search').value = '';
  renderQuickRows();
  openModal('quicksale_modal');
}

function renderQuickRows() {
  const q = (($('qs_search') || {}).value || '').toLowerCase().trim();
  const list = data.items.filter((i) => num(i.qty) > 0 && i.kind !== 'service'
    && (!q || `${i.name} ${i.sku || ''}`.toLowerCase().includes(q)));
  $('qs_rows').innerHTML = list.length ? list.map((i) =>
    `<div class="sale-row"><div class="sinfo"><b>${esc(i.name)}</b>`
    + `<small>на складе ${nfmt(i.qty)} шт · ${money(i.price)}/шт</small></div>`
    + `<input type="number" min="0" max="${Math.floor(num(i.qty))}" step="1" placeholder="0" data-qs-qty="${esc(i.id)}" data-price="${esc(i.price)}"></div>`).join('')
    : '<div class="empty compact"><span>Нет товара в наличии.</span></div>';
  updateQsTotal();
}

function updateQsTotal() {
  let sum = 0;
  $$('[data-qs-qty]', $('qs_rows')).forEach((el) => { sum += num(el.value) * num(el.dataset.price); });
  sum -= num(($('qs_discount') || {}).value);
  $('qs_total').textContent = money(Math.max(0, sum));
}

async function saveQuickSale() {
  const rows = $$('[data-qs-qty]', $('qs_rows')).map((el) => ({
    nom_id: el.dataset.qsQty, qty: num(el.value), price: num(el.dataset.price),
  })).filter((r) => r.qty > 0);
  if (!rows.length) return fail(new Error('Введите количество хотя бы по одной позиции'));
  try {
    const res = await post('/api/sale/quick', {
      rows, warehouse_id: $('qs_warehouse').value, channel: $('qs_channel').value,
      account_id: $('qs_account').value, discount: num($('qs_discount').value),
    });
    closeModal('quicksale_modal');
    await Promise.all([refresh(), refreshDocs()]);
    PF.refreshFinance && PF.refreshFinance();
    toast('Продано', `${res.document.number} · ${money(res.document.amount)}`);
  } catch (e) { fail(e); }
}

/* ============================================================ типы цен */
function openPriceTypes() {
  $('ptype_rows').innerHTML = data.priceTypes.map((p) =>
    `<div class="ptype-row" data-pt="${esc(p.id)}">`
    + `<input data-pt-name="${esc(p.id)}" value="${esc(p.name)}" placeholder="Название">`
    + `<input type="number" step="any" data-pt-markup="${esc(p.id)}" value="${esc(p.markup)}" title="Наценка, %">`
    + `<label class="check" title="Основная цена"><input type="radio" name="ptbase" data-pt-base="${esc(p.id)}"${num(p.is_base) ? ' checked' : ''}></label>`
    + `<button class="btn sm" type="button" data-pt-save="${esc(p.id)}">✓</button></div>`).join('');
  openModal('ptype_modal');
}

/** Локальное время в ISO с смещением — как его пишет коннектор (now_iso).
    Отправлять UTC-строку с «Z» нельзя: часть SQL сравнивает даты как текст. */
function localISO(date) {
  const pad = (v) => String(Math.floor(Math.abs(v))).padStart(2, '0');
  const off = -date.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '-';
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    + `${sign}${pad(off / 60)}:${pad(off % 60)}`;
}

/* =============================================================== вкладки */
function switchPane(tabsId, prefix, pane) {
  const tabs = $(tabsId);
  if (!tabs) return;
  $$('button', tabs).forEach((b) => b.classList.toggle('on', b.dataset.pane === pane));
  $$(`[id^="${prefix}-"]`).forEach((p) => p.classList.toggle('on', p.id === `${prefix}-${pane}`));
}

/* =============================================================== события */
function bind() {
  // --- товары
  $('prod_add').addEventListener('click', () => openNom());
  $('prod_search').addEventListener('input', debounce(render, 200));
  ['prod_group', 'prod_kind', 'prod_status'].forEach((id) =>
    $(id).addEventListener('change', render));
  $('prod_warehouse').addEventListener('change', refresh);
  $('prod_view').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-mode]');
    if (!btn) return;
    viewMode = btn.dataset.mode;
    $$('button', $('prod_view')).forEach((b) => b.classList.toggle('on', b === btn));
    render();
  });
  $('prod_sale_btn').addEventListener('click', openQuickSale);
  $('prod_plan_btn').addEventListener('click', openPlan);
  $('prod_recalc').addEventListener('click', async () => {
    if (!confirmDanger('Пересчитать цены от себестоимости и наценки? Текущие цены попадут в историю.')) return;
    try {
      const res = await post('/api/nomenclature/recalc-prices', {});
      await refresh();
      toast('Цены пересчитаны', `Изменено позиций: ${res.changed}`);
    } catch (e) { fail(e); }
  });

  const gridClick = async (e) => {
    // Кнопки внутри строки проверяем раньше самой строки: иначе клик по
    // действию товара открывал бы карточку вместо выполнения действия.
    const recalc = e.target.closest('[data-nom-recalc]');
    if (recalc) { e.stopPropagation(); return recalcNomPrices(recalc.dataset.nomRecalc); }
    const batch = e.target.closest('[data-nom-batch]');
    if (batch) { e.stopPropagation(); return openBatch(batch.dataset.nomBatch); }
    const sell = e.target.closest('[data-nom-sell]');
    if (!sell) {
      const edit = e.target.closest('[data-nom-edit]');
      if (edit) return openNom(edit.dataset.nomEdit);
    }
    if (sell) {
      e.stopPropagation();
      const item = data.items.find((i) => i.id === sell.dataset.nomSell);
      if (!item) return;
      if (!confirmDanger(`Продать 1 шт «${item.name}» за ${money(item.price)}?`)) return;
      try {
        await post('/api/sale/quick', { rows: [{ nom_id: item.id, qty: 1 }] });
        await Promise.all([refresh(), refreshDocs()]);
        PF.refreshFinance && PF.refreshFinance();
        toast('Продано', item.name);
      } catch (err) { fail(err); }
    }
  };
  $('prod_grid').addEventListener('click', gridClick);
  $('prod_tbody').addEventListener('click', gridClick);

  // --- карточка
  $('nom_tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-pane]');
    if (btn) switchPane('nom_tabs', 'nompane', btn.dataset.pane);
  });
  $('nom_save').addEventListener('click', saveNom);
  $('nf_recalc_prices').addEventListener('click', () => recalcNomPrices());
  $('nom_batch').addEventListener('click', () => {
    closeModal('nom_modal');
    openBatch(editingNom);
  });
  $('nom_delete').addEventListener('click', async () => {
    if (!editingNom || !confirmDanger('Удалить позицию? Если по ней были движения — она уйдёт в архив.')) return;
    try {
      await post('/api/nomenclature/delete', { id: editingNom });
      closeModal('nom_modal');
      await refresh();
      toast('Удалено');
    } catch (e) { fail(e); }
  });
  $('nf_photo_btn').addEventListener('click', () => $('nf_photo_file').click());
  $('nf_photo_file').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      $('nf_photo_preview').src = reader.result;
      $('nf_photo_preview').hidden = false;
    };
    reader.readAsDataURL(file);
  });
  $('nf_spec_add').addEventListener('click', () => {
    specRows.push({ nom_id: '', qty: 1 });
    renderSpec();
  });
  $('nf_spec_rows').addEventListener('input', (e) => {
    const nom = e.target.closest('[data-spec-nom]');
    if (nom) specRows[+nom.dataset.specNom].nom_id = nom.value;
    const qty = e.target.closest('[data-spec-qty]');
    if (qty) specRows[+qty.dataset.specQty].qty = num(qty.value);
  });
  $('nf_spec_rows').addEventListener('click', (e) => {
    const del = e.target.closest('[data-spec-del]');
    if (del) { specRows.splice(+del.dataset.specDel, 1); renderSpec(); }
  });
  ['nf_grams', 'nf_hours', 'nf_material', 'nf_fit_per_plate', 'nf_post_minutes'].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener('input', updateNomCost);
  });

  // --- партии
  $('batch_add').addEventListener('click', () => openBatch());
  $('batch_plan_btn').addEventListener('click', openPlan);
  ['bf_nom', 'bf_qty', 'bf_plates', 'bf_printer', 'bf_spool', 'bf_file'].forEach((id) =>
    $(id).addEventListener('input', runPlan));
  $('bf_mode').addEventListener('input', () => { toggleBatchMode(); runPlan(); });
  $('bf_item_add').addEventListener('click', () => {
    collectMixedRows();
    mixedRows.push({ nom_id: '', qty: 1 });
    renderMixedRows();
  });
  $('bf_item_rows').addEventListener('input', runPlan);
  $('bf_item_rows').addEventListener('change', runPlan);
  $('bf_item_rows').addEventListener('click', (e) => {
    const del = e.target.closest('[data-mixed-del]');
    if (!del) return;
    collectMixedRows();
    mixedRows.splice(+del.dataset.mixedDel, 1);
    if (!mixedRows.length) mixedRows = [{ nom_id: '', qty: 1 }];
    renderMixedRows();
    runPlan();
  });
  $('bf_create').addEventListener('click', createBatch);
  $('batch_filter').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-state]');
    if (!btn) return;
    $$('button', $('batch_filter')).forEach((b) => b.classList.toggle('on', b === btn));
    refreshBatches();
  });
  $('batch_list').addEventListener('click', async (e) => {
    const recv = e.target.closest('[data-batch-receive]');
    if (recv) {
      const b = batchData.find((x) => x.id === recv.dataset.batchReceive);
      const mixedList = b.items_list || [];
      if (mixedList.length) {
        // Смешанная партия: принимаем целыми плитами — каждый товар своим количеством.
        const perPlate = mixedList.reduce((s, r) => s + num(r.qty_per_plate), 0);
        const maxPlates = Math.max(1, Math.ceil(Math.max(0, num(b.qty_planned) - num(b.qty_done)) / Math.max(1, perPlate)));
        const answer = window.prompt(`Сколько ПЛИТ оприходовать? (на плите ${perPlate} шт, осталось ~${maxPlates})`, '1');
        if (answer == null) return;
        const plates = Math.min(maxPlates, Math.max(0, Math.round(num(answer))));
        if (!plates) return;
        try {
          await post('/api/batch/receive', {
            id: b.id, qty: plates * perPlate, job_id: '',
            note: 'приёмка вручную (плитами)',
            items: mixedList.map((r) => ({ nom_id: r.nom_id, qty: num(r.qty_per_plate) * plates })),
          });
          toast('Плиты оприходованы', `${plates} шт · ${plates * perPlate} изделий на складе`);
          await Promise.all([refreshBatches(), refresh()]);
        } catch (err) { fail(err); }
        return;
      }
      const rest = Math.max(0, num(b.qty_planned) - num(b.qty_done));
      const answer = window.prompt(
        `Сколько годных штук принять на склад?\nОсталось по плану: ${rest} шт`, String(rest));
      if (answer === null) return;
      const qty = num(answer);
      if (qty <= 0) return;
      try {
        await post('/api/batch/receive', { id: b.id, qty });
        await Promise.all([refreshBatches(), refresh()]);
        toast('Принято на склад', `${nfmt(qty)} шт`);
      } catch (err) { fail(err); }
      return;
    }
    const rep = e.target.closest('[data-batch-repeat]');
    if (rep) {
      try {
        await post('/api/batch/repeat', { id: rep.dataset.batchRepeat });
        await refreshBatches();
        PF.refreshCore && PF.refreshCore();
        toast('Партия повторена');
      } catch (err) { fail(err); }
      return;
    }
    const can = e.target.closest('[data-batch-cancel]');
    if (can) {
      if (!confirmDanger('Отменить партию? Незапущенные задания будут сняты с очереди.')) return;
      try {
        await post('/api/batch/cancel', { id: can.dataset.batchCancel });
        await Promise.all([refreshBatches(), refresh()]);
        PF.refreshCore && PF.refreshCore();
        toast('Партия отменена');
      } catch (err) { fail(err); }
    }
  });

  // --- план пополнения
  $('plan_rows').addEventListener('input', updatePlanTotal);
  $('plan_rows').addEventListener('change', updatePlanTotal);
  $('plan_create').addEventListener('click', createFromPlan);

  // --- документы
  $('doc_add').addEventListener('click', () => openDoc(null, $('doc_new_kind').value));
  $('doc_search').addEventListener('input', debounce(refreshDocs, 250));
  ['doc_filter_kind', 'doc_filter_state'].forEach((id) =>
    $(id).addEventListener('change', refreshDocs));
  $('doc_tbody').addEventListener('click', (e) => {
    const row = e.target.closest('[data-doc], [data-doc-open]');
    if (!row) return;
    openDoc(row.dataset.doc || row.dataset.docOpen);
  });
  $('df_add_row').addEventListener('click', () => {
    docRows.push({ nom_id: '', qty: 1, price: 0, qty_fact: 0 });
    renderDocRows();
  });
  $('df_tbody').addEventListener('input', (e) => {
    const nom = e.target.closest('[data-row-nom]');
    if (nom) {
      const index = +nom.dataset.rowNom;
      docRows[index].nom_id = nom.value;
      const item = data.items.find((i) => i.id === nom.value);
      const kind = $('doc_modal').dataset.kind;
      if (item && !num(docRows[index].price)) {
        docRows[index].price = (kind === 'receipt' || kind === 'production')
          ? num(item.cost) : num(item.price);
      }
      renderDocRows();
      return;
    }
    const qty = e.target.closest('[data-row-qty]');
    if (qty) docRows[+qty.dataset.rowQty].qty = num(qty.value);
    const fact = e.target.closest('[data-row-fact]');
    if (fact) docRows[+fact.dataset.rowFact].qty_fact = num(fact.value);
    const price = e.target.closest('[data-row-price]');
    if (price) docRows[+price.dataset.rowPrice].price = num(price.value);
    const index = qty ? +qty.dataset.rowQty : price ? +price.dataset.rowPrice : null;
    if (index != null) {
      const cell = $('df_tbody').querySelector(`[data-row-sum="${index}"]`);
      if (cell) cell.textContent = money(num(docRows[index].qty) * num(docRows[index].price));
    }
    updateDocTotal();
  });
  $('df_tbody').addEventListener('click', (e) => {
    const del = e.target.closest('[data-row-del]');
    if (del) { docRows.splice(+del.dataset.rowDel, 1); renderDocRows(); }
  });
  $('doc_save').addEventListener('click', () => saveDoc(false));
  $('doc_post').addEventListener('click', () => saveDoc(true));
  $('doc_unpost').addEventListener('click', async () => {
    if (!editingDoc || !confirmDanger('Отменить проведение? Движения по складу будут сняты.')) return;
    try {
      await post('/api/document/unpost', { id: editingDoc });
      closeModal('doc_modal');
      await Promise.all([refreshDocs(), refresh()]);
      PF.refreshFinance && PF.refreshFinance();
      toast('Проведение отменено');
    } catch (e) { fail(e); }
  });
  $('doc_delete').addEventListener('click', async () => {
    if (!editingDoc || !confirmDanger('Удалить черновик документа?')) return;
    try {
      await post('/api/document/delete', { id: editingDoc });
      closeModal('doc_modal');
      await refreshDocs();
      toast('Документ удалён');
    } catch (e) { fail(e); }
  });

  // --- склады
  $('wh_add').addEventListener('click', () => openWh());
  $('wh_types_btn').addEventListener('click', openPriceTypes);
  $('wh_grid').addEventListener('click', (e) => {
    const edit = e.target.closest('[data-wh-edit]');
    if (edit) openWh(edit.dataset.whEdit);
  });
  $('wh_save').addEventListener('click', async () => {
    const payload = {
      id: editingWh || '', name: $('wf_name').value.trim(), kind: $('wf_kind').value,
      address: $('wf_address').value.trim(), retail: $('wf_retail').checked ? 1 : 0,
      note: $('wf_note').value.trim(),
    };
    if (!payload.name) return fail(new Error('Укажите название склада'));
    try {
      await post('/api/warehouse/save', payload);
      closeModal('wh_modal');
      await refresh();
      renderWarehouses();
      toast('Склад сохранён', payload.name);
    } catch (e) { fail(e); }
  });
  $('wh_delete').addEventListener('click', async () => {
    if (!editingWh || !confirmDanger('Удалить склад? Это возможно только при нулевых остатках.')) return;
    try {
      await post('/api/warehouse/delete', { id: editingWh });
      closeModal('wh_modal');
      await refresh();
      renderWarehouses();
      toast('Склад удалён');
    } catch (e) { fail(e); }
  });
  $('turn_period').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-days]');
    if (!btn) return;
    $$('button', $('turn_period')).forEach((b) => b.classList.toggle('on', b === btn));
    loadTurnover();
  });
  $('turn_warehouse').addEventListener('change', loadTurnover);
  $('wh_reserves').addEventListener('click', async (e) => {
    const rel = e.target.closest('[data-reserve-release]');
    if (!rel) return;
    try {
      await post('/api/reserve/release', { id: rel.dataset.reserveRelease });
      await refresh();
      renderWarehouses();
      toast('Резерв снят');
    } catch (err) { fail(err); }
  });

  // --- быстрая продажа
  $('qs_search').addEventListener('input', debounce(renderQuickRows, 200));
  $('qs_rows').addEventListener('input', updateQsTotal);
  $('qs_discount').addEventListener('input', updateQsTotal);
  $('qs_save').addEventListener('click', saveQuickSale);

  // --- типы цен
  $('ptype_add').addEventListener('click', async () => {
    const name = window.prompt('Название типа цен:', 'Новый тип');
    if (!name) return;
    try {
      await post('/api/price-type/save', { name, markup: 100 });
      await refresh();
      openPriceTypes();
    } catch (e) { fail(e); }
  });
  $('ptype_rows').addEventListener('click', async (e) => {
    const save = e.target.closest('[data-pt-save]');
    if (!save) return;
    const id = save.dataset.ptSave;
    const box = save.closest('.ptype-row');
    const name = $$('[data-pt-name]', box)[0].value.trim();
    const markup = num($$('[data-pt-markup]', box)[0].value);
    const base = $$('[data-pt-base]', box)[0].checked;
    try {
      await post('/api/price-type/save', { id, name, markup, is_base: base ? 1 : 0 });
      await refresh();
      toast('Тип цен сохранён', name);
    } catch (err) { fail(err); }
  });
}

/* =============================================================== старт */
PF.on('ready', () => {
  bind();
  refresh();
  refreshDocs();
  refreshBatches();
});
PF.on('data', () => {
  if (document.querySelector('#view-products.on')) refresh();
  if (document.querySelector('#view-batches.on')) refreshBatches();
});
PF.on('view', (d) => {
  if (d.view === 'products') refresh();
  if (d.view === 'batches') refreshBatches();
  if (d.view === 'documents') refreshDocs();
  if (d.view === 'warehouses') { refresh().then(renderWarehouses); }
});
setInterval(() => {
  if (document.querySelector('#view-products.on')) refresh();
  if (document.querySelector('#view-batches.on')) refreshBatches();
}, 30000);

PF.modules.products = { refresh, openNom, openBatch, openDoc, openQuickSale, openPlan };
})();
