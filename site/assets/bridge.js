/* PrintFlow — Мост Bambu Studio: Watch Folder, 3MF превью, AMS-маппинг, Preflight, FTPS прогресс, Health */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, toast, fail, openModal, closeModal } = U;
const { get, post } = PF.api;

let watchTimer = 0;
let lastWatchIds = new Set();

function renderWatchBanner(items){
  // верх принтеров: показать новые файлы из Watch Folder
  const host = $('pr_watch_banner');
  if (!host) return;
  if (!items.length){
    host.hidden = true;
    host.innerHTML = '';
    return;
  }
  host.hidden = false;
  host.innerHTML = `<div class="notice" style="border-color:var(--accent)"><span>⇪</span><span><b>Из Bambu Studio: ${items.length} новых файла</b> — из папки Watch Folder</span></div>` +
    items.slice(0,5).map(it=>{
      const g = it.total_grams || it.grams || 0;
      const m = it.total_minutes || it.minutes || 0;
      const plates = it.plate_count || 1;
      const order = it.order_id ? (PF.state.orders.find(o=>o.id===it.order_id)?.number || it.order_id) : '—';
      return `<div class="tx-row" data-watch-fid="${esc(it.fid||'')}" style="cursor:pointer">
        <span class="tx-ic income">3MF</span>
        <div class="tx-body"><b>${esc(it.name||it.file||'файл')}</b><small>${g}г · ${m}мин · плит ${plates} · заказ ${esc(order)}</small></div>
        <button class="btn sm" data-watch-open>Открыть</button></div>`;
    }).join('');
}

async function pollWatch(){
  try{
    const data = await get('/api/watch/pending', {limit: 6});
    const items = data.items || [];
    renderWatchBanner(items);
    renderWatchCard(items.length);
    // тост для новых
    const ids = new Set(items.map(i=>i.fid));
    let newOnes = items.filter(i=>!lastWatchIds.has(i.fid));
    if (lastWatchIds.size && newOnes.length){
      const first = newOnes[0];
      toast('Новый файл из Bambu Studio', `${first.name} · ${first.total_grams||first.grams||0}г`, 'info');
    }
    lastWatchIds = ids;
  }catch(e){}
}

/* 18.8: папка — главный путь нарезки, поэтому её статус виден всегда,
   а не только когда есть новые файлы. Включить/создать — в один клик. */
async function renderWatchCard(pendingCount){
  let status;
  try{ status = await get('/api/watch/status'); }catch(e){ return; }
  const text = $('pr_watch_text');
  const meta = $('pr_watch_meta');
  const tag = $('pr_watch_tag');
  const en = $('pr_watch_enable');
  const dis = $('pr_watch_disable');
  const ensure = $('pr_watch_ensure');
  if (!text) return;
  const path = status.path || '';
  if (status.enabled){
    if (tag){ tag.textContent = 'Включена'; tag.className = 'tag ok'; }
    if (text){
      const n = num(pendingCount);
      text.textContent = n
        ? `${n} новый(ые) файл(а) из папки — откройте баннер выше.`
        : 'Папка на связи: слайсер сохранил 3MF или G-code — файл появится здесь сам.';
    }
    if (meta){ meta.textContent = path + (status.exists ? '' : ' · папка ещё не создана'); }
    if (en) en.hidden = true;
    if (dis) dis.hidden = false;
    if (ensure) ensure.hidden = !!status.exists;
  } else {
    if (tag){ tag.textContent = 'Выключена'; tag.className = 'tag warn'; }
    if (text) text.textContent = 'Главный путь нарезки выключен: включите папку и укажите её в настройках слайсера (Bambu Studio / OrcaSlicer → экспорт).';
    if (meta){ meta.textContent = path ? `Путь: ${path}` : 'Путь не задан — задайте в Настройках → Принтеры и Bambu'; }
    if (en) en.hidden = false;
    if (dis) dis.hidden = true;
    if (ensure) ensure.hidden = !!status.exists;
  }
}

