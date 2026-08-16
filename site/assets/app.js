
// ---------- навигация ----------
const gs=[...document.querySelectorAll('.navgroup')];
const secs=[...document.querySelectorAll('section')];
function upd(){
 let cur=secs[0]; const y=window.scrollY+90;
 for(const s of secs){ if(s.offsetTop<=y) cur=s; }
 gs.forEach(g=>{const a=g.querySelector('a.main');
  const on=a.getAttribute('href')==='#'+cur.id;
  a.classList.toggle('on',on); g.classList.toggle('open',on);});
 const t=document.getElementById('top'); if(t) t.style.display=window.scrollY>500?'flex':'none';
}
window.addEventListener('scroll',upd);
const nv=document.getElementById('nav');
document.getElementById('burger').onclick=()=>nv.classList.toggle('show');
nv.addEventListener('click',e=>{if(e.target.tagName==='A'&&innerWidth<900)nv.classList.remove('show')});

// ---------- поиск по навигации ----------
const si=document.getElementById('navsearch');
if(si){si.addEventListener('input',()=>{
 const q=si.value.toLowerCase().trim();
 gs.forEach(g=>{
  const txt=g.textContent.toLowerCase();
  const hit=!q||txt.includes(q);
  g.style.display=hit?'':'none';
  if(q&&hit){g.classList.add('open');
   g.querySelectorAll('a.sub').forEach(s=>{s.style.display=s.textContent.toLowerCase().includes(q)||g.querySelector('a.main').textContent.toLowerCase().includes(q)?'':'none';});
  }else{g.querySelectorAll('a.sub').forEach(s=>s.style.display='');}
 });
 document.querySelectorAll('.navsec').forEach(n=>n.style.display=q?'none':'');
});}

// ---------- копирование блоков ----------
document.querySelectorAll('pre').forEach(p=>{
 const b=document.createElement('button'); b.className='copy'; b.textContent='Копировать';
 b.onclick=()=>{navigator.clipboard.writeText(p.querySelector('code')?p.querySelector('code').innerText:p.innerText);
  b.textContent='Скопировано ✓'; setTimeout(()=>b.textContent='Копировать',1600);};
 p.appendChild(b);
});

// ---------- чекбоксы с сохранением ----------
function initChecks(){
 let i=0;
 document.querySelectorAll('section li').forEach(li=>{
  const m=li.innerHTML.match(/^\s*\[([ xX])\]\s*/);
  if(!m) return;
  const id='chk_'+(i++);
  const checked=localStorage.getItem(id)==='1';
  li.innerHTML=li.innerHTML.replace(/^\s*\[([ xX])\]\s*/,'');
  const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=checked; cb.id=id;
  cb.onchange=()=>localStorage.setItem(id,cb.checked?'1':'0');
  li.insertBefore(cb,li.firstChild);
 });
 updProg();
}
function updProg(){
 const all=document.querySelectorAll('section input[type=checkbox]');
 const done=[...all].filter(c=>c.checked).length;
 const el=document.getElementById('progtxt');
 if(el){el.textContent=done+' / '+all.length;
  document.getElementById('progbar').style.width=(all.length?done/all.length*100:0)+'%';}
}
document.addEventListener('change',e=>{if(e.target.type==='checkbox')updProg();});

// ---------- сохранение полей калькуляторов ----------
function bindSave(){
 document.querySelectorAll('input[data-k],select[data-k]').forEach(inp=>{
  const k='f_'+inp.dataset.k;
  const v=localStorage.getItem(k);
  if(v!==null&&v!=='') inp.value=v;
  inp.addEventListener(inp.tagName==='SELECT'?'change':'input',()=>{localStorage.setItem(k,inp.value);calcAll();});
 });
}
const num=id=>{const e=document.getElementById(id);return e?parseFloat(e.value.replace(',','.'))||0:0;};
const put=(id,txt)=>{const e=document.getElementById(id);if(e)e.textContent=txt;};
const fmt=n=>Math.round(n).toLocaleString('ru-RU');
const fmt2=n=>(Math.round(n*100)/100).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});

