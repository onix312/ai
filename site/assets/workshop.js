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

function bind() {
  const shiftBtn = $('shift_refresh');
  if (shiftBtn) shiftBtn.addEventListener('click', loadShift);
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
PF.modules.workshop = { loadShift, loadSuppliers, loadPresets };
})();