async function setWatchEnabled(on){
  try{
    await post('/api/settings', { watch_folder_enabled: on });
    toast(on ? 'Watch Folder включён' : 'Watch Folder выключен',
          on ? 'Следим за папкой: новый файл появится в разделе «Печать»' : 'Папку больше не смотрим', 'info');
    renderWatchCard(0);
  }catch(e){ fail(e); }
}

async function ensureWatchFolder(){
  try{
    const r = await post('/api/watch/ensure', {});
    toast('Папка готова', r.path || '', 'info');
    renderWatchCard(0);
  }catch(e){ fail(e); }
}

function initWatch(){
  // создать баннер если нет
  if (!$('pr_watch_banner')){
    const ws = $('pr_workspace');
    if (ws){
      const banner = document.createElement('div');
      banner.id = 'pr_watch_banner';
      banner.hidden = true;
      banner.style.marginBottom = '12px';
      ws.insertBefore(banner, ws.firstChild);
      banner.addEventListener('click', (e)=>{
        const row = e.target.closest('[data-watch-fid]');
        if (!row) return;
        const fid = row.dataset.watchFid;
        openWatchFile(fid);
      });
    }
  }
  pollWatch();
  watchTimer = setInterval(pollWatch, 4000);
  // 18.8: карточка-статус — главный путь, кнопки — один клик
  const en = $('pr_watch_enable');
  const dis = $('pr_watch_disable');
  const ensure = $('pr_watch_ensure');
  if (en) en.onclick = ()=>setWatchEnabled(true);
  if (dis) dis.onclick = ()=>setWatchEnabled(false);
  if (ensure) ensure.onclick = ensureWatchFolder;
  // слушать SSE watch
  PF.on('watch', (d)=>{
    // live event via bus — fallback polling already
    pollWatch();
  });
}

