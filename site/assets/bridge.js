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
    // actions
    html += `<div style="display:flex;gap:8px;margin-top:12px"><button class="btn primary" id="watch_enqueue">В очередь</button><button class="btn" id="watch_create_order">Создать заказ</button><button class="btn ghost" id="watch_dismiss">Скрыть</button></div>`;
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
    dlg.querySelector('#watch_enqueue').onclick=async()=>{
      const sel = Array.from(dlg.querySelectorAll('[data-plate-cb]:checked')).map(cb=>parseInt(cb.value)) || [1];
      const plate = sel[0]||1;
      // AMS auto-map
      let mapping=[];
      try{
        const pr = PF.livePrinter()?.id || '';
        if (pr && filaments.length){
          const res = await post('/api/printer/ams/auto-map',{printer_id: pr, required: filaments});
          mapping = res.mapping || [];
        }
      }catch(e){}
      try{ await post('/api/watch/enqueue',{fid, plate, ams_mapping: mapping}); toast('В очереди', it.name); dlg.close(); pollWatch(); PF.refreshCore(); }catch(e){fail(e);}
    };
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
  }
});

// expose for debug
PF.modules.bridge = { pollWatch, openWatchFile };
})();