// ================================================================
// БАЗА ИЗДЕЛИЙ — вес, часы печати, розничная цена (рынок 2025)
// [категория, название, вес г, часы, цена ₽, отход AMS г]
// ================================================================
const PRESETS=[
 ['🐉 Игрушки и фиджеты','Дракон кристальный 18 см',45,3.0,550,15],
 ['🐉 Игрушки и фиджеты','Дракон кристальный 28 см',110,5.5,890,25],
 ['🐉 Игрушки и фиджеты','Дракон XL 40+ см (витринный)',250,11,1900,40],
 ['🐉 Игрушки и фиджеты','Дракон в яйце (сюрприз)',35,2.2,500,10],
 ['🐉 Игрушки и фиджеты','Змейка подвижная 45 см',40,2.2,350,0],
 ['🐉 Игрушки и фиджеты','Подвижный человечек (тип Dummy 13)',60,5.0,750,0],
 ['🐉 Игрушки и фиджеты','Аксолотль флекси',30,1.8,350,10],
 ['🐉 Игрушки и фиджеты','Осьминог-перевёртыш',22,1.3,300,8],
 ['🐉 Игрушки и фиджеты','Лягушка флекси',25,1.5,300,8],
 ['🐉 Игрушки и фиджеты','Динозавр-скелет флекси',45,2.5,500,0],
 ['🐉 Игрушки и фиджеты','Брелок-кликер антистресс',10,0.6,200,4],
 ['🐉 Игрушки и фиджеты','Мини-фигурка (корзинка «всё по 150»)',8,0.5,150,0],
 ['🐉 Игрушки и фиджеты','Брелок с именем (AMS 2 цвета)',8,0.5,350,4],
 ['🏠 Быт и декор','Подставка под телефон',25,1.3,350,0],
 ['🏠 Быт и декор','Крючок на дверь',12,0.7,150,0],
 ['🏠 Быт и декор','Клипсы для пакетов, 5 шт',15,0.8,200,0],
 ['🏠 Быт и декор','Органайзер для мелочи',70,3.5,500,0],
 ['🏠 Быт и декор','Ваза-спираль',55,2.0,600,0],
 ['🏠 Быт и декор','Ночник-литофан',90,4.5,900,0],
 ['🤝 B2B','Топпер на торт (AMS 2 цвета)',7,0.4,500,3],
 ['🤝 B2B','Вырубка для печенья',12,0.6,250,0],
 ['🤝 B2B','Держатель ценника',10,0.5,250,0],
 ['🤝 B2B','Именная табличка (AMS 2 цвета)',30,1.5,600,8],
 ['🎄 Сезон','Ёлочная игрушка с именем',15,0.8,300,5],
 ['🎁 Наборы','Подарочный набор (дракон + яйцо + коробка)',150,7.5,1400,30]
];

// ================================================================
// КАНАЛЫ ПРОДАЖ — комиссия % + фикс. издержки на заказ, ₽
// ================================================================
const CHANNELS={
 shop:  {n:'Магазин (полка, касса)',        fee:0,   fix:0},
 tg:    {n:'Telegram / перевод на карту',   fee:0,   fix:0},
 avito: {n:'Авито Доставка',                fee:7,   fix:0},
 wb:    {n:'Wildberries FBS',               fee:20,  fix:60},
 ozon:  {n:'Ozon FBS',                      fee:18,  fix:60}
};