async function openWatchFile(fid){
  try{
    const data = await get('/api/watch/pending', {limit:20});
    const it = (data.items||[]).find(x=> (x.fid||'')===fid) || (await get('/api/watch/pending',{})).items?.[0];
    if (!it) return fail(new Error('Файл не найден'));
    // показать модалку с превью и действиями
    const detail = await get('/api/estimate', {file: it.name});
    const est = detail.estimate || {};
    const plates = est.plates || it.plates || [];
    const thumbs = detail.detail?.thumbnails || {};
    let html = `<div class="watch-preview"><b>${esc(it.name)}</b><small>${est.total_grams||est.grams||0}г · ${est.total_minutes||est.minutes||0}мин · плит ${plates.length||1}</small>`;
    // показать первую превью если есть
    const firstThumbKey = Object.keys(thumbs)[0];
    if (firstThumbKey){
      html += `<img src="data:image/png;base64,${thumbs[firstThumbKey]}" style="max-width:100%;border-radius:8px;margin:8px 0;max-height:220px;object-fit:contain">`;
    }
    html += `</div>`;
    // AMS required
    const filaments = est.filaments || est.filaments || [];
    if (filaments.length){
      html += `<div class="notice"><span>ℹ</span><span>Требуется: ${filaments.map(f=>esc(f.type + ' ' + (f.color||''))).join(' · ')}</span></div>`;
    }
    // plate selector
    if (plates.length>1){
      html += `<div style="margin:8px 0"><b>Плиты:</b> `+plates.map((p,i)=>`<label class="chk"><input type="checkbox" data-plate-cb value="${i+1}" ${i===0?'checked':''}> Плита ${i+1} (${p.grams}г ${p.minutes}мин)</label>`).join(' ')+`</div>`;
    }
    // 18.8: G-code с проверенным FarmLoop-блоком — можно прямо в конвейер,
    // и катушка со склада подбирается по материалу автоматически
    const mat = (est.estimate?.material || filaments[0]?.type || '');
    if (it.farmloop){
      html += `<div class="notice" style="border-color:var(--accent);margin-top:10px"><span>🔁</span><span>G-code с проверенным <b>FarmLoop-блоком</b> — можно поставить серию в конвейер.</span></div>`;
    }
    // actions
    html += `<div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap"><button class="btn primary" id="watch_enqueue">В очередь</button>`;
    if (it.farmloop){
      html += `<input id="watch_cycles" type="number" min="1" max="100" value="5" style="width:74px" aria-label="Циклов конвейера"><button class="btn" id="watch_enqueue_series">В конвейер</button>`;
    }
    html += `<button class="btn" id="watch_create_order">Создать заказ</button><button class="btn ghost" id="watch_dismiss">Скрыть</button></div>`;
    let dlg = $('watch_modal');
    if (!dlg){
      dlg = document.createElement('dialog');
      dlg.id='watch_modal';
      dlg.className='modal';
      document.body.appendChild(dlg);
    }
    dlg.innerHTML = `<div class="modal-head"><div><span class="eyebrow">Bambu Studio → PrintFlow</span><h2>Новый файл</h2></div><button class="icon-btn" data-close="watch_modal">×</button></div><div class="modal-body">${html}</div>`;
    dlg.showModal();
    dlg.querySelector('[data-close="watch_modal"]').onclick=()=>dlg.close();
    dlg.querySelector('#watch_dismiss').onclick=async()=>{ await post('/api/watch/dismiss',{fid}); dlg.close(); pollWatch(); };
    dlg.querySelector('#watch_create_order').onclick=async()=>{ try{ const r=await post('/api/watch/create-order',{fid}); toast('Заказ создан','№'+r.order.number); }catch(e){fail(e);} };
    async function enqueueWatch(extra){
      const sel = Array.from(dlg.querySelectorAll('[data-plate-cb]:checked')).map(cb=>parseInt(cb.value)) || [1];
      const plate = sel[0]||1;
      // AMS auto-map — только если сам сервер не подберёт катушку со склада
      // (spool с AMS-слотом задаёт маппинг сам, см. pick_warehouse_spool)
      let mapping=[];
      try{
        const pr = PF.livePrinter()?.id || '';
        if (pr && filaments.length){
          const res = await post('/api/printer/ams/auto-map',{printer_id: pr, required: filaments});
          mapping = res.mapping || [];
        }
      }catch(e){}
      const payload = {fid, plate, ams_mapping: mapping, material: mat};
      Object.assign(payload, extra);
      const r = await post('/api/watch/enqueue', payload);
      if (extra.cycles > 1){
        toast('Конвейер: серия в очереди', `${it.name} × ${extra.cycles}${r.spool_id ? ' · катушка ' + r.spool_id : ''}`);
      } else {
        toast('В очереди', it.name + (r.spool_id ? ' · катушка ' + r.spool_id : ''));
      }
      dlg.close(); pollWatch(); PF.refreshCore();
    }
    dlg.querySelector('#watch_enqueue').onclick=async()=>{ try{ await enqueueWatch({}); }catch(e){fail(e);} };
    const seriesBtn = dlg.querySelector('#watch_enqueue_series');
    if (seriesBtn){
      seriesBtn.onclick=async()=>{
        let c = parseInt((dlg.querySelector('#watch_cycles')||{}).value, 10) || 1;
        c = Math.max(1, Math.min(100, c));
        try{ await enqueueWatch({cycles: c}); }catch(e){fail(e);}
      };
    }
  }catch(e){ fail(e); }
}

