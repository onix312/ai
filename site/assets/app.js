
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
const num=id=>{const e=document.getElementById(id);return e?parseFloat(String(e.value).replace(',','.'))||0:0;};
const put=(id,txt)=>{const e=document.getElementById(id);if(e)e.textContent=txt;};
const fmt=n=>Math.round(n).toLocaleString('ru-RU');
const fmt2=n=>(Math.round(n*100)/100).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});
const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const today=()=>new Date().toISOString().slice(0,10);
const ruDate=s=>{const d=new Date(s);return isNaN(d)?s:d.toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit'});};

// ================================================================
// БАЗА ИЗДЕЛИЙ — стартовый набор (рынок 2025)
// [категория, название, вес г, часы, цена ₽, отход AMS г, шт на стол]
// ================================================================
const PRESETS=[
 ['🐉 Игрушки и фиджеты','Дракон кристальный 18 см',45,3.0,550,15,2],
 ['🐉 Игрушки и фиджеты','Дракон кристальный 28 см',110,5.5,890,25,1],
 ['🐉 Игрушки и фиджеты','Дракон XL 40+ см (витринный)',250,11,1900,40,1],
 ['🐉 Игрушки и фиджеты','Дракон в яйце (сюрприз)',35,2.2,500,10,4],
 ['🐉 Игрушки и фиджеты','Змейка подвижная 45 см',40,2.2,350,0,2],
 ['🐉 Игрушки и фиджеты','Подвижный человечек (тип Dummy 13)',60,5.0,750,0,2],
 ['🐉 Игрушки и фиджеты','Аксолотль флекси',30,1.8,350,10,6],
 ['🐉 Игрушки и фиджеты','Осьминог-перевёртыш',22,1.3,300,8,8],
 ['🐉 Игрушки и фиджеты','Лягушка флекси',25,1.5,300,8,8],
 ['🐉 Игрушки и фиджеты','Динозавр-скелет флекси',45,2.5,500,0,4],
 ['🐉 Игрушки и фиджеты','Брелок-кликер антистресс',10,0.6,200,4,12],
 ['🐉 Игрушки и фиджеты','Мини-фигурка (корзинка «всё по 150»)',8,0.5,150,0,20],
 ['🐉 Игрушки и фиджеты','Брелок с именем (AMS 2 цвета)',8,0.5,350,4,16],
 ['🏠 Быт и декор','Подставка под телефон',25,1.3,350,0,4],
 ['🏠 Быт и декор','Крючок на дверь',12,0.7,150,0,12],
 ['🏠 Быт и декор','Клипсы для пакетов, 5 шт',15,0.8,200,0,6],
 ['🏠 Быт и декор','Органайзер для мелочи',70,3.5,500,0,2],
 ['🏠 Быт и декор','Ваза-спираль',55,2.0,600,0,2],
 ['🏠 Быт и декор','Ночник-литофан',90,4.5,900,0,2],
 ['🤝 B2B','Топпер на торт (AMS 2 цвета)',7,0.4,500,3,6],
 ['🤝 B2B','Вырубка для печенья',12,0.6,250,0,8],
 ['🤝 B2B','Держатель ценника',10,0.5,250,0,12],
 ['🤝 B2B','Именная табличка (AMS 2 цвета)',30,1.5,600,8,4],
 ['🎄 Сезон','Ёлочная игрушка с именем',15,0.8,300,5,10],
 ['🎁 Наборы','Подарочный набор (дракон + яйцо + коробка)',150,7.5,1400,30,1]
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

// ================================================================
// ЕДИНЫЙ СПРАВОЧНИК ПОЗИЦИЙ — источник данных для всех инструментов
// {c:категория, n:название, w:вес г, h:часы, p:цена ₽, a:отход AMS г}
// ================================================================
const CAT_KEY='catalog1', SHELF_KEY='shelf3', HIST_KEY='hist1', PLAN_KEY='plan1', SPOOL_KEY='spool1', BK_KEY='bk_last';
const store=(k,def)=>{try{const d=JSON.parse(localStorage.getItem(k));return d==null?def:d;}catch(e){return def;}};
const save=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v));}catch(e){alert('Не удалось сохранить: память браузера переполнена. Сделайте резервную копию и удалите лишние фото/записи.');}};

function catDefaults(){return PRESETS.map(p=>({c:p[0],n:p[1],w:p[2],h:p[3],p:p[4],a:p[5],f:p[6]}));}
// добросить «шт на стол» в базы, сохранённые до появления партий
function catMigrate(list){
 list.forEach(r=>{if(r.f===undefined||r.f===''||!(+r.f>0)){
  const d=PRESETS.find(p=>p[1]===r.n); r.f=d?d[6]:1;}});
 return list;
}
let CATALOG=(()=>{const d=store(CAT_KEY,null);return Array.isArray(d)&&d.length?catMigrate(d):catDefaults();})();
function catSave(){save(CAT_KEY,CATALOG);}

// продажи за текущую неделю: {название: количество}
let SALES=store(SHELF_KEY,{})||{};
function salesSave(){save(SHELF_KEY,SALES);}

// история недель: [{d:дата, rev, prof, hrs, s:{название:продано}}]
let HIST=store(HIST_KEY,[])||[];
function histSave(){save(HIST_KEY,HIST);}

// очередь печати: [{n:название, q:шт}]
let PLAN=store(PLAN_KEY,[])||[];
function planSave(){save(PLAN_KEY,PLAN);}

// склад пластика: [{c:цвет, t:тип, g:остаток г, pr:цена катушки ₽}]
function spoolDefaults(){return [
 {c:'Чёрный',t:'PLA',g:800,pr:1600},
 {c:'Белый',t:'PLA',g:650,pr:1600},
 {c:'Красный',t:'PLA',g:400,pr:1600},
 {c:'Золотой/шёлк',t:'PLA Silk',g:900,pr:1900}
];}
let SPOOLS=(()=>{const d=store(SPOOL_KEY,null);return Array.isArray(d)&&d.length?d:spoolDefaults();})();
function spoolSave(){save(SPOOL_KEY,SPOOLS);}

// ---------- общие настройки себестоимости (из первого калькулятора) ----------
function settings(){
 return {sp:num('c_sp')||1600, sw:num('c_sw')||1000, am:num('c_am')||12,
         el:num('c_el')||3.5, br:num('c_br')||8,
         oh:num('c_oh'), ams:num('c_amsk')||15, batch:!document.getElementById('c_batch')||document.getElementById('c_batch').checked};
}

// ================================================================
// ПАРТИОННЫЙ РАСЧЁТ — «куча игрушек на столе»
// Печать пачкой меняет экономику: разогрев/калибровка платится один раз
// за ПЛИТУ, а не за штуку; отход AMS на смену цвета тоже делится на всю
// плиту (пруж/башня одна на слой, а не на каждую фигурку).
// batchOf(изделие, кол-во) -> {plates, hours, grams, ...}
// ================================================================
function batchOf(r,qty,st){
 st=st||settings();
 const q=Math.max(0,+qty||0);
 const w=+r.w||0, h=+r.h||0, a=+r.a||0;
 const fit=st.batch?Math.max(1,Math.round(+r.f||1)):1;   // сколько влезает на стол
 const plates=q>0?Math.ceil(q/fit):0;                    // сколько запусков
 const perPlate=q>0?Math.min(fit,q):0;
 const oh=st.oh;                                         // разогрев+калибровка, мин на плиту
 const printH=h*q;                                       // чистая печать
 const ohH=plates*oh/60;                                 // накладные часы
 const hours=printH+ohH;
 // отход AMS: на плите он общий, но чуть растёт с числом моделей на ней
 const perPlateWaste=a>0?a*(1+Math.max(0,perPlate-1)*(st.ams/100)):0;
 const wasteG=a>0?(plates>0?perPlateWaste*plates:0):0;
 const soloWaste=a*q;                                    // если бы печатали по одной
 const k=1+st.br/100;
 const grams=(w*q+wasteG)*k;
 const mat=st.sw>0?grams/st.sw*st.sp:0;
 const machine=hours*(st.am+st.el)*k;
 const cost=mat+machine;
 return {q:q,fit:fit,plates:plates,perPlate:perPlate,hours:hours,printH:printH,ohH:ohH,
         grams:grams,wasteG:wasteG*k,soloWaste:soloWaste*k,cost:cost,mat:mat,machine:machine,
         unit:q>0?cost/q:0,unitH:q>0?hours/q:0};
}
// себестоимость печати без учёта вашего труда — то, что вычитается для «₽ за час принтера»
// qty>1 -> честная партионная себестоимость за штуку
function costOf(w,h,a,st,f,qty){
 st=st||settings();
 if(qty&&qty>1) return batchOf({w:w,h:h,a:a,f:f||1},qty,st).unit;
 const k=1+st.br/100;
 const oh=st.oh/60*(st.am+st.el)*k;
 return ((+w||0)+(+a||0))/st.sw*st.sp*k + (+h||0)*(st.am+st.el)*k + oh;
}
const costOfItem=(r,st,qty)=>costOf(r.w,r.h,r.a,st,r.f,qty);
const NORM=250; // норма ₽ чистыми за час печати
const CAP=110;  // реальный потолок часов печати в неделю на один принтер

