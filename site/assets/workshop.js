/* PrintFlow 9.0 — цех: смена, поставщики, пресеты плиты. */
(() => {
'use strict';
const U = PF.ui, { $, esc, num, money, nfmt, toast, fail, ask } = U;
const { get, post } = PF.api;

async function loadShift() {
  const host = $('shift_list');
  if (!host) return;
  try {
    const data = await get('/api/workshop/shift');
    host.innerHTML = (data.items || []).map((it) =>
      `<label class="check" style="display:flex;gap:8px;align-items:center;margin:6px 0">`
      + `<input type="checkbox" data-shift="${esc(it.id)}" ${it.done ? 'checked' : ''}>`
      + `<span>${esc(it.title)}${it.at ? ` <small class="muted">${esc(it.at.slice(0, 16))}</small>` : ''}</span></label>`
    ).join('') || '<div class="empty compact"><span>Чек-лист пуст.</span></div>';
    host.querySelectorAll('[data-shift]').forEach((box) => {
      box.addEventListener('change', async () => {
        try {
          await post('/api/workshop/shift', { item_id: box.dataset.shift, done: box.checked });
        } catch (e) { fail(e); loadShift(); }
      });
    });
  } catch (e) {
    host.innerHTML = `<div class="notice"><span>ℹ</span><span>${esc(e.message)}</span></div>`;
  }
}

async function loadSuppliers() {
  const host = $('supplier_list');
  if (!host) return;
  try {
    const data = await get('/api/workshop/suppliers');
    const rows = data.suppliers || [];
    const dl = $('fr_suppliers_datalist');
    if (dl) {
      dl.innerHTML = rows.map((s) =>
        `<option value="${esc(s.name)}">${num(s.price_per_kg) ? nfmt(s.price_per_kg) + ' ₽/кг' : ''}</option>`
      ).join('');
    }
    host.innerHTML = rows.length ? rows.map((s) =>
      `<div class="tx-row"><div class="tx-body"><b>${esc(s.name)}</b>`
      + `<small>${num(s.price_per_kg) ? money(s.price_per_kg) + '/кг' : 'нет цены'} ${esc(s.url || '')}</small></div>`
      + `<button class="btn sm" type="button" data-sup-apply="${esc(s.id)}">На катушки</button>`
      + `<button class="icon-btn sm" type="button" data-sup-del="${esc(s.id)}">×</button></div>`
    ).join('') : '<div class="empty compact"><span>Поставщиков пока нет.</span>'
      + '<button class="btn sm primary" type="button" data-empty-click="supplier_add">+ Поставщик</button></div>';
    host.querySelectorAll('[data-sup-apply]').forEach((b) => b.addEventListener('click', async () => {
      try {
        const res = await post('/api/workshop/supplier/apply-price', { id: b.dataset.supApply });
        toast('Цена ₽/кг записана', nfmt(res.price_per_kg) + ' ₽');
        PF.refreshCore && PF.refreshCore();
      } catch (e) { fail(e); }
    }));
    host.querySelectorAll('[data-sup-del]').forEach((b) => b.addEventListener('click', async () => {
      try { await post('/api/workshop/supplier/delete', { id: b.dataset.supDel }); loadSuppliers(); }
      catch (e) { fail(e); }
    }));
  } catch (e) {
    host.innerHTML = `<div class="notice"><span>ℹ</span><span>${esc(e.message)}</span></div>`;
  }
}

async function loadPresets() {
  const host = $('preset_list');
  const sel = $('pj_preset');
  try {
    const data = await get('/api/workshop/presets');
    const rows = data.presets || [];
    if (sel) {
      const keep = sel.value;
      sel.innerHTML = '<option value="">Без пресета</option>' + rows.map((p) =>
        `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
      if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
    }
    if (!host) return;
    host.innerHTML = rows.length ? rows.map((p) => {
      const pay = p.payload || {};
      const bits = [
        pay.use_ams === false ? 'без AMS' : 'AMS',
        pay.bed_level === false ? '' : 'стол',
        pay.flow_cali ? 'поток' : '',
        pay.timelapse ? 'таймлапс' : '',
      ].filter(Boolean).join(' · ');
      return `<div class="tx-row"><div class="tx-body"><b>${esc(p.name)}</b><small>${esc(bits)}</small></div>`
        + `<button class="icon-btn sm" type="button" data-pp-del="${esc(p.id)}">×</button></div>`;
    }).join('') : '<div class="empty compact"><span>Пресетов нет — сохраните настройки плиты.</span>'
      + '<button class="btn sm primary" type="button" data-empty-click="preset_add">+ Пресет</button></div>';
    host.querySelectorAll('[data-pp-del]').forEach((b) => b.addEventListener('click', async () => {
      try { await post('/api/workshop/preset/delete', { id: b.dataset.ppDel }); loadPresets(); }
      catch (e) { fail(e); }
    }));
  } catch (e) {
    if (host) host.innerHTML = `<div class="notice"><span>ℹ</span><span>${esc(e.message)}</span></div>`;
  }
}

function updateReceiptSummary() {
  const tbody = $('fr_tbody');
  const summary = $('fr_summary');
  if (!tbody || !summary) return;
  const rows = [...tbody.querySelectorAll('tr')];
  let totalSpools = 0;
  let totalGrams = 0;
  let totalAmount = 0;
  for (const r of rows) {
    const sc = Math.max(0, Math.round(num(r.querySelector('.fr-count') ? r.querySelector('.fr-count').value : 0, 0)));
    const sg = Math.max(0, num(r.querySelector('.fr-grams') ? r.querySelector('.fr-grams').value : 0, 0));
    const amount = Math.max(0, num(r.querySelector('.fr-amount') ? r.querySelector('.fr-amount').value : 0, 0));
    totalSpools += sc;
    totalGrams += sc * sg;
    totalAmount += amount;
  }
  const kgText = totalGrams ? ` (${nfmt(Math.round(totalGrams / 100) / 10)} кг)` : '';
  summary.innerHTML = `<span>→</span><span>Итого: <b>${nfmt(totalSpools)}</b> катушек · <b>${nfmt(Math.round(totalGrams))}</b> г${kgText} · <b>${money(totalAmount)}</b></span>`;
}

function addReceiptRow(data = {}) {
  const tbody = $('fr_tbody');
  if (!tbody) return;
  const tr = document.createElement('tr');
  const mat = data.material || 'PLA';
  const colorName = data.color_name || '';
  const colorHex = data.color_hex || '#333333';
  const brand = data.brand || '';
  const count = Math.max(1, Math.round(num(data.spool_count, 1)));
  const grams = Math.max(1, num(data.spool_grams, 1000));
  const ppk = data.price_per_kg != null && data.price_per_kg !== '' ? num(data.price_per_kg) : 1600;
  const amount = data.total_amount != null && data.total_amount !== ''
    ? num(data.total_amount)
    : Math.round(ppk * (count * grams) / 1000);

  tr.innerHTML = `
    <td><input class="fr-mat" list="materials_datalist" value="${esc(mat)}" placeholder="PLA" style="width:100%"></td>
    <td><input class="fr-color-name" value="${esc(colorName)}" placeholder="Цвет" style="width:100%"></td>
    <td><input class="fr-color-hex" type="color" value="${esc(colorHex)}" style="width:36px;height:28px;padding:1px"></td>
    <td><input class="fr-brand" value="${esc(brand)}" placeholder="Бренд" style="width:100%"></td>
    <td><input class="fr-count" type="number" min="1" step="1" value="${count}" style="width:100%;text-align:right"></td>
    <td><input class="fr-grams" type="number" min="1" step="any" value="${grams}" style="width:100%;text-align:right"></td>
    <td><input class="fr-ppk" type="number" min="0" step="any" value="${ppk || ''}" placeholder="1600" style="width:100%;text-align:right"></td>
    <td><input class="fr-amount" type="number" min="0" step="any" value="${amount || ''}" placeholder="1600" style="width:100%;text-align:right"></td>
    <td><button class="icon-btn sm fr-del" type="button" title="Удалить">×</button></td>
  `;

  const ppkInput = tr.querySelector('.fr-ppk');
  const amountInput = tr.querySelector('.fr-amount');
  const countInput = tr.querySelector('.fr-count');
  const gramsInput = tr.querySelector('.fr-grams');
  const delBtn = tr.querySelector('.fr-del');

  if (ppkInput && amountInput && countInput && gramsInput) {
    ppkInput.addEventListener('input', () => {
      const sc = Math.max(1, Math.round(num(countInput.value, 1)));
      const sg = Math.max(1, num(gramsInput.value, 1000));
      const p = num(ppkInput.value, 0);
      if (p > 0) {
        amountInput.value = Math.round(p * (sc * sg) / 1000);
      }
      updateReceiptSummary();
    });

    amountInput.addEventListener('input', () => {
      const sc = Math.max(1, Math.round(num(countInput.value, 1)));
      const sg = Math.max(1, num(gramsInput.value, 1000));
      const a = num(amountInput.value, 0);
      const kg = (sc * sg) / 1000;
      if (a > 0 && kg > 0) {
        ppkInput.value = Math.round(a / kg);
      }
      updateReceiptSummary();
    });

    const onQtyChange = () => {
      const sc = Math.max(1, Math.round(num(countInput.value, 1)));
      const sg = Math.max(1, num(gramsInput.value, 1000));
      const p = num(ppkInput.value, 0);
      if (p > 0) {
        amountInput.value = Math.round(p * (sc * sg) / 1000);
      } else if (num(amountInput.value, 0) > 0) {
        const kg = (sc * sg) / 1000;
        if (kg > 0) ppkInput.value = Math.round(num(amountInput.value, 0) / kg);
      }
      updateReceiptSummary();
    };

    countInput.addEventListener('input', onQtyChange);
    gramsInput.addEventListener('input', onQtyChange);
  }

  if (delBtn) {
    delBtn.addEventListener('click', () => {
      tr.remove();
      if (!tbody.children.length) addReceiptRow();
      updateReceiptSummary();
    });
  }

  tbody.appendChild(tr);
  updateReceiptSummary();
}

function openFilamentReceipt(initialData) {
  const modal = $('filament_receipt_modal');
  if (!modal) return;

  const loc = $('fr_location');
  if (loc) loc.value = 'shop';

  const wh = $('fr_warehouse');
  if (wh) {
    wh.innerHTML = '<option value="">Без привязки к складу</option>'
      + (PF.state.warehouses || []).filter((w) => !num(w.archived)).map((w) =>
        `<option value="${esc(w.id)}">${esc(w.name)}</option>`).join('');
  }

  const acc = $('fr_account');
  if (acc) {
    const accounts = (PF.state.accounts || []).filter((a) => !num(a.archived));
    acc.innerHTML = accounts.map((a) =>
      `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
    const defaultAccount = (PF.state.settings && PF.state.settings.default_account) || '';
    if ([...acc.options].some((o) => o.value === defaultAccount)) {
      acc.value = defaultAccount;
    }
  }

  const supp = $('fr_supplier');
  if (supp) supp.value = '';
  const note = $('fr_note');
  if (note) note.value = '';
  const conf = $('fr_confirmed');
  if (conf) conf.checked = true;

  const tbody = $('fr_tbody');
  if (tbody) {
    tbody.innerHTML = '';
    if (Array.isArray(initialData) && initialData.length) {
      initialData.forEach((it) => addReceiptRow(it));
    } else {
      addReceiptRow();
    }
  }
  updateReceiptSummary();
  if (typeof openModal === 'function') {
    openModal('filament_receipt_modal');
  } else if (modal.showModal) {
    modal.showModal();
  }
}

async function submitFilamentReceipt() {
  const btn = $('fr_submit');
  const conf = $('fr_confirmed');
  if (conf && !conf.checked) {
    return fail(new Error('Подтвердите получение и сумму пластика'));
  }
  const tbody = $('fr_tbody');
  if (!tbody) return;
  const trs = [...tbody.querySelectorAll('tr')];
  const items = [];
  for (const tr of trs) {
    const matInput = tr.querySelector('.fr-mat');
    const mat = (matInput ? matInput.value : '').trim();
    if (!mat) continue;
    const cInput = tr.querySelector('.fr-color-name');
    const colorName = (cInput ? cInput.value : '').trim();
    const hexInput = tr.querySelector('.fr-color-hex');
    const colorHex = (hexInput ? hexInput.value : '#4b5563') || '#4b5563';
    const bInput = tr.querySelector('.fr-brand');
    const brand = (bInput ? bInput.value : '').trim();
    const countInput = tr.querySelector('.fr-count');
    const count = Math.max(1, Math.round(num(countInput ? countInput.value : 1, 1)));
    const gramsInput = tr.querySelector('.fr-grams');
    const grams = Math.max(1, num(gramsInput ? gramsInput.value : 1000, 1000));
    const ppkInput = tr.querySelector('.fr-ppk');
    const ppk = num(ppkInput ? ppkInput.value : 0, 0);
    const amountInput = tr.querySelector('.fr-amount');
    const amount = num(amountInput ? amountInput.value : 0, 0);
    items.push({
      material: mat,
      color_name: colorName,
      color_hex: colorHex,
      brand: brand,
      spool_count: count,
      spool_grams: grams,
      price_per_kg: ppk,
      total_amount: amount,
    });
  }
  if (!items.length) {
    return fail(new Error('Укажите хотя бы одну позицию пластика с материалом'));
  }

  const locEl = $('fr_location');
  const location = (locEl ? locEl.value : 'shop') || 'shop';
  const whEl = $('fr_warehouse');
  const warehouse_id = (whEl ? whEl.value : '').trim();
  const suppEl = $('fr_supplier');
  const supplier = (suppEl ? suppEl.value : '').trim();
  const accEl = $('fr_account');
  const account_id = (accEl ? accEl.value : '').trim();
  const noteEl = $('fr_note');
  const note = (noteEl ? noteEl.value : '').trim();
  const requestId = 'frq-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);

  if (btn) btn.disabled = true;
  try {
    const res = await post('/api/workshop/receipt', {
      confirmed: true,
      request_id: requestId,
      location,
      warehouse_id,
      supplier,
      account_id,
      note,
      items,
    });
    if (typeof closeModal === 'function') {
      closeModal('filament_receipt_modal');
    } else {
      const m = $('filament_receipt_modal');
      if (m && m.close) m.close();
    }
    const docNumber = (res.document && res.document.number) ? res.document.number : '';
    const spoolsCount = (res.spools || []).length;
    toast(res.already ? 'Приход уже был записан' : 'Приход пластика оформлен',
      `${docNumber ? docNumber + ' · ' : ''}${nfmt(spoolsCount)} катушек`);
    if (PF.refreshCore) await PF.refreshCore();
    if (PF.refreshFinance) PF.refreshFinance();
    if (PF.refreshMoney) PF.refreshMoney();
  } catch (e) {
    fail(e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function bind() {
  const shiftBtn = $('shift_refresh');
  if (shiftBtn) shiftBtn.addEventListener('click', loadShift);
  const receiptBtn = $('filament_receipt_btn');
  if (receiptBtn) receiptBtn.addEventListener('click', () => openFilamentReceipt());
  const addRowBtn = $('fr_add_row');
  if (addRowBtn) addRowBtn.addEventListener('click', () => addReceiptRow());
  const submitBtn = $('fr_submit');
  if (submitBtn) submitBtn.addEventListener('click', submitFilamentReceipt);
  const addSup = $('supplier_add');
  if (addSup) addSup.addEventListener('click', async () => {
    const ans = await ask({
      title: 'Поставщик пластика',
      fields: [
        { name: 'name', label: 'Название', type: 'text', placeholder: 'Поставщик' },
        { name: 'price', label: 'Цена, ₽/кг', type: 'number', value: '1600', min: 0 },
      ],
      ok: 'Сохранить',
    });
    if (!ans || !ans.name) return;
    try {
      await post('/api/workshop/supplier/save', { name: ans.name, price_per_kg: num(ans.price) });
      toast('Поставщик сохранён', ans.name);
      loadSuppliers();
    } catch (e) { fail(e); }
  });
  const addPp = $('preset_add');
  if (addPp) addPp.addEventListener('click', async () => {
    const name = await ask({
      title: 'Пресет плиты',
      fields: [{ name: 'name', label: 'Название', type: 'text', value: 'Обычная плита' }],
      ok: 'Сохранить',
    });
    if (!name) return;
    try {
      await post('/api/workshop/preset/save', {
        name,
        use_ams: true, bed_level: true, flow_cali: false, timelapse: false, plate: 1,
      });
      toast('Пресет сохранён', name);
      loadPresets();
    } catch (e) { fail(e); }
  });
}

PF.on('ready', () => { bind(); loadShift(); loadSuppliers(); loadPresets(); });
PF.on('view', (d) => {
  if (d.view === 'inventory') { loadShift(); loadSuppliers(); loadPresets(); }
  if (d.view === 'printers' || d.view === 'queue') loadPresets();
});
PF.modules.workshop = { loadShift, loadSuppliers, loadPresets, openFilamentReceipt };
})();