/* AMS auto-map в модалке печати */
async function enhancePrintModal(){
  const modal = $('print_modal');
  if (!modal) return;
  // добавить контейнер для AMS превью и preflight
  if (!$('pj_ams_preview')){
    const grid = modal.querySelector('.form-grid');
    if (grid){
      const wrap = document.createElement('div');
      wrap.id='pj_ams_preview';
      wrap.className='notice';
      wrap.hidden=true;
      grid.parentNode.insertBefore(wrap, grid.nextSibling);
    }
  }
  if (!$('pj_preflight')){
    const wrap2 = document.createElement('div');
    wrap2.id='pj_preflight';
    wrap2.style.marginTop='10px';
    modal.querySelector('.modal-body').appendChild(wrap2);
  }
  // кнопка авто-мап
  const mappingInput = $('pj_ams_mapping');
  if (mappingInput && !$('pj_automap_btn')){
    const btn = document.createElement('button');
    btn.type='button';
    btn.id='pj_automap_btn';
    btn.className='btn sm';
    btn.textContent='Авто-мап AMS';
    btn.style.marginTop='6px';
    mappingInput.parentNode.appendChild(btn);
    btn.addEventListener('click', async()=>{
      try{
        const file = $('pj_file').value;
        if (!file) return toast('Укажите файл','Сначала выберите файл');
        const est = await get('/api/estimate',{file});
        const filaments = est.estimate?.filaments || est.estimate?.filaments || (est.estimate?.material? [{type: est.estimate.material, color: est.estimate.color_hex||'#CCCCCC'}]:[]);
        if (!filaments.length) return toast('Нет данных о материале','Слайсер не отдал filament_type');
        const prId = $('pj_printer').value || PF.livePrinter()?.id || '';
        const res = await post('/api/printer/ams/auto-map',{printer_id: prId, required: filaments});
        const map = res.mapping || [];
        mappingInput.value = map.filter(x=>x>=0).join(',');
        const wrap = $('pj_ams_preview');
        if (wrap){
          wrap.hidden=false;
          wrap.innerHTML = `<span>ℹ</span><span>Требуется: ${filaments.map(f=>esc(f.type+' '+f.color)).join(' · ')} → Слоты: ${map.join(', ')||'—'}</span>`;
        }
        toast('AMS смаплен', 'Слоты: '+map.join(', '));
      }catch(e){ fail(e); }
    });
  }
  // preflight check on file/printer change
  async function runPreflight(){
    const file = $('pj_file').value;
    const pr = $('pj_printer').value;
    const plate = parseInt($('pj_plate').value||'1');
    const map = ($('pj_ams_mapping').value||'').split(',').map(s=>parseInt(s)).filter(n=>!isNaN(n));
    if (!file || !pr) return;
    try{
      const res = await post('/api/printer/preflight',{printer_id: pr, file, plate, ams_mapping: map});
      const host=$('pj_preflight');
      if (!host) return;
      let html='';
      if (res.blocks?.length){
        html+= `<div class="notice bad"><span>✕</span><span><b>Блоки:</b> ${res.blocks.map(b=>esc(b.title+': '+b.detail)).join('<br>')}</span></div>`;
      }
      if (res.warns?.length){
        html+= `<div class="notice warn"><span>⚠</span><span>${res.warns.map(b=>esc(b.title+': '+b.detail)).join('<br>')}</span></div>`;
      }
      if (res.infos?.length){
        html+= `<div class="notice"><span>ℹ</span><span>${res.infos.map(b=>esc(b.title+': '+b.detail)).join('<br>')}</span></div>`;
      }
      if (!res.blocks?.length && !res.warns?.length){
        html+= `<div class="notice ok"><span>✓</span><span>Preflight ок — можно печатать</span></div>`;
      }
      // estimate info
      if (res.estimate){
        html+= `<small class="muted">Оценка: ${res.estimate.grams||0}г · ${res.estimate.minutes||0}мин · ${esc(res.estimate.material||'')}</small>`;
      }
      host.innerHTML=html;
    }catch(e){}
  }
  $('pj_file')?.addEventListener('change', runPreflight);
  $('pj_printer')?.addEventListener('change', runPreflight);
  $('pj_plate')?.addEventListener('input', runPreflight);
  $('pj_ams_mapping')?.addEventListener('input', runPreflight);
  // перехватить кнопку Печатать для проверки
  const startBtn=$('pj_start');
  if (startBtn && !startBtn.dataset.patched){
    startBtn.dataset.patched='1';
    startBtn.addEventListener('click', async(e)=>{
      // дать preflight шанс
      const file=$('pj_file').value; const pr=$('pj_printer').value;
      if (file&&pr){
        try{
          const res=await post('/api/printer/preflight',{printer_id: pr, file, plate: parseInt($('pj_plate').value||'1'), ams_mapping: ($('pj_ams_mapping').value||'').split(',').map(s=>parseInt(s)).filter(n=>!isNaN(n))});
          if (res.blocks?.length){
            e.preventDefault(); e.stopImmediatePropagation();
            if (!confirm(res.blocks.map(b=>b.title+': '+b.detail).join('\\n')+'\\n\\nВсё равно печатать?')) return;
          } else if (res.warns?.length){
            if (!confirm(res.warns.map(b=>b.title+': '+b.detail).join('\\n')+'\\n\\nПродолжить?')){ e.preventDefault(); e.stopImmediatePropagation(); return; }
          }
        }catch(err){}
      }
    }, true);
  }
}