// ---------- пресеты в калькуляторе заказа ----------
function buildPreset(){
 const sel=document.getElementById('c_preset'); if(!sel) return;
 let html='<option value="">— свой заказ (вручную) —</option>',cat='';
 PRESETS.forEach((p,i)=>{
  if(p[0]!==cat){if(cat)html+='</optgroup>';html+='<optgroup label="'+p[0]+'">';cat=p[0];}
  html+='<option value="'+i+'">'+p[1]+' · '+p[2]+' г · '+p[3]+' ч</option>';
 });
 html+='</optgroup>'; sel.innerHTML=html;
 sel.addEventListener('change',()=>{
  const i=sel.value; if(i==='')return;
  const p=PRESETS[+i];
  const set=(id,v)=>{const e=document.getElementById(id);e.value=v;localStorage.setItem('f_'+e.dataset.k,v);};
  set('c_w',p[2]); set('c_h',p[3]); set('c_wa',p[5]); set('c_q',1);
  const rp=document.getElementById('c_refprice');
  if(rp) rp.textContent='Рыночная розница на такое: ~'+fmt(p[4])+' ₽';
  calcAll();
 });
}
function buildChan(){
 const sel=document.getElementById('c_chan'); if(!sel) return;
 sel.innerHTML=Object.entries(CHANNELS).map(([k,c])=>'<option value="'+k+'">'+c.n+'</option>').join('');
 sel.addEventListener('change',()=>{
  const c=CHANNELS[sel.value];
  const set=(id,v)=>{const e=document.getElementById(id);e.value=v;localStorage.setItem('f_'+e.dataset.k,v);};
  set('c_fee',c.fee); set('c_fix',c.fix);
  calcAll();
 });
}

// ---------- КАЛЬКУЛЯТОР ЗАКАЗА ----------
function calcOrder(){
 const w=num('c_w'),h=num('c_h'),wa=num('c_wa'),sp=num('c_sp'),sw=num('c_sw'),q=num('c_q'),
  br=num('c_br'),am=num('c_am'),el=num('c_el'),mn=num('c_mn'),hr=num('c_hr'),
  ex=num('c_ex'),pk=num('c_pk'),mk=num('c_mk'),fee=num('c_fee'),fix=num('c_fix');
 const k=1+br/100;
 const mat=sw>0?(w+wa)/sw*sp*k:0, amo=h*am*k, ele=h*el*k, lab=mn/60*hr, oth=ex+pk;
 const cost=mat+amo+ele+lab+oth;
 // цена: желаемый доход + фикс. издержки канала, «в лоб» через комиссию
 const feeK=fee<100?1-fee/100:1;
 const price=Math.ceil((cost*(1+mk/100)+fix)/feeK/10)*10;
 const feeAmt=price*fee/100+fix;
 const net=price-feeAmt;
 const total=price*q;
 const profit=(net-cost)*q;
 const perHour=h>0?profit/(h*q):0;
 const minp=Math.max(400,Math.ceil((cost*1.4+fix)/feeK/10)*10);
 const g=w>0?price/w:0;
 put('r_mat',fmt2(mat)+' ₽'); put('r_amo',fmt2(amo)+' ₽'); put('r_ele',fmt2(ele)+' ₽');
 put('r_lab',fmt2(lab)+' ₽'); put('r_oth',fmt2(oth)+' ₽'); put('r_cost',fmt2(cost)+' ₽');
 put('r_price',fmt(price)+' ₽'); put('r_fee',fee||fix?'−'+fmt(feeAmt)+' ₽':'0 ₽');
 put('r_net',fmt(net)+' ₽'); put('r_total',fmt(total)+' ₽'); put('r_profit',fmt(profit)+' ₽');
 put('r_ph',fmt(perHour)+' ₽/ч'); put('r_min',fmt(minp)+' ₽'); put('r_g',fmt2(g)+' ₽/г');
 const v=document.getElementById('r_verdict');
 if(!v) return;
 if(perHour>=250){v.className='verdict v-ok';v.textContent='✅ Отличный заказ. '+fmt(perHour)+' ₽ чистыми за час печати — это в норме (250–500 ₽/ч). Берите.';}
 else if(perHour>=100){v.className='verdict v-warn';v.textContent='⚠️ Слабовато: '+fmt(perHour)+' ₽/ч чистыми при норме 250–500. Берите ради отзыва или постоянного клиента, иначе поднимите цену.';}
 else {v.className='verdict v-bad';v.textContent='❌ Невыгодно: всего '+fmt(perHour)+' ₽ чистыми за час работы принтера. Поднимите цену, смените канал или откажитесь.';}
}

