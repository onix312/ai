/* PrintFlow 2.0 — стеллаж магазина: готовая продукция на полке.
   Остатки штук, приход с партии печати, продажи (вручную / по QR),
   инвентаризация, оборачиваемость, мёртвый сток, QR-ценники. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, money, nfmt, hoursText, dateText, dateTimeText,
  agoText, toast, fail, openModal, closeModal, confirmDanger } = U;
const { get, post } = PF.api;

let editingShelf = null;
let shelfData = { items: [], summary: {}, moves: [] };

const KIND_LABEL = {
  produce: 'Приход', sale: 'Продажа', online: 'Продажа онлайн',
  writeoff: 'Списание', inventory: 'Инвентаризация',
};
const STATUS_LABEL = {
  ok: 'В наличии', low: 'Мало', dead: 'Мёртвый сток', empty: 'Пусто',
};

/* ============================================================== загрузка */
async function refreshShelf() {
  try {
    const [data, moves] = await Promise.all([
      get('/api/shelf'), get('/api/shelf/moves', { limit: 60 }),
    ]);
    shelfData = { items: data.items || [], summary: data.summary || {}, moves: moves.moves || [] };
    if (document.querySelector('#view-shelf.on')) renderShelf();
    updateNavTag();
    PF.emit('shelf', shelfData);
  } catch (e) { /* офлайн */ }
}
PF.refreshShelf = refreshShelf;

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

function renderShelf() {
  const s = shelfData.summary || {};
  $('shelf_kpis').innerHTML = [
    shelfKpi('Штук на стеллаже', nfmt(s.qty), `${nfmt(s.items)} позиций`),
    shelfKpi('Остаток в рублях', money(s.value), 'по себестоимости (заморожено)'),
    shelfKpi('Продано за 7 дней', `${nfmt(s.sold_7)} шт`, `${money(s.sold_7_money)}`),
    shelfKpi('Мёртвый сток', String(s.dead || 0), s.dead_value ? `${money(s.dead_value)} заморожено` : 'нет позиций без продаж',
      s.dead ? 'bad' : 'ok'),
    shelfKpi('План пополнения', `${nfmt(s.plan_qty)} шт`, 'напечатать, чтобы хватило на 7 дней'),
  ].join('');

  const items = shelfData.items || [];
  $('shelf_grid').innerHTML = items.length ? items.map((i) => {
    const st = i.status || 'ok';
    const days = i.days_left;
    const warn = st === 'dead' ? 'bad' : st === 'low' ? 'warn' : 'ok';
    return `<article class="shelf-card ${st}" data-shelf="${esc(i.id)}">`
      + `<div class="shead">`
      + (i.photo ? `<img class="sphoto" src="/api/shelf/photo.jpg?id=${esc(i.id)}&t=${esc(i.updated_at || '')}" alt="">`
        : `<span class="sphoto ph">◻</span>`)
      + `<div class="sinfo"><h3>${esc(i.name)}</h3>`
      + `<small class="muted">${i.catalog_id ? 'из каталога' : 'без привязки'}${i.note ? ' · ' + esc(i.note) : ''}</small></div>`
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
      + `<button class="btn sm ghost" type="button" data-shelf-qr="${esc(i.id)}">◫ Ценник</button>`
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
  const d = i || { name: '', catalog_id: '', price: '', cost_per_unit: '', qty: 0, min_qty: '', note: '' };
  $('shf_name').value = d.name || '';
  $('shf_price').value = d.price ?? '';
  $('shf_cost').value = d.cost_per_unit ?? '';
  $('shf_qty').value = d.qty ?? '';
  $('shf_min').value = d.min_qty ?? '';
  $('shf_note').value = d.note || '';
  $('shf_catalog_id').innerHTML = '<option value="">Без привязки</option>'
    + (PF.state.catalog || []).map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  $('shf_catalog_id').value = d.catalog_id || '';
  const img = $('shf_photo_preview');
  img.hidden = !d.photo;
  if (d.photo) img.src = `/api/shelf/photo.jpg?id=${esc(d.id)}&t=${esc(d.updated_at || '')}`;
  $('shelf_delete').hidden = !id;
  $('shelf_modal_title').textContent = id ? 'Позиция: ' + d.name : 'Новая позиция стеллажа';
  openModal('shelf_modal');
}

async function saveShelf() {
  const payload = {
    id: editingShelf || '',
    name: $('shf_name').value.trim(),
    catalog_id: $('shf_catalog_id').value,
    price: num($('shf_price').value),
    cost_per_unit: num($('shf_cost').value),
    qty: num($('shf_qty').value),
    min_qty: num($('shf_min').value),
    note: $('shf_note').value.trim(),
  };
  if (!payload.name) return fail(new Error('Укажите название позиции'));
  try {
    const res = await post('/api/shelf/save', payload);
    closeModal('shelf_modal');
    // фото, если выбрали
    const file = $('shf_photo_file').files[0];
    if (file) {
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          await post('/api/shelf/photo', { id: res.item.id, data: reader.result });
          await refreshShelf();
          toast('Позиция сохранена', payload.name);
        } catch (e) { fail(e); }
      };
      reader.readAsDataURL(file);
    } else {
      await refreshShelf();
      toast('Позиция сохранена', payload.name);
    }
  } catch (e) { fail(e); }
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
    const qr = e.target.closest('[data-shelf-qr]');
    if (qr) { openQr(qr.dataset.shelfQr); return; }
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

PF.modules.shelf = { refreshShelf, openShelf, openProduce, openSales, openInventory };
})();