/* FTPS прогресс — показывается в списке файлов */
function initUploadProgress(){
  // bus event upload_progress
  PF.on('upload_progress', (data)=>{
    // data: {name, sent, total, percent}
    const host = $('pr_files');
    if (!host) return;
    let bar = $('ftps_progress');
    if (!bar){
      bar = document.createElement('div');
      bar.id='ftps_progress';
      bar.style.marginTop='8px';
      host.parentNode.insertBefore(bar, host);
    }
    const pct = data.percent || Math.round((data.sent||0)/(data.total||1)*100);
    bar.innerHTML = `<div class="notice"><span>⏳</span><span>Заливка ${esc(data.name||'файл')} — ${pct}% (${Math.round((data.sent||0)/1024)}КБ)</span></div><div class="bar thin"><i style="width:${pct}%"></i></div>`;
    if (pct>=100) setTimeout(()=>{ bar.innerHTML=''; }, 2000);
  });
}

/* Health бейдж */
function renderHealthBadge(){
  const pr = PF.livePrinter();
  const host = $('pr_health_badge') || $('pr_sub');
  if (!pr || !host) return;
  const h = pr.health || {};
  if (!h.ports) return;
  const ports = h.ports;
  let html = '';
  if (h.needs_developer_mode) html += '<span class="chip bad">Нужен Developer Mode</span> ';
  html += Object.entries(ports).map(([k,v])=> `<span class="chip ${v.ok?'ok':'bad'}">${esc(k)} ${v.ok?'✓':'✕'}</span>`).join(' ');
  if (h.firmware) html += `<small class="muted"> прошивка ${esc(h.firmware)}</small>`;
  // вставить под заголовок принтера если есть контейнер
  let badge = $('pr_health_line');
  if (!badge){
    badge=document.createElement('div');
    badge.id='pr_health_line';
    badge.style.marginTop='6px';
    const ws=$('pr_workspace');
    if (ws) ws.insertBefore(badge, ws.firstChild.nextSibling);
  }
  badge.innerHTML = html;
}

/* Инициализация */
PF.on('ready', ()=>{
  initWatch();
  enhancePrintModal();
  initUploadProgress();
});
PF.on('live', ()=>{
  renderHealthBadge();
});
PF.on('view', (d)=>{
  if (d.view==='printers'){
    // обновить watch статус
    get('/api/watch/status').then(data=>{
      const el=$('watch_status');
      if (el) el.innerHTML=`<span>ℹ</span><span>Watch Folder ${data.enabled?'вкл':'выкл'} · путь <code>${esc(data.path||'')}</code> · ожидают ${data.pending||0}</span>`;
    }).catch(()=>{});
    get('/api/studio/status').then(data=>{
      const el=$('studio_status');
      if (!el) return;
      el.innerHTML=`<span>ℹ</span><span>Шлюз ${data.enabled?'вкл':'выкл'} · режим <b>${esc(data.mode||'confirm')}</b> · ${data.running?'слушает LAN':'без сокетов'} · ${esc(data.name||'')} · ${esc(data.serial||'нет SN')} · MQTT :${data.mqtt_port||8883}</span>`;
    }).catch(()=>{});
  }
});