// ---------- пресеты в калькуляторе заказа ----------
function buildPreset(){
 const sel=document.getElementById('c_preset'); if(!sel) return;
 const keep=sel.value;
 let html='<option value="">— свой заказ (вручную) —</option>',cat=null;
 CATALOG.forEach((p,i)=>{
  if(p.c!==cat){if(cat!==null)html+='</optgroup>';html+='<optgroup label="'+esc(p.c)+'">';cat=p.c;}
  html+='<option value="'+i+'">'+esc(p.n)+' · '+p.w+' г · '+p.h+' ч · по '+(p.f||1)+' на стол</option>';
 });
 if(cat!==null)html+='</optgroup>';
 sel.innerHTML=html;
 if(keep&&sel.querySelector('option[value="'+keep+'"]')) sel.value=keep;
 if(!sel.dataset.bound){sel.dataset.bound='1';
  sel.addEventListener('change',()=>{
   const i=sel.value; if(i==='')return;
   const p=CATALOG[+i]; if(!p) return;
   const set=(id,v)=>{const e=document.getElementById(id);if(!e)return;e.value=v;localStorage.setItem('f_'+e.dataset.k,v);};
   set('c_w',p.w); set('c_h',p.h); set('c_wa',p.a); set('c_fit',p.f||1);
   const rp=document.getElementById('c_refprice');
   if(rp) rp.textContent='Рыночная розница на такое: ~'+fmt(p.p)+' ₽';
   calcAll();
  });
 }
}
function buildChan(){
 const sel=document.getElementById('c_chan'); if(!sel||sel.dataset.bound) return;
 sel.innerHTML=Object.entries(CHANNELS).map(([k,c])=>'<option value="'+k+'">'+c.n+'</option>').join('');
 sel.dataset.bound='1';
 sel.addEventListener('change',()=>{
  const c=CHANNELS[sel.value];
  const set=(id,v)=>{const e=document.getElementById(id);if(!e)return;e.value=v;localStorage.setItem('f_'+e.dataset.k,v);};
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
 const st=settings();
 const fit=Math.max(1,Math.round(num('c_fit')||1));
 const B=batchOf({w:w,h:h,a:wa,f:fit},Math.max(1,q),st);
 // на штуку — уже с учётом того, что плита греется один раз на всю партию
 const mat=B.mat/B.q, amo=B.hours*am*k/B.q, ele=B.hours*el*k/B.q;
 const lab=mn/60*hr, oth=ex+pk;
 const cost=mat+amo+ele+lab+oth;
 // сравнение «по одной» против партии
 const S=batchOf({w:w,h:h,a:wa,f:1},Math.max(1,q),st);
 const soloCost=S.cost/S.q+lab+oth;
 const saveTotal=(soloCost-cost)*B.q, saveH=S.hours-B.hours;
 put('r_plates',B.plates+' шт ('+B.perPlate+' на столе)');
 put('r_bhours',fmt2(B.hours)+' ч');
 put('r_ohh',B.ohH>0?'+'+fmt2(B.ohH)+' ч':'0 ч');
 put('r_bwaste',fmt(B.wasteG)+' г');
 const sv=document.getElementById('r_save');
 if(sv){
  if(B.q>1&&B.fit>1&&saveTotal>1){sv.textContent='−'+fmt(saveTotal)+' ₽ и −'+fmt2(saveH)+' ч против печати по одной';sv.style.color='#14663a';}
  else if(B.q>1&&B.fit<=1){sv.textContent='влезает по 1 шт — партия не помогает';sv.style.color='#6b7891';}
  else{sv.textContent='—';sv.style.color='#6b7891';}
 }
 const feeK=fee<100?1-fee/100:1;
 const price=Math.ceil((cost*(1+mk/100)+fix)/feeK/10)*10;
 const feeAmt=price*fee/100+fix;
 const net=price-feeAmt;
 const total=price*q;
 const profit=(net-cost)*q;
 const perHour=B.hours>0?profit/B.hours:0;
 const minp=Math.max(400,Math.ceil((cost*1.4+fix)/feeK/10)*10);
 const g=w>0?price/w:0;
 put('r_mat',fmt2(mat)+' ₽'); put('r_amo',fmt2(amo)+' ₽'); put('r_ele',fmt2(ele)+' ₽');
 put('r_lab',fmt2(lab)+' ₽'); put('r_oth',fmt2(oth)+' ₽'); put('r_cost',fmt2(cost)+' ₽');
 put('r_price',fmt(price)+' ₽'); put('r_fee',fee||fix?'−'+fmt(feeAmt)+' ₽':'0 ₽');
 put('r_net',fmt(net)+' ₽'); put('r_total',fmt(total)+' ₽'); put('r_profit',fmt(profit)+' ₽');
 put('r_ph',fmt(perHour)+' ₽/ч'); put('r_min',fmt(minp)+' ₽'); put('r_g',fmt2(g)+' ₽/г');
 const v=document.getElementById('r_verdict');
 if(!v) return;
 if(perHour>=NORM){v.className='verdict v-ok';v.textContent='✅ Отличный заказ. '+fmt(perHour)+' ₽ чистыми за час печати — это в норме (250–500 ₽/ч). Берите.';}
 else if(perHour>=100){v.className='verdict v-warn';v.textContent='⚠️ Слабовато: '+fmt(perHour)+' ₽/ч чистыми при норме 250–500. Берите ради отзыва или постоянного клиента, иначе поднимите цену.';}
 else {v.className='verdict v-bad';v.textContent='❌ Невыгодно: всего '+fmt(perHour)+' ₽ чистыми за час работы принтера. Поднимите цену, смените канал или откажитесь.';}
}

// ---------- КАЛЬКУЛЯТОР ОКУПАЕМОСТИ ----------
let PAYBACK={month:0,cum:0,inv:0};
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
 PAYBACK={month:month,cum:cum,inv:inv};
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

// ================================================================
// 1. РЕДАКТОР БАЗЫ ИЗДЕЛИЙ
// ================================================================
function buildCat(){
 const tb=document.getElementById('cat_body'); if(!tb) return;
 const st=settings();
 tb.innerHTML=CATALOG.map((r,i)=>{
  const fit=Math.max(1,Math.round(+r.f||1));
  const solo=costOf(r.w,r.h,r.a,st,1,1);
  const B=batchOf(r,fit,st);                 // полная плита
  const cost=B.unit;
  const ph=B.hours>0?((+r.p||0)*B.q-B.cost)/B.hours:0;
  const phSolo=(()=>{const S=batchOf(r,1,st);return S.hours>0?((+r.p||0)-S.cost)/S.hours:0;})();
  return `<tr>
 <td><input data-i="${i}" data-f="c" value="${esc(r.c)}" style="text-align:left;min-width:110px"></td>
 <td><input data-i="${i}" data-f="n" value="${esc(r.n)}" style="text-align:left;min-width:170px;font-weight:600"></td>
 <td><input type="number" step="any" data-i="${i}" data-f="w" value="${r.w}"></td>
 <td><input type="number" step="any" data-i="${i}" data-f="h" value="${r.h}"></td>
 <td><input type="number" step="any" data-i="${i}" data-f="a" value="${r.a}"></td>
 <td><input type="number" step="1" min="1" data-i="${i}" data-f="f" value="${fit}"></td>
 <td><input type="number" step="any" data-i="${i}" data-f="p" value="${r.p}"></td>
 <td class="out">${fmt(cost)}<div class="mini">по 1 шт ${fmt(solo)}</div></td>
 <td class="out" style="color:${ph>=NORM?'#14663a':ph>=100?'#8a5a06':'#96261a'}">${fmt(ph)}${fit>1?'<div class="mini">по 1 шт '+fmt(phSolo)+'</div>':''}</td>
 <td style="text-align:center"><button class="cat-del" data-i="${i}" title="Удалить позицию">✕</button></td></tr>`;
 }).join('');
 put('cat_count',CATALOG.length+' позиций');
}
function catEvents(){
 const tb=document.getElementById('cat_body'); if(!tb) return;
 tb.addEventListener('input',e=>{
  const i=e.target.dataset.i,f=e.target.dataset.f;
  if(i===undefined||!f) return;
  const old=CATALOG[+i].n;
  CATALOG[+i][f]=(f==='c'||f==='n')?e.target.value:e.target.value;
  if(f==='n'&&SALES[old]!==undefined){SALES[e.target.value]=SALES[old];delete SALES[old];salesSave();}
  if(f==='n'){PLAN.forEach(p=>{if(p.n===old)p.n=e.target.value;});planSave();}
  catSave(); refreshAll();
 });
 tb.addEventListener('click',e=>{
  if(!e.target.classList.contains('cat-del')) return;
  const r=CATALOG[+e.target.dataset.i];
  if(!confirm('Удалить «'+r.n+'» из базы? Позиция исчезнет из калькулятора, полки и планировщика.')) return;
  delete SALES[r.n]; salesSave();
  PLAN=PLAN.filter(p=>p.n!==r.n); planSave();
  CATALOG.splice(+e.target.dataset.i,1); catSave(); refreshAll();
 });
 const add=document.getElementById('cat_add');
 if(add) add.onclick=()=>{
  CATALOG.push({c:'📦 Своё',n:'Новая позиция '+(CATALOG.length+1),w:30,h:1.5,p:400,a:0,f:4});
  catSave(); refreshAll();
  const tbl=document.getElementById('cat_body');
  if(tbl&&tbl.lastElementChild) tbl.lastElementChild.querySelector('input[data-f="n"]').focus();
 };
 const srt=document.getElementById('cat_sort');
 if(srt) srt.onclick=()=>{
  const st=settings();
  const phOf=r=>{const B=batchOf(r,Math.max(1,Math.round(+r.f||1)),st);
   return B.hours>0?((+r.p||0)*B.q-B.cost)/B.hours:-1e9;};
  CATALOG.sort((a,b)=>phOf(b)-phOf(a));
  catSave(); refreshAll();
 };
 const grp=document.getElementById('cat_group');
 if(grp) grp.onclick=()=>{
  const order=[];CATALOG.forEach(r=>{if(!order.includes(r.c))order.push(r.c);});
  CATALOG.sort((a,b)=>order.indexOf(a.c)-order.indexOf(b.c));
  catSave(); refreshAll();
 };
 const rst=document.getElementById('cat_reset');
 if(rst) rst.onclick=()=>{
  if(!confirm('Вернуть стандартную базу из '+PRESETS.length+' позиций? Ваши добавленные изделия сотрутся.')) return;
  CATALOG=catDefaults(); catSave(); refreshAll();
 };
}

// ================================================================
// 2. ТЕСТ ПОЛКИ + УМНЫЕ ВЕРДИКТЫ
// ================================================================
function buildShelf(){
 const tb=document.getElementById('sh_body'); if(!tb) return;
 tb.innerHTML=CATALOG.map((r,i)=>`<tr>
 <td class="lbl">${esc(r.n)}<div class="mini">${esc(r.c)}</div></td>
 <td class="out">${r.w}</td><td class="out">${r.h}</td>
 <td class="out" id="sh_c${i}">—</td>
 <td class="out">${fmt(r.p)}</td>
 <td class="out" id="sh_m${i}">—</td><td class="out" id="sh_h${i}">—</td>
 <td><input type="number" step="any" min="0" data-sn="${esc(r.n)}" value="${SALES[r.n]===undefined?'':SALES[r.n]}" placeholder="0"></td>
 <td class="out" id="sh_t${i}" style="font-size:11.5px">—</td>
 <td id="sh_v${i}" style="font-size:11.5px;font-weight:700">—</td></tr>`).join('');
}
function calcShelf(){
 if(!document.getElementById('sh_body')) return;
 const st=settings();
 const prev=HIST.length?HIST[HIST.length-1].s||{}:null;
 let totRev=0,totProf=0,totHrs=0,rows=[];
 CATALOG.forEach((r,i)=>{
  const w=+r.w||0,h=+r.h||0,price=+r.p||0;
  const raw=SALES[r.n];
  const sold=(raw===undefined||raw==='')?null:(+raw||0);
  // считаем так, как реально печатали: партией на столько штук, сколько продали
  const B=batchOf(r,Math.max(1,sold||1),st);
  const cost=B.unit;
  const marg=price-cost;
  const hEff=B.unitH;                       // часы на штуку с учётом разогрева плиты
  const ph=hEff>0?marg/hEff:0;
  if(sold!==null){totRev+=price*sold; totProf+=marg*sold; totHrs+=(sold>0?B.hours:0);}
  put('sh_c'+i,fmt(cost)); put('sh_m'+i,fmt(marg)); put('sh_h'+i,fmt(ph));
  rows.push({i:i,n:r.n,sold:sold,ph:ph,h:hEff,marg:marg,plates:B.plates});
  // тренд к прошлой неделе
  const tEl=document.getElementById('sh_t'+i);
  if(tEl){
   if(!prev||sold===null||prev[r.n]===undefined||prev[r.n]===''){tEl.textContent='—';tEl.style.color='#6b7891';}
   else{
    const p0=+prev[r.n]||0, d=sold-p0;
    if(d>0){tEl.textContent='▲ '+p0+'→'+sold;tEl.style.color='#14663a';}
    else if(d<0){tEl.textContent='▼ '+p0+'→'+sold;tEl.style.color='#96261a';}
    else{tEl.textContent='= '+sold;tEl.style.color='#6b7891';}
   }
  }
 });
 // перегрузка принтера: кого резать первым — самые низкие ₽/час из продающихся
 const over=totHrs-CAP;
 const cut=new Set();
 if(over>0){
  const sellers=rows.filter(r=>r.sold>0).sort((a,b)=>a.ph-b.ph);
  let freed=0;
  for(const r of sellers){ if(freed>=over) break; freed+=r.h*r.sold; cut.add(r.i); }
 }
 // умные вердикты: продажи × выгодность
 rows.forEach(r=>{
  const v=document.getElementById('sh_v'+r.i); if(!v) return;
  if(r.sold===null){v.textContent='—';v.style.color='#6b7891';v.title='';return;}
  const good=r.ph>=NORM, ok=r.ph>=100;
  let t,c,tip;
  if(r.sold>=3&&good){t='🚀 МАСШТАБИРОВАТЬ';c='#14663a';tip='Продаётся и выгодно: печатайте партией, выложите на Авито и предложите в B2B.';}
  else if(r.sold>=3&&!good){t='💰 ПОДНЯТЬ ЦЕНУ';c='#8a5a06';tip='Спрос есть, но '+fmt(r.ph)+' ₽/ч ниже нормы 250. Поднимите цену на 15–20% — на ходовом товаре это почти не бьёт по спросу.';}
  else if(r.sold>=1&&good){t='✅ держать';c='#14663a';tip='Выгодно, но продаётся вяло. Держите на полке, попробуйте лучше выложить и подсветить ценником.';}
  else if(r.sold>=1&&ok){t='⏳ наблюдать';c='#8a5a06';tip='И спрос средний, и маржа средняя. Ещё неделя наблюдения — потом решайте.';}
  else if(r.sold>=1){t='✂️ невыгодно';c='#96261a';tip='Продаётся, но приносит всего '+fmt(r.ph)+' ₽/ч. Поднимите цену или замените позицию.';}
  else if(good){t='📣 в другой канал';c='#8a5a06';tip='Выгодная позиция, но на полке не берут. Не убирайте — выложите на Авито/Telegram или предложите в B2B.';}
  else{t='🗑 убрать с полки';c='#96261a';tip='Не продаётся и невыгодно. Освободите место под то, что продаётся.';}
  if(cut.has(r.i)){t='⛔ резать первым';c='#96261a';tip='Принтер перегружен, а эта позиция даёт меньше всех — '+fmt(r.ph)+' ₽/ч. Сокращайте тираж именно здесь.';}
  v.textContent=t;v.style.color=c;v.title=tip;
 });
 put('sh_rev',fmt(totRev)+' ₽'); put('sh_prof',fmt(totProf)+' ₽');
 put('sh_hrs',fmt(totHrs)+' ч');
 const avg=totHrs>0?totProf/totHrs:0;
 put('sh_avg',fmt(avg)+' ₽/ч');
 const cap=document.getElementById('sh_cap');
 if(cap){
  const pct=Math.round(totHrs/CAP*100);
  if(totHrs===0){cap.textContent='—';cap.style.color='';}
  else if(totHrs<=CAP){cap.textContent=pct+'% — влезает (потолок P1S ~110–130 ч/нед)';cap.style.color='#14663a';}
  else{cap.textContent=pct+'% — НЕ УСПЕТЬ, перебор '+fmt(over)+' ч';cap.style.color='#96261a';}
 }
 const bar=document.getElementById('sh_bar');
 if(bar){const pct=Math.min(100,totHrs/CAP*100);bar.style.width=pct+'%';
  bar.style.background=totHrs>CAP?'#c0392b':totHrs>CAP*0.85?'#b7791f':'linear-gradient(90deg,#2b5bb5,#4d84e0)';}
 const sv=document.getElementById('sh_verdict');
 if(sv){
  const sold=rows.filter(r=>r.sold>0).length, zero=rows.filter(r=>r.sold===0).length;
  if(!rows.some(r=>r.sold!==null)){sv.className='verdict';sv.style.cssText='';sv.textContent='Впишите продажи за неделю в колонку «Продано» — появятся вердикты по каждой позиции.';}
  else if(totHrs>CAP){sv.className='verdict v-bad';sv.innerHTML='⛔ <b>Принтер физически не тянет:</b> нужно '+fmt(totHrs)+' ч, реальный потолок ~'+CAP+' ч/нед. Уберите позиции с пометкой «резать первым» — они дают меньше всего ₽ за час — или считайте второй принтер.';}
  else if(avg>=NORM){sv.className='verdict v-ok';sv.innerHTML='✅ Неделя здоровая: <b>'+fmt(totProf)+' ₽</b> прибыли, средний заработок <b>'+fmt(avg)+' ₽/ч</b> при норме 250–500. Продавалось позиций: '+sold+'. Загрузка принтера '+Math.round(totHrs/CAP*100)+'%.';}
  else{sv.className='verdict v-warn';sv.innerHTML='⚠️ Средний заработок <b>'+fmt(avg)+' ₽/ч</b> ниже нормы 250. Поднимите цены на позиции с пометкой «ПОДНЯТЬ ЦЕНУ»'+(zero?' и уберите '+zero+' непродающихся':'')+'.';}
 }
 return {rev:totRev,prof:totProf,hrs:totHrs,avg:avg};
}
function shelfEvents(){
 const tb=document.getElementById('sh_body'); if(!tb) return;
 tb.addEventListener('input',e=>{
  const n=e.target.dataset.sn; if(!n) return;
  if(e.target.value==='') delete SALES[n]; else SALES[n]=e.target.value;
  salesSave(); calcShelf(); dash();
 });
 const cl=document.getElementById('sh_clear');
 if(cl) cl.onclick=()=>{
  if(!confirm('Очистить продажи текущей недели? Сохранённые недели в истории останутся.')) return;
  SALES={}; salesSave(); buildShelf(); calcShelf(); dash();
 };
}

// ================================================================
// 3. ИСТОРИЯ НЕДЕЛЬ
// ================================================================
function saveWeek(){
 const t=calcShelf();
 if(!t||(t.rev===0&&t.hrs===0)){alert('Сначала впишите продажи за неделю в «Тесте полки».');return;}
 const d=document.getElementById('h_date');
 const date=(d&&d.value)?d.value:today();
 HIST.push({d:date,rev:t.rev,prof:t.prof,hrs:t.hrs,s:JSON.parse(JSON.stringify(SALES))});
 HIST.sort((a,b)=>a.d<b.d?-1:1);
 histSave();
 if(confirm('Неделя от '+ruDate(date)+' сохранена: '+fmt(t.rev)+' ₽ выручки.\n\nОчистить колонку «Продано», чтобы начать новую неделю?')){
  SALES={}; salesSave(); buildShelf();
 }
 refreshAll();
}
function buildHist(){
 const tb=document.getElementById('h_body'); if(!tb) return;
 if(!HIST.length){
  tb.innerHTML='<tr><td colspan="7" style="text-align:center;color:#6b7891;padding:18px">Пока пусто. Впишите продажи в «Тесте полки» и нажмите «Сохранить неделю» — здесь появится динамика.</td></tr>';
  put('h_best','—'); put('h_total','—'); put('h_avg','—');
  const v=document.getElementById('h_verdict'); if(v){v.className='verdict';v.style.cssText='';v.textContent='Сохраните хотя бы две недели — покажу тренд: растёте или падаете.';}
  drawSpark([]);
  return;
 }
 tb.innerHTML=HIST.map((w,i)=>{
  const prev=i>0?HIST[i-1]:null;
  const ph=w.hrs>0?w.prof/w.hrs:0;
  let d='—',col='#6b7891';
  if(prev&&prev.rev>0){const p=(w.rev-prev.rev)/prev.rev*100;
   d=(p>=0?'+':'')+Math.round(p)+'%'; col=p>=5?'#14663a':p<=-5?'#96261a':'#6b7891';}
  return `<tr><td class="lbl">${ruDate(w.d)}</td><td class="out">${fmt(w.rev)}</td><td class="out">${fmt(w.prof)}</td>
  <td class="out">${fmt2(w.hrs)}</td><td class="out" style="color:${ph>=NORM?'#14663a':'#96261a'}">${fmt(ph)}</td>
  <td class="out" style="color:${col}">${d}</td>
  <td style="text-align:center"><button class="cat-del" data-h="${i}" title="Удалить неделю">✕</button></td></tr>`;
 }).join('');
 const best=HIST.reduce((a,b)=>b.rev>a.rev?b:a);
 const total=HIST.reduce((s,w)=>s+w.prof,0);
 const avgRev=HIST.reduce((s,w)=>s+w.rev,0)/HIST.length;
 put('h_best',fmt(best.rev)+' ₽ ('+ruDate(best.d)+')');
 put('h_total',fmt(total)+' ₽');
 put('h_avg',fmt(avgRev)+' ₽');
 drawSpark(HIST);
 const v=document.getElementById('h_verdict');
 if(v){
  if(HIST.length<2){v.className='verdict';v.style.cssText='';v.innerHTML='Сохранена 1 неделя. Со второй появится тренд и сравнение по позициям.';}
  else{
   const n=HIST.length, last=HIST[n-1], prev=HIST[n-2];
   const gr=prev.rev>0?(last.rev-prev.rev)/prev.rev*100:0;
   const need=PAYBACK.inv>0?PAYBACK.inv/6/4.3:0; // сколько прибыли в неделю нужно для окупаемости за 6 мес
   let s='';
   if(need>0) s=' Для окупаемости за 6 месяцев нужно ~<b>'+fmt(need)+' ₽</b> прибыли в неделю — сейчас '+fmt(last.prof)+' ₽.';
   if(gr>=10){v.className='verdict v-ok';v.innerHTML='📈 Рост: выручка +'+Math.round(gr)+'% к прошлой неделе ('+fmt(prev.rev)+' → '+fmt(last.rev)+' ₽). Так держать — масштабируйте то, что помечено «МАСШТАБИРОВАТЬ».'+s;}
   else if(gr<=-10){v.className='verdict v-bad';v.innerHTML='📉 Падение: выручка '+Math.round(gr)+'% ('+fmt(prev.rev)+' → '+fmt(last.rev)+' ₽). Проверьте: не кончился ли ходовой цвет, не пустеет ли полка, давно ли обновляли Авито.'+s;}
   else{v.className='verdict v-warn';v.innerHTML='➡️ Стабильно: '+(gr>=0?'+':'')+Math.round(gr)+'% к прошлой неделе. Чтобы сдвинуться — новый канал или новая позиция в базе.'+s;}
  }
 }
}
function drawSpark(h){
 const el=document.getElementById('h_spark'); if(!el) return;
 if(!h.length){el.innerHTML='';return;}
 const max=Math.max(...h.map(w=>w.rev),1);
 const bars=h.slice(-16);
 el.innerHTML=bars.map(w=>{
  const pct=Math.max(3,w.rev/max*100);
  return `<div class="spk" title="${ruDate(w.d)}: ${fmt(w.rev)} ₽"><i style="height:${pct}%"></i><span>${ruDate(w.d)}</span></div>`;
 }).join('');
}
function histEvents(){
 const b=document.getElementById('h_save'); if(b) b.onclick=saveWeek;
 const tb=document.getElementById('h_body');
 if(tb) tb.addEventListener('click',e=>{
  const i=e.target.dataset.h; if(i===undefined) return;
  if(!confirm('Удалить неделю от '+ruDate(HIST[+i].d)+'?')) return;
  HIST.splice(+i,1); histSave(); refreshAll();
 });
 const d=document.getElementById('h_date'); if(d&&!d.value) d.value=today();
}

// ================================================================
// 4. ОПТ B2B — сетка скидок по тиражу
// ================================================================
function buildB2B(){
 const sel=document.getElementById('b_item'); if(!sel) return;
 const keep=sel.value;
 sel.innerHTML=CATALOG.map((r,i)=>'<option value="'+i+'">'+esc(r.n)+'</option>').join('');
 if(keep&&sel.querySelector('option[value="'+keep+'"]')) sel.value=keep;
 else{const saved=localStorage.getItem('f_bitem');if(saved&&sel.querySelector('option[value="'+saved+'"]'))sel.value=saved;}
}
function calcB2B(){
 const sel=document.getElementById('b_item'); if(!sel) return;
 const r=CATALOG[+sel.value]; if(!r) return;
 const st=settings();
 const retail=num('b_retail')||+r.p||0;
 const pack=num('b_pack');
 const fit=Math.max(1,Math.round(+r.f||1));
 // себестоимость опта считаем по полной плите — так тираж и печатается
 const unit=batchOf(r,fit,st).unit+pack;
 const unitSolo=costOf(r.w,r.h,r.a,st,1,1)+pack;
 const tiers=[
  {q:num('b_q1')||10,d:num('b_d1')},
  {q:num('b_q2')||25,d:num('b_d2')},
  {q:num('b_q3')||50,d:num('b_d3')},
  {q:num('b_q4')||100,d:num('b_d4')}
 ];
 const floor=unit*1.4; // ниже этого опускаться нельзя
 let rows='',bestQ=0,warn=false;
 const S1=batchOf(r,1,st);
 rows+=`<tr><td class="lbl">Розница, 1 шт</td><td class="out">1</td><td class="out">0%</td><td class="out">${fmt(retail)}</td><td class="out">${fmt(retail-unitSolo)}</td><td class="out">${fmt(S1.hours>0?(retail-unitSolo)/S1.hours:0)}</td><td class="out">${fmt(retail)}</td><td style="font-size:11.5px;font-weight:700;color:#14663a">базовая</td></tr>`;
 tiers.forEach(t=>{
  const price=Math.round(retail*(1-t.d/100));
  const B=batchOf(r,t.q,st);
  const marg=price-(B.unit+pack);
  const ph=B.hours>0?(price*t.q-(B.cost+pack*t.q))/B.hours:0;
  const total=price*t.q;
  const hrs=B.hours;
  let txt,col;
  if(price<unit){txt='❌ В УБЫТОК';col='#96261a';warn=true;}
  else if(price<floor){txt='❌ ниже минимума';col='#96261a';warn=true;}
  else if(ph>=NORM){txt='✅ выгодно';col='#14663a';bestQ=t.q;}
  else if(ph>=100){txt='⚠️ на грани';col='#8a5a06';}
  else{txt='❌ мало ₽/час';col='#96261a';warn=true;}
  rows+=`<tr><td class="lbl">от ${t.q} шт</td><td class="out">${t.q}</td><td class="out">${t.d}%</td><td class="out">${fmt(price)}</td>
  <td class="out">${fmt(marg)}</td><td class="out" style="color:${ph>=NORM?'#14663a':ph>=100?'#8a5a06':'#96261a'}">${fmt(ph)}</td>
  <td class="out">${fmt(total)}</td><td style="font-size:11.5px;font-weight:700;color:${col}">${txt}</td></tr>`;
  rows+=`<tr class="sub"><td colspan="8" class="mini" style="padding:3px 10px 7px">${t.q} шт = <b>${B.plates} плит${B.plates===1?'а':''}</b> по ${B.perPlate} шт · ${fmt2(hrs)} ч печати ≈ ${fmt2(hrs/12)} дн. по 12 ч · пластика ${fmt(B.grams)} г · ваша прибыль ${fmt(marg*t.q)} ₽</td></tr>`;
 });
 const tb=document.getElementById('b_rows'); if(tb) tb.innerHTML=rows;
 put('b_cost',fmt2(unit)+' ₽');
 put('b_fit',fit+' шт на стол');
 const bs=document.getElementById('b_solo');
 if(bs){if(fit>1){bs.textContent='по 1 шт вышло бы '+fmt2(unitSolo)+' ₽ — партия дешевле на '+fmt(unitSolo-unit)+' ₽/шт';bs.style.color='#14663a';}
  else{bs.textContent='влезает по 1 шт — экономии от партии нет';bs.style.color='#6b7891';}}
 put('b_floor',fmt(floor)+' ₽');
 const maxD=retail>0?Math.max(0,(1-floor/retail)*100):0;
 put('b_maxd',Math.floor(maxD)+'%');
 const v=document.getElementById('b_verdict');
 if(v){
  if(warn){v.className='verdict v-bad';v.innerHTML='⚠️ Часть тиражей уходит в минус. Ваш пол по цене — <b>'+fmt(floor)+' ₽</b> за штуку, это максимум <b>'+Math.floor(maxD)+'%</b> скидки от розницы '+fmt(retail)+' ₽. Больше не давайте: опт должен грузить принтер, а не съедать прибыль.';}
  else{v.className='verdict v-ok';v.innerHTML='✅ Сетка рабочая. Максимальная скидка, которую можно дать — <b>'+Math.floor(maxD)+'%</b> (цена не ниже '+fmt(floor)+' ₽). '+(bestQ?'Лучше всего заходит тираж от <b>'+bestQ+' шт</b>. ':'')+'Скидку давайте только за <b>предоплату и весь тираж сразу</b>, а не «по 5 штук со скидкой как за 50».';}
 }
}

// ================================================================
// 5. ПЛАНИРОВЩИК ПЕЧАТИ
// ================================================================
let PLAN_NEED={g:0,h:0};
function buildPlan(){
 const sel=document.getElementById('pl_item');
 if(sel) sel.innerHTML=CATALOG.map((r,i)=>'<option value="'+i+'">'+esc(r.n)+' · '+r.h+' ч</option>').join('');
 const tb=document.getElementById('pl_body'); if(!tb) return;
 if(!PLAN.length){
  tb.innerHTML='<tr><td colspan="7" style="text-align:center;color:#6b7891;padding:16px">Очередь пуста. Выберите изделие сверху и нажмите «В очередь».</td></tr>';
  return;
 }
 const st=settings();
 tb.innerHTML=PLAN.map((p,i)=>{
  const r=CATALOG.find(c=>c.n===p.n);
  if(!r) return `<tr><td class="lbl">${esc(p.n)}</td><td colspan="5" class="mini">нет в базе — удалите строку</td><td style="text-align:center"><button class="cat-del" data-p="${i}">✕</button></td></tr>`;
  const q=+p.q||0;
  const B=batchOf(r,q,st);
  const marg=(+r.p||0)*q-B.cost;
  return `<tr><td class="lbl">${esc(r.n)}</td>
  <td><input type="number" step="1" min="0" data-pq="${i}" value="${q}"></td>
  <td class="out">${B.plates}<div class="mini">по ${B.perPlate} шт</div></td>
  <td class="out">${fmt2(B.hours)}${B.ohH>0?'<div class="mini">+'+fmt2(B.ohH)+' ч разогрев</div>':''}</td>
  <td class="out">${fmt(B.grams)}</td>
  <td class="out">${fmt(+r.p*q)}</td><td class="out">${fmt(marg)}</td>
  <td style="text-align:center"><button class="cat-del" data-p="${i}" title="Убрать">✕</button></td></tr>`;
 }).join('');
}
function calcPlan(){
 if(!document.getElementById('pl_body')) return;
 const st=settings();
 let hrs=0,g=0,rev=0,prof=0,plates=0,ohh=0,soloH=0;
 PLAN.forEach(p=>{
  const r=CATALOG.find(c=>c.n===p.n); if(!r) return;
  const q=+p.q||0; if(q<=0) return;
  const B=batchOf(r,q,st);
  hrs+=B.hours; g+=B.grams; plates+=B.plates; ohh+=B.ohH;
  rev+=(+r.p||0)*q; prof+=(+r.p||0)*q-B.cost;
  soloH+=batchOf({w:r.w,h:r.h,a:r.a,f:1},q,st).hours;
 });
 put('pl_plates',plates+' шт');
 put('pl_saveh',soloH>hrs?'−'+fmt2(soloH-hrs)+' ч против печати по одной':'—');
 PLAN_NEED={g:g,h:hrs};
 const hpd=num('pl_hpd')||12, days=num('pl_days')||7;
 const need=hpd>0?hrs/hpd:0;
 const avail=hpd*days;
 put('pl_hrs',fmt2(hrs)+' ч'); put('pl_g',fmt(g)+' г ('+fmt2(g/1000)+' кг)');
 put('pl_rev',fmt(rev)+' ₽'); put('pl_prof',fmt(prof)+' ₽');
 put('pl_dur',fmt2(need)+' дн.');
 put('pl_ph',fmt(hrs>0?prof/hrs:0)+' ₽/ч');
 const stock=SPOOLS.reduce((s,x)=>s+(+x.g||0),0);
 put('pl_stock',fmt(stock)+' г');
 const bar=document.getElementById('pl_bar');
 if(bar){const pct=avail>0?Math.min(100,hrs/avail*100):0;bar.style.width=pct+'%';
  bar.style.background=hrs>avail?'#c0392b':hrs>avail*0.85?'#b7791f':'linear-gradient(90deg,#2b5bb5,#4d84e0)';}
 put('pl_load',avail>0?Math.round(hrs/avail*100)+'% срока':'—');
 const v=document.getElementById('pl_verdict');
 if(!v) return;
 if(hrs===0){v.className='verdict';v.style.cssText='';v.textContent='Добавьте изделия в очередь — посчитаю часы, пластик и успеете ли к сроку.';return;}
 const msgs=[];
 let bad=false,warn=false;
 if(hrs>avail){bad=true;msgs.push('<b>Не успеваете:</b> нужно '+fmt2(hrs)+' ч, а за '+days+' дн. по '+hpd+' ч выходит только '+fmt2(avail)+' ч. Не хватает '+fmt2(hrs-avail)+' ч — уберите из очереди самое долгое или сдвиньте срок.');}
 else if(hrs>avail*0.85){warn=true;msgs.push('Успеваете <b>впритык</b>: '+fmt2(hrs)+' из '+fmt2(avail)+' ч. Любой брак или сбой электричества — и срыв. Заложите запас.');}
 else msgs.push('<b>Успеваете:</b> '+fmt2(hrs)+' ч из доступных '+fmt2(avail)+' ч за '+days+' дн. Запас '+fmt2(avail-hrs)+' ч.');
 if(g>stock){bad=true;msgs.push('<b>Не хватит пластика:</b> нужно '+fmt(g)+' г, на складе '+fmt(stock)+' г. Докупите минимум '+fmt(Math.ceil((g-stock)/1000))+' катушк(и) — в Крыму доставка идёт долго, заказывайте сразу.');}
 else if(g>stock*0.8){warn=true;msgs.push('Пластика хватит впритык: '+fmt(g)+' г из '+fmt(stock)+' г.');}
 if(hrs>CAP&&days>=7) msgs.push('Учтите: '+fmt2(hrs)+' ч — это выше реального недельного потолка одного P1S (~'+CAP+' ч).');
 v.className='verdict '+(bad?'v-bad':warn?'v-warn':'v-ok');
 v.innerHTML=(bad?'⛔ ':warn?'⚠️ ':'✅ ')+msgs.join('<br>');
}
function planEvents(){
 const add=document.getElementById('pl_add');
 if(add) add.onclick=()=>{
  const sel=document.getElementById('pl_item'); const r=CATALOG[+sel.value]; if(!r) return;
  const q=Math.max(1,Math.round(num('pl_qty')||1));
  const ex=PLAN.find(p=>p.n===r.n);
  if(ex) ex.q=(+ex.q||0)+q; else PLAN.push({n:r.n,q:q});
  planSave(); buildPlan(); calcPlan(); calcPlastic(); dash();
 };
 const tb=document.getElementById('pl_body'); if(!tb) return;
 tb.addEventListener('input',e=>{
  const i=e.target.dataset.pq; if(i===undefined) return;
  PLAN[+i].q=e.target.value; planSave(); calcPlan(); calcPlastic(); dash();
 });
 tb.addEventListener('click',e=>{
  const i=e.target.dataset.p; if(i===undefined) return;
  PLAN.splice(+i,1); planSave(); buildPlan(); calcPlan(); calcPlastic(); dash();
 });
 const cl=document.getElementById('pl_clear');
 if(cl) cl.onclick=()=>{if(!PLAN.length||!confirm('Очистить всю очередь печати?'))return;PLAN=[];planSave();buildPlan();calcPlan();calcPlastic();dash();};
 const fill=document.getElementById('pl_fromshelf');
 if(fill) fill.onclick=()=>{
  const src=HIST.length?HIST[HIST.length-1].s:SALES;
  let n=0;
  Object.keys(src||{}).forEach(k=>{
   const q=+src[k]||0; if(q<=0) return;
   if(!CATALOG.some(c=>c.n===k)) return;
   const ex=PLAN.find(p=>p.n===k); if(ex) ex.q=q; else PLAN.push({n:k,q:q});
   n++;
  });
  if(!n){alert('Нет данных о продажах. Впишите продажи в «Тесте полки» или сохраните неделю в истории.');return;}
  planSave(); buildPlan(); calcPlan(); calcPlastic(); dash();
 };
}

// ================================================================
// 6. УЧЁТ ПЛАСТИКА
// ================================================================
function buildSpools(){
 const tb=document.getElementById('sp_body'); if(!tb) return;
 tb.innerHTML=SPOOLS.map((s,i)=>{
  const g=+s.g||0, pct=Math.max(0,Math.min(100,g/1000*100));
  const col=g<200?'#c0392b':g<400?'#b7791f':'#1e8449';
  return `<tr>
  <td><input data-si="${i}" data-sf="c" value="${esc(s.c)}" style="text-align:left;min-width:110px;font-weight:600"></td>
  <td><input data-si="${i}" data-sf="t" value="${esc(s.t)}" style="text-align:left;min-width:80px"></td>
  <td><input type="number" step="any" min="0" data-si="${i}" data-sf="g" value="${g}"></td>
  <td style="min-width:110px"><div class="progwrap" style="margin:0"><div class="progbar" style="width:${pct}%;background:${col}"></div></div><div class="mini" style="color:${col}">${g<200?'почти пусто':g<400?'на исходе':'норма'}</div></td>
  <td><input type="number" step="any" min="0" data-si="${i}" data-sf="pr" value="${+s.pr||0}"></td>
  <td class="out">${fmt((+s.pr||0)/1000*g)}</td>
  <td style="text-align:center"><button class="cat-del" data-sd="${i}" title="Удалить катушку">✕</button></td></tr>`;
 }).join('');
}
function calcPlastic(){
 if(!document.getElementById('sp_body')) return;
 const stock=SPOOLS.reduce((s,x)=>s+(+x.g||0),0);
 const value=SPOOLS.reduce((s,x)=>s+(+x.pr||0)/1000*(+x.g||0),0);
 const auto=document.getElementById('sp_auto');
 let week=num('sp_week');
 if(auto&&auto.checked){
  // расход из планировщика, иначе из продаж последней недели
  let g=PLAN_NEED.g;
  if(!g){
   const st=settings();
   const src=HIST.length?HIST[HIST.length-1].s:SALES;
   Object.keys(src||{}).forEach(k=>{
    const r=CATALOG.find(c=>c.n===k); if(!r) return;
    g+=batchOf(r,+src[k]||0,st).grams;
   });
  }
  week=g;
  const wi=document.getElementById('sp_week');
  if(wi){wi.value=Math.round(g);wi.disabled=true;}
 }else{const wi=document.getElementById('sp_week');if(wi)wi.disabled=false;}
 const lead=num('sp_lead')||14;
 put('sp_stock',fmt(stock)+' г ('+fmt2(stock/1000)+' кг)');
 put('sp_value',fmt(value)+' ₽');
 put('sp_weekout',fmt(week)+' г/нед');
 const days=week>0?stock/(week/7):0;
 put('sp_days',week>0?fmt(days)+' дн.':'—');
 const need=week/7*lead; // сколько нужно на срок доставки
 put('sp_need',fmt(need)+' г');
 const v=document.getElementById('sp_verdict');
 if(!v) return;
 const low=SPOOLS.filter(s=>(+s.g||0)<200).map(s=>s.c);
 if(week<=0){v.className='verdict';v.style.cssText='';v.innerHTML='Укажите расход в неделю (или включите «брать из планировщика») — посчитаю, на сколько дней хватит и когда заказывать.'+(low.length?'<br>Уже почти пусто: <b>'+low.map(esc).join(', ')+'</b>.':'');return;}
 const buy=Math.max(0,Math.ceil((need*1.5-stock)/1000));
 if(days<lead){v.className='verdict v-bad';v.innerHTML='🚨 <b>Заказывайте пластик сегодня.</b> Остатка на '+fmt(days)+' дн., а доставка идёт '+lead+' дн. — встанете без пластика. Нужно минимум <b>'+Math.max(1,buy)+' катушк(и)</b>.'+(low.length?'<br>Заканчивается: <b>'+low.map(esc).join(', ')+'</b>.':'');}
 else if(days<lead*1.6){v.className='verdict v-warn';v.innerHTML='⚠️ <b>Пора заказывать.</b> Хватит на '+fmt(days)+' дн., доставка '+lead+' дн. Правило для Крыма: держите запас на 2 недели вперёд и заказывайте, когда осталась одна полная катушка. Возьмите '+Math.max(1,buy)+' шт.'+(low.length?'<br>Заканчивается: <b>'+low.map(esc).join(', ')+'</b>.':'');}
 else{v.className='verdict v-ok';v.innerHTML='✅ Запаса на <b>'+fmt(days)+' дн.</b> при расходе '+fmt(week)+' г/нед — с доставкой '+lead+' дн. успеваете спокойно.'+(low.length?'<br>Но заканчивается цвет: <b>'+low.map(esc).join(', ')+'</b> — добавьте в следующий заказ.':'');}
}
function spoolEvents(){
 const tb=document.getElementById('sp_body'); if(!tb) return;
 tb.addEventListener('input',e=>{
  const i=e.target.dataset.si,f=e.target.dataset.sf;
  if(i===undefined||!f) return;
  SPOOLS[+i][f]=e.target.value; spoolSave();
  if(f==='g'||f==='pr'){buildSpools();}
  calcPlastic(); calcPlan(); dash();
 });
 tb.addEventListener('click',e=>{
  const i=e.target.dataset.sd; if(i===undefined) return;
  SPOOLS.splice(+i,1); spoolSave(); buildSpools(); calcPlastic(); calcPlan(); dash();
 });
 const add=document.getElementById('sp_add');
 if(add) add.onclick=()=>{SPOOLS.push({c:'Новый цвет',t:'PLA',g:1000,pr:1600});spoolSave();buildSpools();calcPlastic();dash();};
 const wo=document.getElementById('sp_writeoff');
 if(wo) wo.onclick=()=>{
  if(!PLAN_NEED.g){alert('Сначала соберите очередь в «Планировщике печати».');return;}
  if(!confirm('Списать '+fmt(PLAN_NEED.g)+' г со склада (пропорционально остаткам)? Так делают после того, как очередь напечатана.')) return;
  const total=SPOOLS.reduce((s,x)=>s+(+x.g||0),0);
  if(total<=0){alert('На складе нечего списывать.');return;}
  SPOOLS.forEach(s=>{s.g=Math.max(0,Math.round((+s.g||0)-PLAN_NEED.g*((+s.g||0)/total)));});
  spoolSave(); buildSpools(); calcPlastic(); calcPlan(); dash();
 };
 const au=document.getElementById('sp_auto');
 if(au){
  au.checked=localStorage.getItem('sp_autochk')==='1';
  au.addEventListener('change',()=>{localStorage.setItem('sp_autochk',au.checked?'1':'0');calcPlastic();dash();});
 }
}

// ================================================================
// 7. РЕЗЕРВНАЯ КОПИЯ
// ================================================================
const BK_FIELDS=()=>{const o={};Object.keys(localStorage).forEach(k=>{if(/^(f_|chk_)/.test(k))o[k]=localStorage.getItem(k);});return o;};
function backupData(){
 return {app:'3dprint-guide',v:2,date:new Date().toISOString(),
  catalog:CATALOG,sales:SALES,history:HIST,plan:PLAN,spools:SPOOLS,
  fields:BK_FIELDS(),autochk:localStorage.getItem('sp_autochk')||'0'};
}
function dl(name,text,type){
 const b=new Blob([text],{type:(type||'application/json')+';charset=utf-8'});
 const a=document.createElement('a');
 a.href=URL.createObjectURL(b); a.download=name;
 document.body.appendChild(a); a.click();
 setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},400);
}
function doBackup(){
 const d=today();
 dl('3d-печать-копия-'+d+'.json',JSON.stringify(backupData(),null,1));
 localStorage.setItem(BK_KEY,d); bkStatus();
}
function csv(rows){
 return '\ufeff'+rows.map(r=>r.map(c=>{
  const s=String(c==null?'':c);
  return /[";\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;
 }).join(';')).join('\r\n');
}
function exportCSV(){
 const st=settings();
 const rows=[['РАЗДЕЛ','Название','Категория','Вес г','Часы','Отход AMS г','Шт на стол','Цена ₽','Себест. партией ₽','Себест. по 1 шт ₽','Маржа ₽','₽/час','Продано за неделю']];
 CATALOG.forEach(r=>{
  const fit=Math.max(1,Math.round(+r.f||1));
  const B=batchOf(r,fit,st);
  const c=B.unit, solo=costOf(r.w,r.h,r.a,st,1,1), m=(+r.p||0)-c;
  rows.push(['База изделий',r.n,r.c,r.w,r.h,r.a,fit,r.p,Math.round(c),Math.round(solo),Math.round(m),
   Math.round(B.hours>0?((+r.p||0)*B.q-B.cost)/B.hours:0),SALES[r.n]===undefined?'':SALES[r.n]]);
 });
 rows.push([]);
 rows.push(['РАЗДЕЛ','Неделя','Выручка ₽','Прибыль ₽','Часы','₽/час']);
 HIST.forEach(w=>rows.push(['История недель',w.d,Math.round(w.rev),Math.round(w.prof),w.hrs,Math.round(w.hrs>0?w.prof/w.hrs:0)]));
 rows.push([]);
 rows.push(['РАЗДЕЛ','Цвет','Тип','Остаток г','Цена катушки ₽','Стоимость остатка ₽']);
 SPOOLS.forEach(s=>rows.push(['Склад пластика',s.c,s.t,s.g,s.pr,Math.round((+s.pr||0)/1000*(+s.g||0))]));
 rows.push([]);
 rows.push(['РАЗДЕЛ','Позиция','Штук','Плит','Часы','Пластик г']);
 PLAN.forEach(p=>{const r=CATALOG.find(c=>c.n===p.n);if(!r)return;
  const B=batchOf(r,+p.q||0,st);
  rows.push(['Очередь печати',p.n,p.q,B.plates,Math.round(B.hours*100)/100,Math.round(B.grams)]);});
 dl('3d-печать-таблицы-'+today()+'.csv',csv(rows),'text/csv');
}
function doRestore(file){
 const fr=new FileReader();
 fr.onload=()=>{
  let d;
  try{d=JSON.parse(fr.result);}catch(e){alert('Не похоже на файл копии: не удалось прочитать JSON.');return;}
  if(!d||d.app!=='3dprint-guide'){alert('Это не файл копии этого сайта.');return;}
  if(!confirm('Восстановить данные из копии от '+(d.date||'').slice(0,10)+'?\n\nТекущие цифры, база изделий, история и галочки будут заменены.')) return;
  if(Array.isArray(d.catalog)&&d.catalog.length){CATALOG=d.catalog;catSave();}
  if(d.sales&&typeof d.sales==='object'){SALES=d.sales;salesSave();}
  if(Array.isArray(d.history)){HIST=d.history;histSave();}
  if(Array.isArray(d.plan)){PLAN=d.plan;planSave();}
  if(Array.isArray(d.spools)&&d.spools.length){SPOOLS=d.spools;spoolSave();}
  if(d.fields&&typeof d.fields==='object'){
   Object.keys(localStorage).forEach(k=>{if(/^(f_|chk_)/.test(k))localStorage.removeItem(k);});
   Object.keys(d.fields).forEach(k=>localStorage.setItem(k,d.fields[k]));
  }
  if(d.autochk!==undefined) localStorage.setItem('sp_autochk',d.autochk);
  alert('Готово. Страница перезагрузится.');
  location.reload();
 };
 fr.readAsText(file);
}
function bkStatus(){
 const last=localStorage.getItem(BK_KEY);
 const el=document.getElementById('b_last');
 const rem=document.getElementById('bk_remind');
 if(el) el.textContent=last?ruDate(last)+' ('+last+')':'никогда';
 const days=last?Math.floor((Date.now()-new Date(last).getTime())/864e5):999;
 const has=CATALOG.length!==PRESETS.length||HIST.length>0||Object.keys(SALES).length>0;
 if(rem){
  if(!has&&!last){rem.style.display='none';}
  else if(days>=14){rem.style.display='';rem.className='verdict v-bad';
   rem.innerHTML='🚨 Копии нет уже '+(last?days+' дн.':'ни разу')+'. Все ваши цифры лежат только в этом браузере — очистка кэша, переустановка или «почистил телефон» сотрут историю недель, базу изделий и остатки пластика <b>без возможности восстановить</b>. Нажмите «Скачать копию» — это 2 секунды.';}
  else if(days>=7){rem.style.display='';rem.className='verdict v-warn';
   rem.innerHTML='⚠️ Последняя копия '+days+' дн. назад. Раз в неделю скачивайте файл — привяжите к тому же дню, когда сохраняете неделю в истории.';}
  else{rem.style.display='';rem.className='verdict v-ok';
   rem.innerHTML='✅ Копия свежая — '+days+' дн. назад. Храните файл не в браузере: облако, почта самому себе или флешка.';}
 }
}
function backupEvents(){
 const b=document.getElementById('b_save'); if(b) b.onclick=doBackup;
 const c=document.getElementById('b_csv'); if(c) c.onclick=exportCSV;
 const f=document.getElementById('b_file');
 const l=document.getElementById('b_load');
 if(l&&f){l.onclick=()=>f.click(); f.onchange=()=>{if(f.files&&f.files[0])doRestore(f.files[0]);f.value='';};}
 const w=document.getElementById('b_wipe');
 if(w) w.onclick=()=>{
  if(!confirm('Стереть ВСЁ: базу изделий, продажи, историю недель, очередь, склад пластика и галочки?')) return;
  if(!confirm('Точно? Восстановить можно будет только из скачанного файла копии.')) return;
  localStorage.clear(); location.reload();
 };
}

// ================================================================
// СВОДКА-ДАШБОРД
// ================================================================
function dash(){
 const st=settings();
 // неделя: текущие продажи, если пусто — последняя сохранённая
 let rev=0,prof=0,hrs=0,src=null;
 const hasNow=Object.keys(SALES).some(k=>SALES[k]!==''&&SALES[k]!==undefined);
 if(hasNow){src=SALES;}
 else if(HIST.length){const w=HIST[HIST.length-1];rev=w.rev;prof=w.prof;hrs=w.hrs;}
 if(src){
  Object.keys(src).forEach(k=>{
   const r=CATALOG.find(c=>c.n===k); if(!r) return;
   const q=+src[k]||0; if(q<=0) return;
   const B=batchOf(r,q,st);
   rev+=(+r.p||0)*q; hrs+=B.hours; prof+=(+r.p||0)*q-B.cost;
  });
 }
 const ph=hrs>0?prof/hrs:0;
 put('d_rev',rev?fmt(rev)+' ₽':'—');
 put('d_revs',hasNow?'выручка, текущая неделя':HIST.length?'выручка, '+ruDate(HIST[HIST.length-1].d):'впишите продажи в «Тесте полки»');
 put('d_ph',hrs>0?fmt(ph)+' ₽':'—');
 const phs=document.getElementById('d_phs');
 if(phs) phs.textContent=hrs>0?(ph>=NORM?'за час печати — норма':'за час печати — ниже нормы'):'за час печати';
 const phEl=document.getElementById('d_ph');
 if(phEl&&hrs>0) phEl.style.color=ph>=NORM?'#7ff0b0':'#ffc9c2'; else if(phEl) phEl.style.color='';
 // пластик
 const stock=SPOOLS.reduce((s,x)=>s+(+x.g||0),0);
 let week=num('sp_week');
 const au=document.getElementById('sp_auto');
 if(au&&au.checked) week=PLAN_NEED.g||week;
 const days=week>0?stock/(week/7):0;
 put('d_plast',fmt2(stock/1000)+' кг');
 put('d_plasts',week>0?'пластика ≈ на '+fmt(days)+' дн.':'пластика на складе');
 const dp=document.getElementById('d_plast');
 if(dp) dp.style.color=(week>0&&days<14)?'#ffc9c2':'';
 // окупаемость
 put('d_pay',PAYBACK.month?PAYBACK.month+'-й мес.':'—');
 put('d_pays',PAYBACK.month?'точка окупаемости':'за 6 мес. не окупается');
 // загрузка
 put('d_load',hrs>0?Math.round(hrs/CAP*100)+'%':'—');
 bkStatus();
}

// ================================================================
function refreshAll(){
 buildPreset(); buildCat(); buildShelf(); buildB2B(); buildPlan(); buildSpools(); buildHist();
 calcAll();
}
function calcAll(){
 try{calcOrder()}catch(e){}
 try{calcPay()}catch(e){}
 try{calcShelf()}catch(e){}
 try{calcB2B()}catch(e){}
 try{calcPlan()}catch(e){}
 try{calcPlastic()}catch(e){}
 try{buildCat()}catch(e){}
 try{dash()}catch(e){}
}

// ---------- init ----------
buildPreset();
buildChan();
buildCat();
buildShelf();
buildB2B();
buildPlan();
buildSpools();
buildHist();
catEvents();
shelfEvents();
histEvents();
planEvents();
spoolEvents();
backupEvents();
bindSave();
initChecks();
// чекбокс «считать партиями» — сохраняем отдельно (bindSave работает по value)
const cb=document.getElementById('c_batch');
if(cb){
 const v=localStorage.getItem('c_batchchk');
 if(v!==null) cb.checked=v==='1';
 cb.addEventListener('change',()=>{localStorage.setItem('c_batchchk',cb.checked?'1':'0');refreshAll();});
}
const bsel=document.getElementById('b_item');
if(bsel) bsel.addEventListener('change',()=>{localStorage.setItem('f_bitem',bsel.value);
 const r=CATALOG[+bsel.value];const ri=document.getElementById('b_retail');
 if(r&&ri){ri.value=r.p;localStorage.setItem('f_bretail',r.p);}
 calcAll();});
calcAll();
upd();
