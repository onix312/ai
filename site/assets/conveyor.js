/* PrintFlow 18.12 — FarmLoop full timeline constructor + metrics + ROI + ΔE + balancer + twin */
(() => {
'use strict';
const { $, esc, num, toast, fail, agoText, dateTimeText, confirmDanger } = PF.ui;
const { get, post } = PF.api;
const { settingGroup } = PF.modules.settings;

const FARMLOOP = [
  ['farmloop_profile', 'FarmLoop: профиль', 'Профиль конвейера', 'text'],
  ['farmloop_mechanics_verified', 'Механика проверена', 'допуск на работу без человека', 'bool'],
  ['farmloop_template_verified', 'Шаблон серии проверен', 'Допуск', 'bool'],
  ['farmloop_pusher_enabled', 'Толкатель установлен', 'Конвейер снимает сам', 'bool'],
  ['farmloop_bender_enabled', 'Изгибатель установлен', 'Детали отгибаются', 'bool'],
  ['farmloop_sensor_mode', 'Подтверждение пустой платформы', 'Как понимать что деталь снята', 'select', [
    ['manual', 'manual — человек'],
    ['sensor', 'sensor — датчик'],
    ['camera', 'camera — камера'],
    ['both', 'both — датчик и камера'],
  ]],
  ['farmloop_sensor_timeout_s', 'Таймаут подтверждения, с', '', 'num', 1],
  ['farmloop_camera_threshold_pct', 'Порог камеры, %', '', 'num', 0.5],
  ['farmloop_cooldown_s', 'Пауза между циклами, с', '', 'num', 1],
  ['farmloop_auto_next', 'Следующий цикл автоматически', '', 'bool'],
  ['farmloop_max_cycles', 'Максимум циклов', '', 'num', 1],
  ['farmloop_unattended_series', 'Бесконтрольная серия', '', 'bool'],
  ['farmloop_max_detach_attempts', 'Попытки снятия', '', 'num', 1],
  ['bed_auto_reference', 'Авто-эталон пустого стола', 'Обновлять эталон при успехе', 'bool'],
  ['farmloop_auto_tune', 'Авто-тюнинг толкателя', 'Увеличивать вылет при фейлах', 'bool'],
];

let bound = false;
let gates = null;
let conveyorSpools = [];
let conveyorModels = [];
let prepFile = null;
let prepSliced = null;

let timelineBlocks = [
  {type:'cool', bed:0, nozzle:0},
  {type:'fan', on:true, p:2, speed:255},
  {type:'cool', bed_wait:35},
  {type:'fan', on:false, p:2},
  {type:'park', z_lift:15},
  {type:'push', x:128, y_start:10, y_end:245, speed:2400, repeat:1},
  {type:'park', home_xy:true},
];
let dragIndex = null;

const put = (id, html) => { const el = $(id); if (el) el.innerHTML = html; return el; };
function slStemOf(name){ const base=String(name||'').split('/').pop()||''; return base.replace(/\.[^.]+$/,''); }
function parseAmsSlot(raw){
  const s=String(raw??'').trim(); if(!s) return null;
  const m=s.match(/(\d+)\s*$/); const cand=m?m[1]:s; const n=Number(cand);
  if(!Number.isFinite(n)) return null; const iv=Math.trunc(n);
  if((iv>=0&&iv<=15)||iv===254) return iv; return null;
}
function filterUsableSpools(list){
  return (list||[]).filter(sp=>{ if(sp.archived) return false; const rem=Number(sp.remaining_grams); if(Number.isFinite(rem)&&rem<=0) return false; return true; });
}

/* статус */
async function loadConveyorStatus(){
  const text=$('cv_status_text'), meta=$('cv_status_meta'), tag=$('cv_status_tag'), head=$('cv_tag');
  if(!text) return;
  try{
    const profile=await get('/api/farmloop/profile');
    if(profile.template_installed){
      if(tag){tag.textContent='Шаблон установлен'; tag.className='tag ok';}
      if(head){head.textContent='Готов'; head.className='tag ok'; head.hidden=false;}
      if(text) text.textContent='Шаблон на месте: файлы серии собираются с блоком автоснятия.';
      if(meta) meta.textContent=`${profile.template_name} · вход: ${profile.input_format} · профиль ${profile.id||''}`;
    }else{
      if(tag){tag.textContent='Ждёт шаблон'; tag.className='tag warn';}
      if(head){head.textContent='Не готов'; head.className='tag warn'; head.hidden=false;}
      if(text) text.textContent=profile.blocked_reason||'Сначала соберите механику FarmLoop Stage 1.';
      if(meta) meta.textContent=`Профиль ${profile.id||''} · вход: ${profile.input_format||''}`;
    }
  }catch(e){
    if(tag){tag.textContent='Нет связи'; tag.className='tag bad';}
    if(head){head.textContent='Нет связи'; head.className='tag bad'; head.hidden=false;}
    if(text) text.textContent='Не удалось проверить конвейер.';
    if(meta) meta.textContent='';
  }
}
function renderConveyorSettings(){ put('cv_settings', settingGroup(FARMLOOP)); }
function renderConveyorGates(){
  const note=$('cv_gate_note'); if(!note) return; if(!gates){note.hidden=true; return;}
  const s=gates.settings||{}; const chain=[];
  if(s.can_prepare) chain.push('шаблон'); if(s.can_auto_next) chain.push('автоснятие'); if(s.can_unattended_series) chain.push('серия без человека');
  if(!s.can_prepare){ note.hidden=false; note.className='cv-gate-note warn'; note.textContent='⚠ Конвейер не готов: '+(gates.blocked_reason||'нет шаблона'); return; }
  if(s.can_auto_next&&s.can_unattended_series){ note.hidden=false; note.className='cv-gate-note ok'; note.textContent='✓ Все допуски: '+chain.join(' → '); return; }
  if(s.can_auto_next){ note.hidden=false; note.className='cv-gate-note info'; note.textContent='✓ Автоснятие готово. Для серии без человека включите допуск «Бесконтрольная серия»'; return; }
  note.hidden=false; note.className='cv-gate-note warn'; note.textContent='⚠ Шаблон на месте, но автоснятие не включено: '+(gates.blocked_reason||'');
}
async function loadConveyorGates(){
  try{ const data=await get('/api/farmloop/settings'); gates=data; renderConveyorGates(); }
  catch(e){ const note=$('cv_gate_note'); if(note){ note.hidden=false; note.className='cv-gate-note warn'; note.textContent='Гейты не подтянулись: '+(e&&e.message?e.message:'нет связи'); } }
}
async function saveConveyorSettings(){
  const host=$('cv_settings'), btn=$('cv_save'); if(!host) return; if(btn) btn.disabled=true;
  try{
    const payload={}; host.querySelectorAll('[data-setting]').forEach(el=>{ const k=el.dataset.setting; if(el.type==='checkbox') payload[k]=el.checked; else if(el.type==='number') payload[k]=num(el.value); else payload[k]=el.value; });
    const res=await post('/api/settings', payload); PF.setSettings(res.settings||{}); renderConveyorSettings(); toast('Конвейер сохранён','Допуски применены'); loadConveyorGates();
  }catch(e){ fail(e); } finally{ if(btn) btn.disabled=false; }
}
async function loadConveyorHistory(){
  const host=$('cv_history'); if(!host) return;
  let data; try{ data=await get('/api/events',{kind:'farmloop',limit:'30'}); }catch(e){ host.innerHTML=`<div class="cv-history-empty">Журнал не подтянулся: ${esc(e&&e.message?e.message:'нет связи')}</div>`; return; }
  const events=data.events||[]; if(!events.length){ host.innerHTML='<div class="cv-history-empty">Событий конвейера ещё нет.</div>'; return; }
  host.innerHTML=events.map(e=>`<div class="cv-event"><span class="cv-event-dot"></span><div class="cv-event-body"><b>${esc(e.title||'Событие')}</b><small>${esc(e.detail||'')} · ${dateTimeText(e.at||'')} · ${agoText(e.at||'')}</small></div></div>`).join('');
}

/* тестовая очистка */
let testCleanPrinter='';
function testCleanSelectable(p){ const connected=!!(p.connection&&p.connection.connected); const state=String((p.printer&&p.printer.state)||'').toUpperCase(); return connected&&(state==='IDLE'||state==='FINISH'); }
async function loadTestCleanPrinters(){
  const sel=$('cv_test_clean_printer'); if(!sel) return;
  let printers=[]; try{ const state=await get('/api/state'); printers=Array.isArray(state.printers)?state.printers:[]; }catch{ sel.innerHTML='<option value="">Станок: первый свободный</option>'; sel.disabled=true; return; }
  sel.disabled=false; const opts=['<option value="">Станок: первый свободный</option>'];
  printers.forEach(p=>{ const id=String(p.id||''); if(!id) return; const free=testCleanSelectable(p); const stateLabel=String((p.printer&&(p.printer.state_label||p.printer.state))||'нет связи'); opts.push(`<option value="${esc(id)}">${esc(p.name||id)} — ${esc(stateLabel)} ${free?'✓':'·'}</option>`); });
  sel.innerHTML=opts.join(''); sel.value=printers.some(p=>String(p.id||'')===testCleanPrinter)?testCleanPrinter:''; testCleanPrinter=sel.value;
}
async function handleTestClean(){
  const sel=$('cv_test_clean_printer'); const printerId=(sel&&sel.value)||''; const where=printerId?`на выбранном станке (${(sel.options[sel.selectedIndex]||{}).text||printerId})`:'на первом свободном станке';
  const msg='Проверьте, что стол свободен, сопло остыло, корзина установлена.\n\nЗапустить тестовый проход толкателя по шаблону FarmLoop '+where+'?';
  if(!confirmDanger(msg)) return;
  const btn=$('cv_test_clean_btn'); if(btn) btn.disabled=true;
  try{ const payload={confirmed:true}; if(printerId) payload.printer_id=printerId; const res=await post('/api/farmloop/test-clean', payload); toast('Тестовая очистка стола',`Команд: ${res.executed_commands} на «${res.printer}»`); await Promise.all([loadConveyorHistory(), loadTestCleanPrinters()]); }catch(e){ fail(e); loadTestCleanPrinters(); } finally{ if(btn) btn.disabled=false; }
}

/* конструктор */
async function loadConstructorTemplate(){
  try{
    const res=await get('/api/farmloop/timeline');
    if(res.exists && Array.isArray(res.blocks) && res.blocks.length){ timelineBlocks=res.blocks; }
    else{
      const legacy=await get('/api/farmloop/template'); const b=legacy.blocks||{};
      const tempEl=$('cv_c_temp'); if(tempEl) tempEl.value=b.cooldown_temp||35;
      const zEl=$('cv_c_zlift'); if(zEl) zEl.value=b.z_lift||15;
      const yEl=$('cv_c_y'); if(yEl) yEl.value=b.pusher_y||245;
      const speedEl=$('cv_c_speed'); if(speedEl) speedEl.value=b.pusher_speed||2400;
      const fanEl=$('cv_c_fan'); if(fanEl) fanEl.checked=!!b.fan_assist;
      const customEl=$('cv_c_custom'); if(customEl) customEl.value=b.custom_gcode||'';
    }
    renderTimeline();
    previewTimeline();
  }catch{}
}
async function saveConstructorTemplate(resetDefault=false){
  const btn=$('cv_c_save'), rBtn=$('cv_c_reset'), st=$('cv_c_status');
  if(btn) btn.disabled=true; if(rBtn) rBtn.disabled=true; if(st) st.textContent='Сохраняем…';
  try{
    let payload;
    if(resetDefault){
      payload={reset_default:true};
      timelineBlocks=[
        {type:'cool', bed:0, nozzle:0},
        {type:'fan', on:true, p:2, speed:255},
        {type:'cool', bed_wait:35},
        {type:'fan', on:false, p:2},
        {type:'park', z_lift:15},
        {type:'push', x:128, y_start:10, y_end:245, speed:2400, repeat:1},
        {type:'park', home_xy:true},
      ];
    }else{
      const legacyMode = !$('cv_timeline') || $('cv_timeline').children.length===0;
      if(legacyMode && $('cv_c_temp')){
        payload={blocks:{cooldown_temp:num($('cv_c_temp')?.value,35), fan_assist:!!$('cv_c_fan')?.checked, z_lift:num($('cv_c_zlift')?.value,15), pusher_y:num($('cv_c_y')?.value,245), pusher_speed:num($('cv_c_speed')?.value,2400), custom_gcode:$('cv_c_custom')?.value||''}};
      }else{
        payload={blocks:timelineBlocks};
      }
    }
    let res;
    if(payload.reset_default || (payload.blocks && !Array.isArray(payload.blocks))){
      res=await post('/api/farmloop/template', payload);
    }else{
      res=await post('/api/farmloop/timeline/save', {blocks:timelineBlocks});
    }
    const prev=$('cv_c_preview'); if(prev&&res.gcode) prev.textContent=res.gcode;
    toast('Шаблон FarmLoop сохранён',`Блоков: ${timelineBlocks.length}`);
    if(st) st.textContent='✓ Шаблон сохранён и проверен';
    await Promise.all([loadConveyorStatus(), loadConveyorGates(), loadConstructorTemplate()]);
  }catch(e){ fail(e); if(st) st.textContent=`Ошибка: ${e.message||e}`; } finally{ if(btn) btn.disabled=false; if(rBtn) rBtn.disabled=false; }
}
function toggleGcodePreview(){
  const prev=$('cv_c_preview'), btn=$('cv_c_preview_btn'); if(!prev) return;
  prev.hidden=!prev.hidden; if(btn) btn.textContent=prev.hidden?'Показать итоговый G-code':'Скрыть G-code';
}

/* TIMELINE FULL CONSTRUCTOR */
function renderTimeline(){
  ensureTimelinePalette();
  const host=$('cv_timeline'); if(!host) return;
  host.innerHTML='';
  timelineBlocks.forEach((b, idx)=>{
    const div=document.createElement('div');
    div.className='cv-tl-block';
    div.draggable=true;
    div.dataset.idx=idx;
    let label='', desc='';
    switch(b.type){
      case 'cool':
        if(b.bed_wait!=null){ label=`Cool bed R${b.bed_wait}°`; desc=`Охлаждение до ${b.bed_wait}°`; }
        else{ label=`Cool B${b.bed??0} N${b.nozzle??0}`; desc=`M140 S${b.bed} M104 S${b.nozzle}`; }
        break;
      case 'fan': label=b.on?`Fan P${b.p||2} ON ${b.speed||255}`:`Fan P${b.p||2} OFF`; desc=b.on?'Обдув':'Выкл обдув'; break;
      case 'park':
        if(b.home_xy){ label='Park Home XY'; desc='G28 X Y'; }
        else if(b.z_lift!=null){ label=`Lift Z+${b.z_lift}`; desc=`Подъём ${b.z_lift}мм`; }
        else{ label=`Park X${b.x||''} Y${b.y||''}`; desc='Парковка'; }
        break;
      case 'push': label=`Push Y${b.y_start||10}→${b.y_end||245} x${b.repeat||1}`; desc=`Толкатель F${b.speed||2400}`; break;
      case 'dwell': label=`Dwell ${b.ms||1000}ms`; desc='Пауза'; break;
      case 'custom': label='Custom'; desc=(b.gcode||'').slice(0,60); break;
      default: label=b.type; desc='';
    }
    div.innerHTML=`<div class="cv-tl-drag">☰</div><div class="cv-tl-info"><b>${esc(label)}</b><small>${esc(desc)}</small></div><div class="cv-tl-actions"><button class="btn sm ghost" data-act="edit">✎</button><button class="btn sm ghost" data-act="del">✕</button></div>`;
    div.addEventListener('dragstart', e=>{ dragIndex=idx; e.dataTransfer.effectAllowed='move'; div.classList.add('dragging'); });
    div.addEventListener('dragend', ()=>{ div.classList.remove('dragging'); dragIndex=null; });
    div.addEventListener('dragover', e=>{
      e.preventDefault();
      if(dragIndex!=null && dragIndex!==idx){
        const from=dragIndex; const to=idx;
        const item=timelineBlocks.splice(from,1)[0];
        timelineBlocks.splice(to,0,item);
        dragIndex=to;
        renderTimeline();
        previewTimeline();
      }
    });
    div.querySelectorAll('[data-act]').forEach(btn=>{
      btn.addEventListener('click', ()=>{
        const act=btn.dataset.act;
        if(act==='del'){ timelineBlocks.splice(idx,1); renderTimeline(); previewTimeline(); }
        if(act==='edit'){ editBlock(idx); }
      });
    });
    host.appendChild(div);
  });
}

function ensureTimelinePalette(){
  if($('cv_tl_palette')) return;
  const host=$('cv_constructor'); if(!host) return;
  const pal=document.createElement('div'); pal.id='cv_tl_palette'; pal.className='cv-tl-palette';
  pal.innerHTML=`
    <div class="cv-tl-palette-title">Добавить блок</div>
    <div class="cv-tl-palette-btns">
      <button class="btn sm" data-add="cool">Cool</button>
      <button class="btn sm" data-add="fan">Fan</button>
      <button class="btn sm" data-add="park">Park</button>
      <button class="btn sm" data-add="push">Push</button>
      <button class="btn sm" data-add="dwell">Dwell</button>
      <button class="btn sm" data-add="custom">Custom</button>
    </div>
    <div class="cv-tl-live">
      <div id="cv_tl_gcode" class="cv-gcode-preview" style="max-height:200px;overflow:auto;background:#111;color:#0f0;padding:8px;font-family:monospace;font-size:11px;white-space:pre"></div>
      <small id="cv_tl_status" class="muted"></small>
      <div><canvas id="cv_tl_canvas" width="256" height="256" style="border:1px solid #ccc;max-width:256px;background:#f5f5f5"></canvas></div>
    </div>
  `;
  host.appendChild(pal);
  if(!$('cv_timeline')){
    const tl=document.createElement('div'); tl.id='cv_timeline'; tl.className='cv-timeline'; pal.insertBefore(tl, pal.querySelector('.cv-tl-live'));
  }
  pal.querySelectorAll('[data-add]').forEach(btn=>{
    btn.addEventListener('click', ()=>{ addBlock(btn.dataset.add); });
  });
}

function addBlock(type){
  let block={type};
  switch(type){
    case 'cool': block={type:'cool', bed_wait:35}; break;
    case 'fan': block={type:'fan', on:true, p:2, speed:255}; break;
    case 'park': block={type:'park', x:128, y:10}; break;
    case 'push': block={type:'push', x:128, y_start:10, y_end:245, speed:2400, repeat:1}; break;
    case 'dwell': block={type:'dwell', ms:1000}; break;
    case 'custom': block={type:'custom', gcode:'; custom'}; break;
  }
  timelineBlocks.push(block);
  renderTimeline();
  previewTimeline();
}

function editBlock(idx){
  const b=timelineBlocks[idx]; if(!b) return;
  const json=prompt('Редактировать блок JSON:', JSON.stringify(b, null, 2));
  if(!json) return;
  try{ const nb=JSON.parse(json); if(nb.type) { timelineBlocks[idx]=nb; renderTimeline(); previewTimeline(); } }catch(e){ alert('Ошибка JSON: '+e.message); }
}

async function previewTimeline(){
  const gEl=$('cv_tl_gcode'), sEl=$('cv_tl_status'), canvas=$('cv_tl_canvas');
  if(!gEl) return;
  try{
    const res=await post('/api/farmloop/timeline/preview', {blocks:timelineBlocks});
    gEl.textContent=res.gcode||'';
    if(sEl){
      const warns=res.warnings||[];
      sEl.textContent=warns.length?'⚠ '+warns.join('; '):'✓ Валидно';
      sEl.className=warns.length?'muted warn':'muted ok';
    }
    if(canvas && res.preview_path){
      drawPreviewPath(canvas, res.preview_path);
    }
    const mainPrev=$('cv_c_preview'); if(mainPrev) mainPrev.textContent=res.gcode||'';
  }catch(e){
    if(sEl) sEl.textContent='Ошибка: '+(e.message||e);
  }
}

function drawPreviewPath(canvas, path){
  const ctx=canvas.getContext('2d'); if(!ctx) return;
  ctx.clearRect(0,0,256,256);
  ctx.fillStyle='#f5f5f5'; ctx.fillRect(0,0,256,256);
  ctx.strokeStyle='#ddd'; ctx.strokeRect(0,0,256,256);
  ctx.strokeStyle='#bbb'; ctx.strokeRect(10,10,236,236);
  // projection overlay (4): if bedProjection.corners exists, draw quadrilateral
  if(bedProjection && bedProjection.corners && Array.isArray(bedProjection.corners) && bedProjection.corners.length===4){
    try{
      ctx.strokeStyle='#ff00ff'; ctx.lineWidth=2; ctx.beginPath();
      bedProjection.corners.forEach((c,i)=>{
        const x=10+ (c[0]||0)*236;
        const y=10+ (c[1]||0)*236;
        if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
      });
      ctx.closePath(); ctx.stroke();
      ctx.fillStyle='rgba(255,0,255,0.1)'; ctx.fill();
    }catch{}
  }
  path.forEach(seg=>{
    const x=10+ (seg.x||0)/256*236;
    const y=10+ (seg.y||0)/256*236;
    const x2=10+ (seg.x2||seg.x||0)/256*236;
    const y2=10+ (seg.y2||seg.y||0)/256*236;
    ctx.beginPath();
    ctx.moveTo(x,y);
    ctx.lineTo(x2,y2);
    ctx.strokeStyle=seg.type==='push'?'#e00': seg.type==='return'?'#0a0':'#00a';
    ctx.lineWidth=seg.type==='push'?3:1;
    ctx.stroke();
  });
}

/* метрики / twin / roi / delta / balancer */
async function loadMetrics(){
  const host=$('cv_metrics'); if(!host) return;
  try{
    const m=await get('/api/farmloop/metrics');
    host.innerHTML=`<h3>Метрики FarmLoop</h3><div class="cv-metrics"><span>Успех: ${m.success||0}</span> <span>Фейл: ${m.fail||0}</span> <span>Успешность: ${m.success_rate||0}%</span> <span>Всего: ${m.jobs_done||0}</span></div>`;
  }catch{}
}
async function loadDigitalTwin(){
  const host=$('cv_twin'); if(!host) return;
  try{
    const twin=await get('/api/farmloop/digital-twin');
    host.innerHTML=`<h3>Цифровой двойник</h3><div class="cv-twin"><b>${esc(twin.printer_id||'')}</b> · ${esc(twin.state||'')} · очередь ${twin.queue_len||0} · толкатель ${twin.push_mm||15}мм · стол ${twin.bed_cleared?'пуст':'занят'}</div>`;
  }catch{}
}
async function loadROI(){
  const host=$('cv_roi'); if(!host) return;
  try{
    const data=await get('/api/farmloop/roi'); const roi=data.roi||[0.1,0.1,0.9,0.9];
    if(!host.querySelector('#roi_x0')){
      host.innerHTML=`<h3>ROI редактор</h3><div class="cv-roi"><label>x0<input id="roi_x0" type="number" step="0.01" min="0" max="1" value="${roi[0]}"></label><label>y0<input id="roi_y0" type="number" step="0.01" min="0" max="1" value="${roi[1]}"></label><label>x1<input id="roi_x1" type="number" step="0.01" min="0" max="1" value="${roi[2]}"></label><label>y1<input id="roi_y1" type="number" step="0.01" min="0" max="1" value="${roi[3]}"></label><button class="btn sm" id="roi_save">Сохранить ROI</button></div>`;
    }else{
      $('roi_x0').value=roi[0]; $('roi_y0').value=roi[1]; $('roi_x1').value=roi[2]; $('roi_y1').value=roi[3];
    }
    const btn=$('roi_save');
    if(btn && !btn._bound){ btn._bound=true; btn.addEventListener('click', async()=>{
      const vals=[num($('roi_x0')?.value,0.1), num($('roi_y0')?.value,0.1), num($('roi_x1')?.value,0.9), num($('roi_y1')?.value,0.9)];
      try{ await post('/api/farmloop/roi',{roi:vals}); toast('ROI сохранён', vals.join(',')); }catch(e){ fail(e); }
    });}
  }catch{}
}
async function searchSpoolsDelta(hex){
  if(!hex) return;
  try{
    const res=await get('/api/farmloop/spools/delta',{hex, material:''});
    const host=$('cv_delta'); if(!host) return;
    const list=(res.spools||[]).map(s=>`<div>${esc(s.material||'')} ${esc(s.color_name||'')} ${esc(s.color_hex||'')} ΔE ${s._delta_e} · ${Math.round(s.remaining_grams||0)}г</div>`).join('');
    if(!host.querySelector('#cv_delta_input')){
      host.innerHTML=`<h3>ΔE поиск катушек</h3><div class="cv-delta-controls"><input id="cv_delta_input" placeholder="#FF0000" class="field" value="${esc(hex)}"><button class="btn sm" id="cv_delta_btn">Найти</button></div><div id="cv_delta_results">${list||'Нет катушек'}</div>`;
      const btn=$('cv_delta_btn'); if(btn) btn.addEventListener('click', ()=>searchSpoolsDelta($('cv_delta_input')?.value||''));
    }else{
      const r=$('cv_delta_results'); if(r) r.innerHTML=list||'Нет катушек';
    }
  }catch{}
}
async function loadBalancer(){
  const host=$('cv_balancer'); if(!host) return;
  try{
    const res=await get('/api/farmloop/queue/balance');
    if(!res.ok){ host.innerHTML=`<h3>Балансировщик очереди</h3><div>${esc(res.error||'ошибка')}</div>`; return; }
    const plan=(res.plan||[]).map(p=>`<div>Job ${esc(p.job_id.slice(0,8))} → ${esc(p.printer_id)} score ${p.score} ${esc(p.material||'')}</div>`).join('');
    host.innerHTML=`<h3>Балансировщик очереди</h3><div>Заданий ${res.jobs} · принтеров ${res.printers}</div>${plan||'<div>Нет плана</div>'}`;
  }catch(e){ host.innerHTML=`<h3>Балансировщик</h3><div>${esc(e.message||'нет связи')}</div>`; }
}

let bedProjection = null;
async function loadBedProjection(){
  try{
    const data=await get('/api/farmloop/bed/projection');
    bedProjection=data.projection||null;
    const host=$('cv_projection');
    if(host){
      if(bedProjection){
        host.innerHTML=`<h3>Проекция стола</h3><div class="muted small">${esc(JSON.stringify(bedProjection).slice(0,200))}</div><button class="btn sm" id="proj_clear">Сбросить</button>`;
        const btn=$('proj_clear'); if(btn && !btn._bound){ btn._bound=true; btn.addEventListener('click', async()=>{ await post('/api/farmloop/bed/projection',{projection:{}}); bedProjection=null; toast('Проекция сброшена',''); }); }
      }else{
        host.innerHTML=`<h3>Проекция стола</h3><div class="muted">Нет калибровки. Кликните 4 угла стола на камере (front-left, front-right, back-right, back-left) в формате corners:[[x,y],...] 0..1</div><div><input id="proj_input" class="field" placeholder='{\"corners\":[[0.1,0.9],[0.9,0.9],[0.9,0.1],[0.1,0.1]]}'><button class="btn sm" id="proj_save">Сохранить</button></div>`;
        const btn=$('proj_save'); if(btn && !btn._bound){ btn._bound=true; btn.addEventListener('click', async()=>{
          try{ const val=JSON.parse($('proj_input').value||'{}'); await post('/api/farmloop/bed/projection',{projection:val}); bedProjection=val; toast('Проекция сохранена',''); loadBedProjection(); previewTimeline(); }catch(e){ fail(e); }
        });}
      }
    }
  }catch{}
}

async function loadAIDetect(){
  const host=$('cv_ai'); if(!host) return;
  try{
    const res=await get('/api/farmloop/ai-detect');
    if(!res.ok){ host.innerHTML=`<h3>AI детектор</h3><div>${esc(res.error||'')}</div>`; return; }
    const ai=res.ai||{};
    host.innerHTML=`<h3>AI детектор</h3><div>diff ${ai.diff_pct||0}% · ${ai.ai_stub?'⚠ спагетти':'ок'} · принтер ${esc(res.printer_id||'')}</div>`;
  }catch(e){ const host=$('cv_ai'); if(host) host.innerHTML=`<h3>AI детектор</h3><div>${esc(e.message||'')}</div>`; }
}

async function loadForce(){
  const host=$('cv_force'); if(!host) return;
  try{
    const res=await get('/api/farmloop/force');
    if(!res.ok){ host.innerHTML=`<h3>Force feedback</h3><div>${esc(res.error||'')}</div>`; return; }
    host.innerHTML=`<h3>Force feedback</h3><div>Толкатель ${res.push_mm}мм · tuned ${res.tuned}мм · сила ~${res.force_estimate}N · успех ${res.metrics?.success_rate||0}%</div><div><input id="force_input" type="number" min="5" max="60" step="1" value="${res.push_mm}" class="field" style="width:80px"><button class="btn sm" id="force_save">Сохранить</button></div>`;
    const btn=$('force_save'); if(btn && !btn._bound){ btn._bound=true; btn.addEventListener('click', async()=>{
      try{ const v=num($('force_input').value,15); await post('/api/farmloop/force',{push_mm:v, printer_id:res.printer_id||''}); toast('Force сохранён', v+'мм'); loadForce(); }catch(e){ fail(e); }
    });}
  }catch{}
}

async function loadAMSMap(){
  const host=$('cv_amsmap'); if(!host) return;
  try{
    const res=await get('/api/farmloop/ams/mappings',{limit:20});
    if(!res.ok){ host.innerHTML=`<h3>AMS маппинг</h3><div>${esc(res.error||'')}</div>`; return; }
    const rows=(res.mappings||[]).map(m=>`<div>${esc(m.filename||m.file_hash||'')} · ${esc(m.printer_id||'')} · [${(m.mapping||[]).join(',')}] · ${esc(m.updated_at||'')}</div>`).join('');
    host.innerHTML=`<h3>AMS маппинг (persistence 12)</h3>${rows||'Нет сохранённых'}`;
  }catch{}
}

async function loadAccountingReport(){
  const host=$('cv_accounting'); if(!host) return;
  try{
    const res=await get('/api/farmloop/accounting/report',{days:7});
    if(!res.ok){ host.innerHTML=`<h3>Экономика конвейера</h3><div>${esc(res.error||'')}</div>`; return; }
    const acc=res.accounting||{};
    const farm=res.farmloop||{};
    host.innerHTML=`<h3>Экономика конвейера (7д)</h3><div>Доход ${acc.income||0} · Расход ${acc.expense||0} · Прибыль ${acc.profit||0} · FarmLoop успех ${farm.success_rate||0}% (${farm.success||0}/${farm.total||0})</div>`;
  }catch{}
}

/* warehouse spools */
async function loadWarehouseSpools(){
  try{ const res=await get('/api/spools'); conveyorSpools=filterUsableSpools(res.spools||[]); renderSpoolOptions(); }catch{ conveyorSpools=[]; }
}
function renderSpoolOptions(){
  const sel=$('cv_prep_spool'); if(!sel) return;
  const currentVal=sel.value;
  const opts=['<option value="">Авто-подбор по материалу</option>'];
  filterUsableSpools(conveyorSpools).forEach(s=>{
    const slotNum=parseAmsSlot(s.ams_slot); const slot=slotNum!=null?` · AMS ${slotNum}`:(s.ams_slot?` · AMS ${esc(s.ams_slot)}`:''); const rem=s.remaining_grams?` · ${Math.round(s.remaining_grams)}г`:'';
    const delta=s._delta_e!=null?` ΔE ${s._delta_e}`:'';
    opts.push(`<option value="${esc(s.id)}">${esc(s.material||'PLA')} ${esc(s.color_name||'')} ${esc(s.color_hex||'')}${slot}${rem}${delta}</option>`);
  });
  sel.innerHTML=opts.join(''); if(currentVal) sel.value=currentVal;
}
function autoSelectSpool(material){
  const sel=$('cv_prep_spool'); if(!sel||!material) return;
  const matLower=String(material).toLowerCase(); const usable=filterUsableSpools(conveyorSpools);
  const found=usable.find(s=>String(s.material||'').toLowerCase()===matLower && (Number(s.remaining_grams)||0)>0);
  if(found) sel.value=found.id;
}

/* library */
async function loadLibraryModels(){
  const sel=$('cv_library_select'); if(!sel) return;
  try{ const res=await get('/api/library',{kind:'stl'}); conveyorModels=res.files||[]; const opts=['<option value="">Или выберите модель из библиотеки…</option>']; conveyorModels.forEach(m=>{ opts.push(`<option value="${esc(m.id)}">${esc(m.name||m.id)}${m.dedup?' · dedup':''}</option>`); }); sel.innerHTML=opts.join(''); }catch{ if(sel) sel.innerHTML='<option value="">Не удалось загрузить модели</option>'; }
}
async function selectModel(id, name){
  if(!id){ resetPrepMaster(); return; }
  prepFile={type:'mesh', id, name:name||id}; const pBox=$('cv_prep_params'), rBox=$('cv_prep_result'), st=$('cv_prep_status');
  if(pBox) pBox.hidden=false; if(rBox) rBox.hidden=true; if(st) st.textContent='Считаем план модели…';
  try{ const plan=await post('/api/slicer/plan',{id}); if(st){ const b=plan.model&&plan.model.box_mm?plan.model.box_mm.map(x=>Math.round(x)).join('×')+' мм':''; st.textContent=`✓ Модель готова (${b}).`; } autoSelectSpool((plan.settings&&plan.settings.material)||'PLA'); }catch(e){ if(st) st.textContent=`План: ${e.message||e}`; }
}
async function handleFileUpload(file){
  if(!file) return;
  const name=String(file.name||''); const isMesh=/\.(stl|obj)$/i.test(name); const isGcodeOr3mf=/\.(3mf|gcode)$/i.test(name);
  if(!isMesh&&!isGcodeOr3mf){ fail(new Error('Поддерживаются .stl,.obj,.3mf,.gcode')); return; }
  const dzSub=document.querySelector('.cv-dropzone-sub'); if(dzSub) dzSub.textContent=`Загружаем «${name}»…`;
  try{
    if(isMesh){
      const form=new FormData(); form.append('file',file); const res=await post('/api/library/upload', form); await loadLibraryModels(); const sel=$('cv_library_select'); if(sel) sel.value=res.id; await selectModel(res.id,res.name);
      if(res.dedup) toast('Дедуп','Файл уже есть — использована существующая копия');
    }else{
      const form=new FormData(); form.append('file',file); const res=await post('/api/estimate/upload', form);
      handleGcodeReady({file:res.file||name, grams:num(res.grams)||(res.estimate&&num(res.estimate.total_grams))||0, minutes:num(res.minutes)||(res.estimate&&num(res.estimate.total_minutes))||0, material:res.material||'', layers:(res.estimate&&res.estimate.layers)||0});
    }
  }catch(e){ fail(e); } finally{ if(dzSub) dzSub.textContent='или нажмите кнопку для выбора файла с компьютера'; }
}
function handleGcodeReady(info){
  const c=Math.max(1,Math.min(100,num($('cv_prep_cycles')?.value,1))); const spoolSel=$('cv_prep_spool'); const spool=filterUsableSpools(conveyorSpools).find(s=>s.id===(spoolSel&&spoolSel.value))||null;
  prepSliced={output:info.file, stem:slStemOf(info.file), minutes:info.minutes||0, grams:info.grams||0, layers:info.layers||0, material:(spool&&spool.material)||info.material||'PLA', spool, cycles:c, farmloop_profile:'bambu-p1s-farmloop-stage1'};
  renderPrepResult(prepSliced);
}
async function handleSlice(){
  if(!prepFile||prepFile.type!=='mesh') return;
  const btn=$('cv_prep_slice'), st=$('cv_prep_status'); if(btn) btn.disabled=true; if(st) st.textContent='Нарезаем модель Stage 1…';
  const layer=num($('cv_prep_layer')?.value,0.20), infill=num($('cv_prep_infill')?.value,15), cycles=Math.max(1,Math.min(100,num($('cv_prep_cycles')?.value,1))), spoolId=$('cv_prep_spool')?.value||'', isFarm=$('cv_prep_farm')?.checked;
  const payload={id:prepFile.id, layer_height:layer, infill_percent:infill, cycles};
  const spool=filterUsableSpools(conveyorSpools).find(s=>s.id===spoolId); if(spool){ payload.spool_id=spool.id; if(spool.material) payload.material=spool.material; const slotNum=parseAmsSlot(spool.ams_slot); if(slotNum!=null) payload.ams_slot=slotNum; else if(spool.ams_slot) payload.ams_slot=spool.ams_slot; }
  if(isFarm) payload.farmloop_profile=(gates&&gates.profile)||'bambu-p1s-farmloop-stage1';
  try{ const res=await post('/api/slicer/slice', payload); const report=res.report||{}; prepSliced={output:res.output, stem:slStemOf(res.output), minutes:report.minutes||0, grams:report.grams||0, layers:report.layers||0, material:(spool&&spool.material)||report.material||'PLA', spool, cycles, farmloop_profile:payload.farmloop_profile||''}; renderPrepResult(prepSliced); toast('Модель нарезана',`${report.minutes||0} мин · ${report.grams||0} г`); }catch(e){ fail(e); if(st) st.textContent=`Ошибка нарезки: ${e.message||e}`; } finally{ if(btn) btn.disabled=false; }
}
function renderPrepResult(data){
  const pBox=$('cv_prep_params'), rBox=$('cv_prep_result'), kpis=$('cv_prep_kpis'), enqBtn=$('cv_prep_enqueue'), dlBtn=$('cv_prep_download');
  if(pBox) pBox.hidden=true; if(rBox) rBox.hidden=false;
  const c=data.cycles||1; const slotNum=data.spool?parseAmsSlot(data.spool.ams_slot):null; const spoolDesc=data.spool?`${data.spool.material} ${data.spool.color_name||''} (AMS: ${slotNum!=null?slotNum:(data.spool.ams_slot||'—')})`:'По умолчанию';
  if(kpis){ kpis.innerHTML=`<div class="cv-prep-kpi"><span>Время печати</span><b>≈ ${data.minutes} мин</b></div><div class="cv-prep-kpi"><span>Расход</span><b>≈ ${data.grams} г</b></div><div class="cv-prep-kpi"><span>Слоёв</span><b>${data.layers||'—'}</b></div><div class="cv-prep-kpi"><span>Катушка</span><b>${esc(spoolDesc)}</b></div><div class="cv-prep-kpi"><span>Файл</span><b style="font-size:12px;overflow:hidden;text-overflow:ellipsis">${esc(data.output)}</b></div><div class="cv-prep-kpi"><span>Блок FarmLoop</span><b style="color:var(--ok)">✓ Внедрён</b></div>`; }
  if(enqBtn) enqBtn.textContent=`В конвейер (${c} детал${c===1?'ь':(c<5?'и':'ей')})`;
  if(dlBtn&&data.output){ dlBtn.hidden=false; dlBtn.href=`/api/uploads?file=${encodeURIComponent(data.output)}`; }
}
async function handleEnqueueSeries(){
  if(!prepSliced) return;
  const c=Math.max(1,Math.min(100,num(prepSliced.cycles)||1));
  const msg=`Поставить в конвейер серию из ${c} деталей подряд?\n\nЗадания будут запускаться автоматически, пока конвейер подтверждает сброс деталей.`;
  if(!confirmDanger(msg)) return;
  const btn=$('cv_prep_enqueue'); if(btn) btn.disabled=true;
  try{
    const spool=prepSliced.spool; const slotNum=spool?parseAmsSlot(spool.ams_slot):null;
    const payload={file:prepSliced.output, name:prepSliced.stem||prepSliced.output, plate:1, no_auto:1, allow_auto_start:false, cycles:c, source:'printflow-conveyor', farmloop_profile:prepSliced.farmloop_profile||'bambu-p1s-farmloop-stage1', est_minutes:prepSliced.minutes||0, est_grams:prepSliced.grams||0, material:(spool&&spool.material)||prepSliced.material||'', spool_id:(spool&&spool.id)||'', ams_mapping:slotNum!=null?[slotNum]:[]};
    await post('/api/jobs/enqueue', payload); toast('Серия в конвейере',`${c} заданий поставлены в очередь.`); await loadConveyorHistory(); resetPrepMaster();
  }catch(e){ fail(e); } finally{ if(btn) btn.disabled=false; }
}
function resetPrepMaster(){
  prepFile=null; prepSliced=null;
  const pBox=$('cv_prep_params'), rBox=$('cv_prep_result'), sel=$('cv_library_select'), fileInput=$('cv_file_input'), st=$('cv_prep_status'), dl=$('cv_prep_download');
  if(pBox) pBox.hidden=true; if(rBox) rBox.hidden=true; if(sel) sel.value=''; if(fileInput) fileInput.value=''; if(st) st.textContent=''; if(dl) dl.hidden=true;
}
function initDropzone(){
  const dz=$('cv_dropzone'), fileInput=$('cv_file_input'), browseBtn=$('cv_browse_btn');
  if(!dz||dz._bound) return; dz._bound=true;
  if(browseBtn&&fileInput){ browseBtn.addEventListener('click', e=>{ e.stopPropagation(); fileInput.click(); }); }
  dz.addEventListener('click', ()=>{ if(fileInput) fileInput.click(); });
  if(fileInput){
    fileInput.multiple=true;
    fileInput.addEventListener('change', ()=>{
      if(fileInput.files){
        Array.from(fileInput.files).forEach(f=>handleFileUpload(f));
        fileInput.value='';
      }
    });
  }
  ['dragenter','dragover'].forEach(type=>{ dz.addEventListener(type, e=>{ e.preventDefault(); e.stopPropagation(); dz.classList.add('dragover'); }); });
  ['dragleave','drop'].forEach(type=>{ dz.addEventListener(type, e=>{ e.preventDefault(); e.stopPropagation(); dz.classList.remove('dragover'); }); });
  dz.addEventListener('drop', e=>{
    const files=e.dataTransfer&&e.dataTransfer.files;
    if(files){ Array.from(files).forEach(f=>handleFileUpload(f)); }
  });
}

/* bind */
function bind(){
  if(bound) return; bound=true;
  const save=$('cv_save'); if(save) save.addEventListener('click', saveConveyorSettings);
  const refresh=$('cv_refresh'); if(refresh) refresh.addEventListener('click', refreshAll);
  const testClean=$('cv_test_clean_btn'); if(testClean) testClean.addEventListener('click', handleTestClean);
  const testCleanSel=$('cv_test_clean_printer'); if(testCleanSel) testCleanSel.addEventListener('change', ()=>{ testCleanPrinter=testCleanSel.value; });
  const cSave=$('cv_c_save'); if(cSave) cSave.addEventListener('click', ()=>saveConstructorTemplate(false));
  const cReset=$('cv_c_reset'); if(cReset) cReset.addEventListener('click', ()=>saveConstructorTemplate(true));
  const cPrev=$('cv_c_preview_btn'); if(cPrev) cPrev.addEventListener('click', toggleGcodePreview);
  const libSel=$('cv_library_select'); if(libSel) libSel.addEventListener('change', ()=>selectModel(libSel.value));
  const libRef=$('cv_library_refresh'); if(libRef) libRef.addEventListener('click', loadLibraryModels);
  const sliceBtn=$('cv_prep_slice'); if(sliceBtn) sliceBtn.addEventListener('click', handleSlice);
  const cancelBtn=$('cv_prep_cancel'); if(cancelBtn) cancelBtn.addEventListener('click', resetPrepMaster);
  const enqBtn=$('cv_prep_enqueue'); if(enqBtn) enqBtn.addEventListener('click', handleEnqueueSeries);
  const resReset=$('cv_prep_reset'); if(resReset) resReset.addEventListener('click', resetPrepMaster);
  initDropzone();
  const view=$('view-conveyor'); if(view){
    if(!$('cv_metrics')){ const d=document.createElement('div'); d.id='cv_metrics'; d.className='card'; d.innerHTML='<h3>Метрики FarmLoop</h3>'; view.appendChild(d); }
    if(!$('cv_twin')){ const d=document.createElement('div'); d.id='cv_twin'; d.className='card'; d.innerHTML='<h3>Цифровой двойник</h3>'; view.appendChild(d); }
    if(!$('cv_roi')){ const d=document.createElement('div'); d.id='cv_roi'; d.className='card'; d.innerHTML='<h3>ROI редактор</h3>'; view.appendChild(d); }
    if(!$('cv_delta')){ const d=document.createElement('div'); d.id='cv_delta'; d.className='card'; d.innerHTML='<h3>ΔE поиск катушек</h3><input id="cv_delta_input" placeholder="#FF0000" class="field"><button class="btn sm" id="cv_delta_btn">Найти</button><div id="cv_delta_results"></div>'; view.appendChild(d); const inp=$('cv_delta_input'), btn=$('cv_delta_btn'); if(btn) btn.addEventListener('click', ()=>searchSpoolsDelta(inp?.value||'')); }
    if(!$('cv_balancer')){ const d=document.createElement('div'); d.id='cv_balancer'; d.className='card'; d.innerHTML='<h3>Балансировщик очереди</h3>'; view.appendChild(d); }
    if(!$('cv_projection')){ const d=document.createElement('div'); d.id='cv_projection'; d.className='card'; d.innerHTML='<h3>Проекция стола</h3>'; view.appendChild(d); }
    if(!$('cv_ai')){ const d=document.createElement('div'); d.id='cv_ai'; d.className='card'; d.innerHTML='<h3>AI детектор</h3>'; view.appendChild(d); }
    if(!$('cv_force')){ const d=document.createElement('div'); d.id='cv_force'; d.className='card'; d.innerHTML='<h3>Force feedback</h3>'; view.appendChild(d); }
    if(!$('cv_amsmap')){ const d=document.createElement('div'); d.id='cv_amsmap'; d.className='card'; d.innerHTML='<h3>AMS маппинг</h3>'; view.appendChild(d); }
    if(!$('cv_accounting')){ const d=document.createElement('div'); d.id='cv_accounting'; d.className='card'; d.innerHTML='<h3>Экономика конвейера</h3>'; view.appendChild(d); }
  }
}

async function refreshAll(){
  renderConveyorSettings();
  await Promise.all([loadConveyorStatus(), loadConveyorGates(), loadConveyorHistory(), loadConstructorTemplate(), loadLibraryModels(), loadWarehouseSpools(), loadTestCleanPrinters(), loadMetrics(), loadDigitalTwin(), loadROI(), loadBalancer(), loadBedProjection(), loadAIDetect(), loadForce(), loadAMSMap(), loadAccountingReport()]);
}

PF.module('conveyor', ()=>{ bind(); refreshAll(); });
PF.on('view', detail=>{ if(!detail||detail.view!=='conveyor') return; bind(); if(!gates) refreshAll(); });

})();