/* ======================================================== Studio Gateway Confirm Modal */
let activeStudioModalPendingId = '';

function renderStudioConfirmModal(pending) {
  if (!pending) return;
  activeStudioModalPendingId = pending.id;

  let dlg = $('studio_confirm_modal');
  if (!dlg) {
    dlg = document.createElement('dialog');
    dlg.id = 'studio_confirm_modal';
    dlg.className = 'modal';
    document.body.appendChild(dlg);
  }

  const printers = (PF.state.printers || []).filter(p => !p.archived);
  const curPrinterId = pending.printer_id || (PF.livePrinter()?.id) || (printers[0]?.id || '');
  const filaments = pending.filaments || [];
  const thumbs = pending.thumbnails || {};
  const firstThumbKey = Object.keys(thumbs)[0];
  const thumbSrc = firstThumbKey ? `data:image/png;base64,${thumbs[firstThumbKey]}` : '';

  let printerOpts = `<option value="">⚡ Пул: любой свободный принтер</option>` + printers.map(p => {
    const sel = (pending.printer_id && p.id === pending.printer_id) ? 'selected' : '';
    return `<option value="${esc(p.id)}"${sel}>${esc(p.name || p.id)} (${esc(p.model || 'Bambu')})</option>`;
  }).join('');

  const estGrams = Math.round(pending.est_grams || 0);
  const estMins = Math.round(pending.est_minutes || 0);
  const timeFormatted = PF.fmt.minutes(estMins);

  let amsHtml = '';
  if (filaments.length) {
    amsHtml = `<div class="notice" style="margin: 10px 0;"><span>🎨</span><span>Расход: ${filaments.map(f => {
      const col = f.color ? `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#${esc(f.color)};margin:0 4px;vertical-align:middle;border:1px solid rgba(0,0,0,0.2)"></span>` : '';
      return `${col}<b>${esc(f.type || 'PLA')}</b> (${Math.round(f.used_g || f.grams || 0)}г)`;
    }).join(' · ')}</span></div>`;
  }

  dlg.innerHTML = `
    <div class="modal-head">
      <div>
        <span class="eyebrow">Bambu Studio · Нажата кнопка «Печать»</span>
        <h2>${esc(pending.filename || 'Новый проект')}</h2>
      </div>
      <button class="icon-btn" type="button" data-studio-action="reject">×</button>
    </div>
    <div class="modal-body" style="padding-top:12px;">
      ${thumbSrc ? `<div style="text-align:center;margin-bottom:12px;"><img src="${thumbSrc}" style="max-height:180px;max-width:100%;border-radius:8px;object-fit:contain;box-shadow:0 2px 8px rgba(0,0,0,0.15)"></div>` : ''}
      <div style="display:flex;gap:16px;margin-bottom:12px;font-size:14px;color:var(--text);">
        <div>⏱ Время: <b>${timeFormatted}</b></div>
        <div>⚖ Вес: <b>${estGrams} г</b></div>
        <div>Плита: <b>№${pending.plate || 1}</b></div>
      </div>
      ${amsHtml}
      <div class="form-grid" style="grid-template-columns: 1fr 1fr; gap:10px; margin-top:8px;">
        <label class="field">
          <span>Целевой принтер</span>
          <select id="st_conf_printer">${printerOpts}</select>
        </label>
        <label class="field">
          <span>Номер плиты</span>
          <input type="number" id="st_conf_plate" min="1" max="16" value="${pending.plate || 1}">
        </label>
      </div>
      <div style="display:flex;gap:12px;margin-top:10px;font-size:13px;">
        <label class="chk"><input type="checkbox" id="st_conf_bed_level" checked> Калибровка стола</label>
        <label class="chk"><input type="checkbox" id="st_conf_flow_cali"> Flow cali</label>
        <label class="chk"><input type="checkbox" id="st_conf_timelapse"> Таймлапс</label>
      </div>
      <div id="st_conf_error" class="notice bad" style="margin-top:10px;display:none;"></div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:18px;gap:8px;">
        <button class="btn ghost" type="button" data-studio-action="reject">Отклонить</button>
        <div style="display:flex;gap:8px;">
          <button class="btn" type="button" data-studio-action="queue">В очередь</button>
          <button class="btn primary" type="button" data-studio-action="start">▶ Печатать сейчас</button>
        </div>
      </div>
    </div>
  `;

  const btnReject = dlg.querySelectorAll('[data-studio-action="reject"]');
  const btnQueue = dlg.querySelector('[data-studio-action="queue"]');
  const btnStart = dlg.querySelector('[data-studio-action="start"]');
  const errBox = dlg.querySelector('#st_conf_error');

  const doAction = async (action) => {
    const printerId = dlg.querySelector('#st_conf_printer')?.value || '';
    const plate = parseInt(dlg.querySelector('#st_conf_plate')?.value || '1', 10);
    const bedLevel = dlg.querySelector('#st_conf_bed_level')?.checked;
    const flowCali = dlg.querySelector('#st_conf_flow_cali')?.checked;
    const timelapse = dlg.querySelector('#st_conf_timelapse')?.checked;

    if (errBox) { errBox.style.display = 'none'; errBox.textContent = ''; }

    try {
      const res = await post('/api/studio/confirm', {
        pending_id: pending.id,
        action: action,
        printer_id: printerId,
        plate: plate,
        bed_level: bedLevel,
        flow_cali: flowCali,
        timelapse: timelapse,
      });

      dlg.close();
      activeStudioModalPendingId = '';

      if (action === 'start') {
        toast('Печать запущена', pending.filename);
      } else if (action === 'queue') {
        toast('Добавлено в очередь', pending.filename);
      } else {
        toast('Проект отклонен', pending.filename);
      }
      PF.refreshCore();
    } catch (err) {
      if (errBox) {
        errBox.style.display = 'flex';
        errBox.textContent = err.message || 'Ошибка обработки действия';
      } else {
        fail(err);
      }
    }
  };

  btnReject.forEach(b => b.onclick = () => doAction('reject'));
  if (btnQueue) btnQueue.onclick = () => doAction('queue');
  if (btnStart) btnStart.onclick = () => doAction('start');

  if (!dlg.open) dlg.showModal();
}

async function checkPendingStudioProjects() {
  try {
    const res = await get('/api/studio/pending');
    const items = res.items || [];
    if (items.length > 0) {
      // Если модалка не открыта или открыта для другого id
      const top = items[0];
      if (top.mode === 'confirm' && top.id !== activeStudioModalPendingId) {
        const dlg = $('studio_confirm_modal');
        if (!dlg || !dlg.open) {
          renderStudioConfirmModal(top);
        }
      }
    }
  } catch (e) {
    // игнорируем ошибку опроса
  }
}

// Слушаем события шины PrintFlow
PF.on('studio', (data) => {
  if (data && data.pending && data.pending.mode === 'confirm') {
    renderStudioConfirmModal(data.pending);
  } else {
    checkPendingStudioProjects();
  }
});

// Периодическая проверка pending проектов при поллинге
PF.on('live', () => {
  checkPendingStudioProjects();
});

// expose for debug
PF.modules.bridge = { pollWatch, openWatchFile, renderStudioConfirmModal, checkPendingStudioProjects };
})();
