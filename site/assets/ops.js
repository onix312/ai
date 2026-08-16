// Операционная панель: заказы, клиенты и проверка ниш.
// Без зависимостей; данные остаются в localStorage и входят в резервную копию.
(() => {
'use strict';
const KEYS={orders:'ops_orders1',customers:'ops_customers1',statuses:'ops_statuses1',niches:'ops_niches1'};
const DEFAULT_STATUSES=[
 {id:'new',name:'Новая заявка',color:'#64748b'},
 {id:'estimate',name:'Расчёт',color:'#8b5cf6'},
 {id:'prepay',name:'Ждём предоплату',color:'#f59e0b'},
 {id:'queue',name:'Очередь',color:'#0ea5e9'},
 {id:'printing',name:'Печать',color:'#2563eb'},
 {id:'post',name:'Постобработка',color:'#7c3aed'},
 {id:'ready',name:'Готов',color:'#10b981'},
 {id:'done',name:'Выдан',color:'#166534'}
];
const DEFAULT_NICHES=[
 {id:'pets',name:'Товары для питомцев',color:'#ec4899',icon:'🐾',hypothesis:'Полезные и персонализированные аксессуары для владельцев питомцев',target:'Проверить адресники, держатели и организацию зоны питомца',views:0,leads:0,active:true},
 {id:'home',name:'Функциональные товары для дома',color:'#0ea5e9',icon:'🏠',hypothesis:'Органайзеры, крепления и держатели точно под пространство клиента',target:'Найти 3 повторяемых решения с высокой прибылью за час',views:0,leads:0,active:true},
 {id:'business',name:'Товары для локального бизнеса',color:'#8b5cf6',icon:'🏪',hypothesis:'Быстрая оснастка, таблички и органайзеры по размерам бизнеса',target:'Получить 5 постоянных B2B-клиентов',views:0,leads:0,active:true}
];
const load=(k,d)=>{try{const v=JSON.parse(localStorage.getItem(k));return v==null?d:v}catch(e){return d}};
const save=(k,v)=>localStorage.setItem(k,JSON.stringify(v));
const money=n=>Math.round(+n||0).toLocaleString('ru-RU')+' ₽';
const nfmt=n=>(+n||0).toLocaleString('ru-RU');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const uid=p=>p+'_'+Date.now().toString(36)+Math.random().toString(36).slice(2,6);
const iso=()=>new Date().toISOString().slice(0,10);
let orders=load(KEYS.orders,[]), customers=load(KEYS.customers,[]), statuses=load(KEYS.statuses,DEFAULT_STATUSES), niches=load(KEYS.niches,DEFAULT_NICHES);
if(!Array.isArray(orders))orders=[]; if(!Array.isArray(customers))customers=[];
if(!Array.isArray(statuses)||!statuses.length)statuses=DEFAULT_STATUSES;
if(!Array.isArray(niches)||!niches.length)niches=DEFAULT_NICHES;
let orderView=localStorage.getItem('ops_order_view')||'kanban';
let editingOrder=null, editingNiche=null;
const $=id=>document.getElementById(id);
const status=id=>statuses.find(s=>s.id===id)||statuses[0];
const niche=id=>niches.find(n=>n.id===id);
const orderProfit=o=>(+o.price||0)-(+o.cost||0);
// Последняя колонка считается завершением; порядок статусов задаёт сам пользователь.
const isDone=o=>o.status===statuses[statuses.length-1].id;
const dateLabel=d=>d?new Date(d+'T00:00:00').toLocaleDateString('ru-RU',{day:'2-digit',month:'short'}):'Без срока';
function persist(){save(KEYS.orders,orders);save(KEYS.customers,customers);save(KEYS.statuses,statuses);save(KEYS.niches,niches);}
function options(list,value,label='name'){return list.map(x=>`<option value="${esc(x.id)}" ${x.id===value?'selected':''}>${esc(x[label])}</option>`).join('')}

function renderAll(){renderDashboard();renderOrders();renderCustomers();renderNiches();fillSelectors();}
function fillSelectors(){
 const nf=$('of_niche'),sf=$('order_status_filter');
 if(nf)nf.innerHTML='<option value="">Без ниши</option>'+options(niches.filter(n=>n.active!==false),nf.value);
 if(sf){const keep=sf.value;sf.innerHTML='<option value="">Все статусы</option>'+options(statuses,keep);sf.value=keep;}
}
function renderDashboard(){
 const open=orders.filter(o=>!isDone(o)), today=iso();
 const activeHours=open.reduce((s,o)=>s+(+o.hours||0),0);
 const activeGrams=open.reduce((s,o)=>s+(+o.grams||0),0);
 const due=open.filter(o=>o.due&&o.due<=today);
 const awaiting=open.reduce((s,o)=>s+Math.max(0,(+o.price||0)-(+o.prepaid||0)),0);
 const stock=(typeof SPOOLS!=='undefined'?SPOOLS:[]).reduce((s,x)=>s+(+x.g||0),0);
 set('op_active',open.length);set('op_hours',activeHours.toFixed(1)+' ч');set('op_due',due.length);set('op_money',money(awaiting));set('op_material',nfmt(activeGrams)+' г');
 const cap=110, loadPct=Math.min(100,activeHours/cap*100);
 if($('op_load_bar'))$('op_load_bar').style.width=loadPct+'%';
 set('op_load_text',Math.round(loadPct)+'% от 110 ч');
 set('op_stock_text',stock?`${nfmt(stock)} г на складе · очередь потребует ${nfmt(activeGrams)} г`:'Заполните склад пластика в разделе «Производство»');
 const urgent=[...open].sort((a,b)=>(a.due||'9999').localeCompare(b.due||'9999')).slice(0,6);
 const box=$('op_today'); if(box) box.innerHTML=urgent.length?urgent.map(o=>{
  const late=o.due&&o.due<today, st=status(o.status);
  return `<button class="today-row" data-open-order="${o.id}"><span class="today-date ${late?'late':''}">${dateLabel(o.due)}</span><span><b>${esc(o.product||'Без названия')}</b><small>${esc(o.customerName||'Без клиента')} · ${esc(st.name)}</small></span><strong>${(+o.hours||0).toFixed(1)} ч</strong></button>`;
 }).join(''):'<div class="empty-state"><b>Активных заказов нет</b><span>Создайте первый заказ — здесь появятся ближайшие сроки.</span></div>';
 const prod=open.filter(o=>{const i=statuses.findIndex(s=>s.id===o.status);return i>=Math.min(3,statuses.length-1);});
 const pq=$('op_queue');if(pq)pq.innerHTML=prod.length?prod.map(o=>`<div class="queue-line"><i style="background:${status(o.status).color}"></i><span><b>${esc(o.product)}</b><small>${esc(o.material||'Материал не указан')} · ${esc(o.color||'цвет не указан')}</small></span><strong>${(+o.hours||0).toFixed(1)} ч</strong></div>`).join(''):'<div class="empty-state compact"><span>Производственная очередь пуста.</span></div>';
}
function set(id,v){if($(id))$(id).textContent=v;}

function filteredOrders(){
 const q=($('order_search')?.value||'').toLowerCase(), st=$('order_status_filter')?.value||'', ni=$('order_niche_filter')?.value||'';
 return orders.filter(o=>(!q||[o.product,o.customerName,o.phone,o.file,o.notes].join(' ').toLowerCase().includes(q))&&(!st||o.status===st)&&(!ni||o.nicheId===ni));
}
function orderCard(o){const n=niche(o.nicheId), paid=(+o.prepaid||0), left=Math.max(0,(+o.price||0)-paid);return `<article class="order-card" draggable="true" data-order-id="${o.id}">
 <div class="order-card-top"><span class="priority p-${esc(o.priority||'normal')}"></span><small>№${esc(o.number||o.id.slice(-5).toUpperCase())}</small><button class="icon-btn" data-open-order="${o.id}" title="Редактировать">•••</button></div>
 <h4>${esc(o.product||'Без названия')}</h4><p>${esc(o.customerName||'Без клиента')}</p>
 <div class="order-tags">${n?`<span style="--tag:${n.color}">${esc(n.icon)} ${esc(n.name)}</span>`:''}<span>${esc(o.material||'—')} · ${(+o.hours||0).toFixed(1)} ч</span></div>
 <div class="order-card-foot"><span class="due ${o.due&&o.due<iso()?'late':''}">${dateLabel(o.due)}</span><b>${money(o.price)}</b></div>${left?`<small class="debt">Осталось ${money(left)}</small>`:'<small class="paid">Оплачено</small>'}</article>`}
function renderOrders(){
 const list=filteredOrders();
 document.querySelectorAll('[data-order-view]').forEach(b=>b.classList.toggle('active',b.dataset.orderView===orderView));
 if($('orders_kanban'))$('orders_kanban').hidden=orderView!=='kanban';if($('orders_table_wrap'))$('orders_table_wrap').hidden=orderView!=='table';
 const board=$('orders_kanban');if(board)board.innerHTML=statuses.map(s=>{const os=list.filter(o=>o.status===s.id);return `<div class="kanban-col" data-drop-status="${s.id}"><header><span><i style="background:${s.color}"></i>${esc(s.name)}</span><b>${os.length}</b></header><div class="kanban-list">${os.map(orderCard).join('')||'<div class="kanban-empty">Перетащите заказ сюда</div>'}</div></div>`}).join('');
 const tb=$('orders_table_body');if(tb)tb.innerHTML=list.length?list.map(o=>{const s=status(o.status),n=niche(o.nicheId);return `<tr data-open-order="${o.id}"><td><b>№${esc(o.number||o.id.slice(-5).toUpperCase())}</b><small>${dateLabel(o.created)}</small></td><td><b>${esc(o.product)}</b><small>${esc(o.file||'Файл не указан')}</small></td><td>${esc(o.customerName||'—')}<small>${esc(o.phone||'')}</small></td><td>${n?esc(n.icon+' '+n.name):'—'}</td><td><span class="status-pill" style="--status:${s.color}">${esc(s.name)}</span></td><td>${(+o.hours||0).toFixed(1)} ч<small>${nfmt(o.grams)} г</small></td><td><b>${money(o.price)}</b><small>прибыль ${money(orderProfit(o))}</small></td><td class="${o.due&&o.due<iso()&&!isDone(o)?'text-bad':''}">${dateLabel(o.due)}</td></tr>`}).join(''):'<tr><td colspan="8"><div class="empty-state"><b>Заказов пока нет</b><span>Добавьте первый заказ кнопкой справа вверху.</span></div></td></tr>';
 set('orders_count',list.length+' из '+orders.length);
 bindDrag();
}
function bindDrag(){
 document.querySelectorAll('.order-card').forEach(c=>c.addEventListener('dragstart',e=>{e.dataTransfer.setData('text/plain',c.dataset.orderId);c.classList.add('dragging')}));
 document.querySelectorAll('.kanban-col').forEach(c=>{c.addEventListener('dragover',e=>{e.preventDefault();c.classList.add('dragover')});c.addEventListener('dragleave',()=>c.classList.remove('dragover'));c.addEventListener('drop',e=>{e.preventDefault();c.classList.remove('dragover');const o=orders.find(x=>x.id===e.dataTransfer.getData('text/plain'));if(o){o.status=c.dataset.dropStatus;persist();renderAll();}})});
}
function openOrder(id){
 editingOrder=id||null;const o=orders.find(x=>x.id===id)||{created:iso(),due:'',status:statuses[0].id,priority:'normal',quality:'pending',qualityNote:'',qty:1,material:'PLA',price:0,cost:0,prepaid:0,hours:0,grams:0};
 $('order_modal_title').textContent=id?'Заказ №'+(o.number||o.id.slice(-5).toUpperCase()):'Новый заказ';
 ['product','customerName','phone','messenger','channel','qty','material','color','grams','hours','price','cost','prepaid','due','file','notes','priority','quality','qualityNote'].forEach(k=>{const e=$('of_'+k);if(e)e.value=o[k]??''});
 $('of_status').innerHTML=options(statuses,o.status);$('of_niche').innerHTML='<option value="">Без ниши</option>'+options(niches.filter(n=>n.active!==false),o.nicheId);
 $('order_delete').hidden=!id;$('order_modal').showModal();updateOrderCalc();
}
function updateOrderCalc(){const price=+$('of_price').value||0,cost=+$('of_cost').value||0,h=+$('of_hours').value||0,pre=+$('of_prepaid').value||0;set('of_profit',money(price-cost));set('of_ph',h?money((price-cost)/h)+'/ч':'—');set('of_left',money(Math.max(0,price-pre)));}
function saveOrder(){
 const product=$('of_product').value.trim();if(!product){$('of_product').focus();return;}
 let o=orders.find(x=>x.id===editingOrder);if(!o){o={id:uid('o'),number:String((Math.max(0,...orders.map(x=>+x.number||0))+1)).padStart(4,'0'),created:iso()};orders.unshift(o)}
 ['product','customerName','phone','messenger','channel','material','color','due','file','notes','priority','status','nicheId','quality','qualityNote'].forEach(k=>o[k]=$('of_'+k).value.trim());
 ['qty','grams','hours','price','cost','prepaid'].forEach(k=>o[k]=+$('of_'+k).value||0);
 upsertCustomer(o);persist();$('order_modal').close();renderAll();
}
function upsertCustomer(o){if(!o.customerName&&!o.phone)return;let c=customers.find(x=>(o.phone&&x.phone===o.phone)||x.name.toLowerCase()===o.customerName.toLowerCase());if(!c){c={id:uid('c'),created:iso()};customers.push(c)}c.name=o.customerName;c.phone=o.phone;c.messenger=o.messenger||c.messenger||'';}

function renderCustomers(){
 const q=($('customer_search')?.value||'').toLowerCase();const rows=customers.filter(c=>!q||[c.name,c.phone,c.messenger].join(' ').toLowerCase().includes(q));
 const tb=$('customers_body');if(tb)tb.innerHTML=rows.length?rows.map(c=>{const os=orders.filter(o=>(o.phone&&o.phone===c.phone)||o.customerName===c.name),rev=os.reduce((s,o)=>s+(+o.price||0),0),last=[...os].sort((a,b)=>(b.created||'').localeCompare(a.created||''))[0];return `<tr data-customer-orders="${esc(c.phone||c.name)}" title="Показать заказы клиента"><td><b>${esc(c.name||'Без имени')}</b><small>${esc(c.phone||'')}</small></td><td>${esc(c.messenger||'—')}</td><td>${os.length}</td><td><b>${money(rev)}</b></td><td>${last?dateLabel(last.created):'—'}</td><td>${os.length>1?'<span class="repeat-badge">Постоянный</span>':'Новый'}</td></tr>`}).join(''):'<tr><td colspan="6"><div class="empty-state"><span>Клиенты появятся автоматически из заказов.</span></div></td></tr>';
 set('customer_total',customers.length);set('customer_repeat',customers.filter(c=>orders.filter(o=>(o.phone&&o.phone===c.phone)||o.customerName===c.name).length>1).length);
}
function renderNiches(){
 const box=$('niche_grid');if(!box)return;box.innerHTML=niches.filter(n=>n.active!==false).map(n=>{const os=orders.filter(o=>o.nicheId===n.id),buyers=new Set(os.map(o=>o.phone||o.customerName).filter(Boolean)),repeat=[...buyers].filter(x=>os.filter(o=>(o.phone||o.customerName)===x).length>1).length,rev=os.reduce((s,o)=>s+(+o.price||0),0),profit=os.reduce((s,o)=>s+orderProfit(o),0),conv=n.leads?os.length/n.leads*100:0;return `<article class="niche-card" style="--niche:${n.color}"><header><span class="niche-icon">${esc(n.icon||'◆')}</span><button class="icon-btn" data-edit-niche="${n.id}">•••</button></header><h3>${esc(n.name)}</h3><p>${esc(n.hypothesis||'Гипотеза не описана')}</p><div class="funnel"><div><b>${nfmt(n.views)}</b><span>показы</span></div><i>→</i><div><b>${nfmt(n.leads)}</b><span>обращения</span></div><i>→</i><div><b>${os.length}</b><span>заказы</span></div><i>→</i><div><b>${repeat}</b><span>повторы</span></div></div><div class="niche-stats"><span>Конверсия обращений<b>${conv.toFixed(1)}%</b></span><span>Выручка<b>${money(rev)}</b></span><span>Прибыль<b>${money(profit)}</b></span></div><footer><small>${esc(n.target||'Цель не задана')}</small><button class="text-btn" data-edit-niche="${n.id}">Изменить данные</button></footer></article>`}).join('');
}
function openNiche(id){editingNiche=id||null;const n=niches.find(x=>x.id===id)||{name:'',icon:'◆',color:'#2563eb',hypothesis:'',target:'',views:0,leads:0,active:true};['name','icon','color','hypothesis','target','views','leads'].forEach(k=>$('nf_'+k).value=n[k]??'');$('niche_delete').hidden=!id;$('niche_modal_title').textContent=id?'Настройка ниши':'Новая ниша';$('niche_modal').showModal();}
function saveNiche(){const name=$('nf_name').value.trim();if(!name)return;let n=niches.find(x=>x.id===editingNiche);if(!n){n={id:uid('n'),active:true};niches.push(n)}['name','icon','color','hypothesis','target'].forEach(k=>n[k]=$('nf_'+k).value.trim());n.views=+$('nf_views').value||0;n.leads=+$('nf_leads').value||0;persist();$('niche_modal').close();renderAll();}
function openStatuses(){const b=$('status_editor');b.innerHTML=statuses.map((s,i)=>`<div class="status-edit-row"><input type="color" value="${esc(s.color)}" data-status-color="${i}"><input value="${esc(s.name)}" data-status-name="${i}"><button class="icon-btn move-btn" data-status-up="${i}" ${i===0?'disabled':''}>↑</button><button class="icon-btn move-btn" data-status-down="${i}" ${i===statuses.length-1?'disabled':''}>↓</button><button class="icon-btn danger" data-status-delete="${i}" ${statuses.length<2?'disabled':''}>×</button></div>`).join('');if(!$('statuses_modal').open)$('statuses_modal').showModal();}
function readStatusEditor(){document.querySelectorAll('[data-status-name]').forEach(e=>{const i=+e.dataset.statusName;if(statuses[i])statuses[i].name=e.value.trim()||statuses[i].name});document.querySelectorAll('[data-status-color]').forEach(e=>{if(statuses[+e.dataset.statusColor])statuses[+e.dataset.statusColor].color=e.value});}
function saveStatuses(){readStatusEditor();persist();$('statuses_modal').close();renderAll();}
function initFilters(){
 const nf=$('order_niche_filter');if(nf)nf.innerHTML='<option value="">Все ниши</option>'+options(niches.filter(n=>n.active!==false),'');
 ['order_search','order_status_filter','order_niche_filter'].forEach(id=>$(id)?.addEventListener(id==='order_search'?'input':'change',renderOrders));$('customer_search')?.addEventListener('input',renderCustomers);
}
function events(){
 document.addEventListener('click',e=>{
  const o=e.target.closest('[data-open-order]');if(o){openOrder(o.dataset.openOrder);return}
  const n=e.target.closest('[data-edit-niche]');if(n){openNiche(n.dataset.editNiche);return}
  const c=e.target.closest('[data-customer-orders]');if(c){$('order_search').value=c.dataset.customerOrders;renderOrders();location.hash='orders';return}
  const v=e.target.closest('[data-order-view]');if(v){orderView=v.dataset.orderView;localStorage.setItem('ops_order_view',orderView);renderOrders();return}
  const up=e.target.closest('[data-status-up]'),down=e.target.closest('[data-status-down]');
  if(up||down){readStatusEditor();const i=+(up?up.dataset.statusUp:down.dataset.statusDown),j=i+(up?-1:1);if(j>=0&&j<statuses.length){[statuses[i],statuses[j]]=[statuses[j],statuses[i]];openStatuses();}return}
  const del=e.target.closest('[data-status-delete]');if(del){readStatusEditor();const i=+del.dataset.statusDelete;if(statuses.length>1&&confirm('Удалить статус? Заказы из него будут перенесены в первый статус.')){const id=statuses[i].id;statuses.splice(i,1);orders.forEach(o=>{if(o.status===id)o.status=statuses[0].id});openStatuses();}return}
 });
 $('new_order')?.addEventListener('click',()=>openOrder());$('dash_new_order')?.addEventListener('click',()=>openOrder());$('order_save')?.addEventListener('click',saveOrder);$('order_cancel')?.addEventListener('click',()=>$('order_modal').close());$('order_close')?.addEventListener('click',()=>$('order_modal').close());$('order_delete')?.addEventListener('click',()=>{if(confirm('Удалить заказ без возможности восстановления?')){orders=orders.filter(x=>x.id!==editingOrder);persist();$('order_modal').close();renderAll();}});
 $('order_form')?.addEventListener('input',updateOrderCalc);$('manage_statuses')?.addEventListener('click',openStatuses);$('statuses_save')?.addEventListener('click',saveStatuses);$('statuses_cancel')?.addEventListener('click',()=>$('statuses_modal').close());$('status_add')?.addEventListener('click',()=>{statuses.push({id:uid('s'),name:'Новый статус',color:'#64748b'});openStatuses()});
 $('new_niche')?.addEventListener('click',()=>openNiche());$('niche_save')?.addEventListener('click',saveNiche);$('niche_cancel')?.addEventListener('click',()=>$('niche_modal').close());$('niche_delete')?.addEventListener('click',()=>{const used=orders.some(o=>o.nicheId===editingNiche);if(confirm(used?'Архивировать нишу? Связь с заказами сохранится.':'Удалить нишу?')){const n=niches.find(x=>x.id===editingNiche);if(used)n.active=false;else niches=niches.filter(x=>x.id!==editingNiche);persist();$('niche_modal').close();renderAll();}});
 $('export_orders')?.addEventListener('click',()=>{const rows=[['Номер','Создан','Срок','Клиент','Телефон','Изделие','Ниша','Статус','Материал','Цвет','Граммы','Часы','Цена','Себестоимость','Предоплата','Прибыль','Файл','Комментарий'],...orders.map(o=>[o.number,o.created,o.due,o.customerName,o.phone,o.product,niche(o.nicheId)?.name||'',status(o.status).name,o.material,o.color,o.grams,o.hours,o.price,o.cost,o.prepaid,orderProfit(o),o.file,o.notes])];const text='\ufeff'+rows.map(r=>r.map(x=>'"'+String(x??'').replace(/"/g,'""')+'"').join(';')).join('\r\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));a.download='заказы-'+iso()+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),500)});
}
function init(){fillSelectors();initFilters();events();renderAll();}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