// ---------- КАЛЬКУЛЯТОР ОКУПАЕМОСТИ ----------
function calcPay(){
 let cum=0, month=0, rows='';
 const inv=num('p_inv');
 for(let m=1;m<=6;m++){
  const rev=num('p_r'+m), exp=num('p_e'+m), tax=rev*num('p_tax')/100;
  const prof=rev-exp-tax; cum+=prof;
  const left=inv-cum;
  if(month===0&&left<=0) month=m;
  rows+=`<tr><td class="lbl">Месяц ${m}</td><td class="out">${fmt(rev)}</td><td class="out">${fmt(exp+tax)}</td><td class="out">${fmt(prof)}</td><td class="out">${fmt(cum)}</td><td class="out" style="color:${left<=0?'#14663a':'#96261a'}">${left<=0?'+'+fmt(-left):'−'+fmt(left)}</td></tr>`;
 }
 const tb=document.getElementById('p_rows'); if(tb) tb.innerHTML=rows;
 const v=document.getElementById('p_verdict');
 if(v){
  if(month){v.className='verdict v-ok';v.innerHTML='✅ Принтер окупается на <b>'+month+'-м месяце</b>. Накоплено за полгода: <b>'+fmt(cum)+' ₽</b>, сверх вложений: <b>'+fmt(cum-inv)+' ₽</b>.';}
  else{v.className='verdict v-warn';v.innerHTML='⚠️ За 6 месяцев не окупается: накоплено '+fmt(cum)+' ₽ из '+fmt(inv)+' ₽. Не хватает <b>'+fmt(inv-cum)+' ₽</b>. Поднимите выручку или сократите расходы.';}
 }
 put('p_need',fmt(inv/6)+' ₽');
 const ph=num('p_ph')||300;
 const hrs=(inv/6)/ph;
 put('p_hours',fmt(hrs)+' ч/мес ≈ '+fmt2(hrs/30)+' ч/день');
}

// ---------- ТЕСТ ПОЛКИ (редактируемый) ----------
const SHELF_KEY='shelf2';
function shelfDefaults(){
 return PRESETS.map(p=>({n:p[1],w:p[2],h:p[3],p:p[4],s:''}));
}
function shelfLoad(){
 try{const d=JSON.parse(localStorage.getItem(SHELF_KEY));if(Array.isArray(d)&&d.length)return d;}catch(e){}
 return shelfDefaults();
}
let SHELF=shelfLoad();
function shelfSave(){localStorage.setItem(SHELF_KEY,JSON.stringify(SHELF));}

function buildShelf(){
 const tb=document.getElementById('sh_body'); if(!tb) return;
 tb.innerHTML=SHELF.map((r,i)=>`<tr>
 <td class="lbl"><input class="sh-name" data-i="${i}" data-f="n" value="${String(r.n).replace(/"/g,'&quot;')}" style="text-align:left;min-width:150px"></td>
 <td><input type="number" step="any" data-i="${i}" data-f="w" value="${r.w}"></td>
 <td><input type="number" step="any" data-i="${i}" data-f="h" value="${r.h}"></td>
 <td class="out" id="sh_c${i}">—</td>
 <td><input type="number" step="any" data-i="${i}" data-f="p" value="${r.p}"></td>
 <td class="out" id="sh_m${i}">—</td><td class="out" id="sh_h${i}">—</td>
 <td><input type="number" step="any" data-i="${i}" data-f="s" value="${r.s}" placeholder="0"></td>
 <td class="out" id="sh_v${i}" style="font-size:11.5px">—</td>
 <td style="text-align:center"><button class="sh-del" data-i="${i}" title="Убрать строку">✕</button></td></tr>`).join('');
}
function calcShelf(){
 const sp=num('c_sp')||1600, sw=num('c_sw')||1000, am=num('c_am')||12, el=num('c_el')||3.5, br=num('c_br')||8;
 let totRev=0,totProf=0,totHrs=0;
 SHELF.forEach((r,i)=>{
  const w=+r.w||0,h=+r.h||0,price=+r.p||0,sold=+r.s||0;
  const cost=(w/sw*sp+h*(am+el))*(1+br/100);
  const marg=price-cost, ph=h>0?marg/h:0;
  totRev+=price*sold; totProf+=marg*sold; totHrs+=h*sold;
  put('sh_c'+i,fmt(cost)); put('sh_m'+i,fmt(marg)); put('sh_h'+i,fmt(ph));
  const v=document.getElementById('sh_v'+i); if(!v) return;
  if(r.s===''||r.s===null){v.textContent='—';v.style.color='#6b7891';}
  else if(sold>=3){v.textContent='МАСШТАБИРОВАТЬ';v.style.color='#14663a';}
  else if(sold>=1){v.textContent='оставить';v.style.color='#8a5a06';}
  else{v.textContent='убрать с полки';v.style.color='#96261a';}
 });
 put('sh_rev',fmt(totRev)+' ₽'); put('sh_prof',fmt(totProf)+' ₽');
 put('sh_hrs',fmt(totHrs)+' ч');
 const cap=document.getElementById('sh_cap');
 if(cap){
  if(totHrs===0){cap.textContent='—';}
  else if(totHrs<=110){cap.textContent='влезает в неделю (реальный потолок P1S ~110–130 ч/нед)';cap.style.color='#14663a';}
  else{cap.textContent='не успеть за неделю на одном принтере! Потолок ~110–130 ч/нед';cap.style.color='#96261a';}
 }
}
function shelfEvents(){
 const tb=document.getElementById('sh_body'); if(!tb) return;
 tb.addEventListener('input',e=>{
  const i=e.target.dataset.i,f=e.target.dataset.f;
  if(i===undefined||!f) return;
  SHELF[+i][f]=f==='n'?e.target.value:e.target.value;
  shelfSave(); calcShelf();
 });
 tb.addEventListener('click',e=>{
  if(!e.target.classList.contains('sh-del')) return;
  SHELF.splice(+e.target.dataset.i,1);
  shelfSave(); buildShelf(); calcShelf();
 });
 const add=document.getElementById('sh_add');
 if(add) add.onclick=()=>{SHELF.push({n:'Новая позиция',w:30,h:1.5,p:300,s:''});shelfSave();buildShelf();calcShelf();};
 const sort=document.getElementById('sh_sort');
 if(sort) sort.onclick=()=>{
  const sp=num('c_sp')||1600, sw=num('c_sw')||1000, am=num('c_am')||12, el=num('c_el')||3.5, br=num('c_br')||8;
  const phOf=r=>{const cost=((+r.w||0)/sw*sp+(+r.h||0)*(am+el))*(1+br/100);return (+r.h||0)>0?((+r.p||0)-cost)/(+r.h):0;};
  SHELF.sort((a,b)=>phOf(b)-phOf(a));
  shelfSave(); buildShelf(); calcShelf();
 };
 const rst=document.getElementById('sh_reset');
 if(rst) rst.onclick=()=>{
  if(!confirm('Вернуть стандартный список из '+PRESETS.length+' позиций? Ваши строки и продажи сотрутся.')) return;
  SHELF=shelfDefaults(); shelfSave(); buildShelf(); calcShelf();
 };
}

function calcAll(){try{calcOrder()}catch(e){} try{calcPay()}catch(e){} try{calcShelf()}catch(e){}}

// ---------- init ----------
buildPreset();
buildChan();
buildShelf();
shelfEvents();
bindSave();
initChecks();
calcAll();
upd();
