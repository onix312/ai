
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
 document.querySelectorAll('input[data-k]').forEach(inp=>{
  const k='f_'+inp.dataset.k;
  const v=localStorage.getItem(k);
  if(v!==null&&v!=='') inp.value=v;
  inp.addEventListener('input',()=>{localStorage.setItem(k,inp.value);calcAll();});
 });
}
const num=id=>parseFloat(document.getElementById(id).value.replace(',','.'))||0;
const fmt=n=>Math.round(n).toLocaleString('ru-RU');
const fmt2=n=>(Math.round(n*100)/100).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});

// ---------- КАЛЬКУЛЯТОР ЗАКАЗА ----------
function calcOrder(){
 const w=num('c_w'),h=num('c_h'),sp=num('c_sp'),sw=num('c_sw'),q=num('c_q'),
  br=num('c_br'),am=num('c_am'),el=num('c_el'),mn=num('c_mn'),hr=num('c_hr'),
  ex=num('c_ex'),pk=num('c_pk'),mk=num('c_mk');
 const k=1+br/100;
 const mat=sw>0?w/sw*sp*k:0, amo=h*am*k, ele=h*el*k, lab=mn/60*hr, oth=ex+pk;
 const cost=mat+amo+ele+lab+oth;
 const price=Math.ceil(cost*(1+mk/100)/10)*10;
 const total=price*q;
 const profit=total-cost*q;
 const perHour=h>0?profit/(h*q):0;
 const minp=Math.max(400,Math.ceil(cost*1.4/10)*10);
 const g=w>0?price/w:0;
 document.getElementById('r_mat').textContent=fmt2(mat)+' ₽';
 document.getElementById('r_amo').textContent=fmt2(amo)+' ₽';
 document.getElementById('r_ele').textContent=fmt2(ele)+' ₽';
 document.getElementById('r_lab').textContent=fmt2(lab)+' ₽';
 document.getElementById('r_oth').textContent=fmt2(oth)+' ₽';
 document.getElementById('r_cost').textContent=fmt2(cost)+' ₽';
 document.getElementById('r_price').textContent=fmt(price)+' ₽';
 document.getElementById('r_total').textContent=fmt(total)+' ₽';
 document.getElementById('r_profit').textContent=fmt(profit)+' ₽';
 document.getElementById('r_ph').textContent=fmt(perHour)+' ₽/ч';
 document.getElementById('r_min').textContent=fmt(minp)+' ₽';
 document.getElementById('r_g').textContent=fmt2(g)+' ₽/г';
 const v=document.getElementById('r_verdict');
 if(perHour>=250){v.className='verdict v-ok';v.textContent='✅ Отличный заказ. '+fmt(perHour)+' ₽ за час печати — это в норме (250–500 ₽/ч). Берите.';}
 else if(perHour>=100){v.className='verdict v-warn';v.textContent='⚠️ Слабовато: '+fmt(perHour)+' ₽/ч при норме 250–500. Берите ради отзыва или постоянного клиента, иначе поднимите цену.';}
 else {v.className='verdict v-bad';v.textContent='❌ Невыгодно: всего '+fmt(perHour)+' ₽ за час работы принтера. Поднимите цену или откажитесь — принтер будет занят зря.';}
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
 document.getElementById('p_rows').innerHTML=rows;
 const v=document.getElementById('p_verdict');
 if(month){v.className='verdict v-ok';v.innerHTML='✅ Принтер окупается на <b>'+month+'-м месяце</b>. Накоплено за полгода: <b>'+fmt(cum)+' ₽</b>, сверх вложений: <b>'+fmt(cum-inv)+' ₽</b>.';}
 else{v.className='verdict v-warn';v.innerHTML='⚠️ За 6 месяцев не окупается: накоплено '+fmt(cum)+' ₽ из '+fmt(inv)+' ₽. Не хватает <b>'+fmt(inv-cum)+' ₽</b>. Поднимите выручку или сократите расходы.';}
 document.getElementById('p_need').textContent=fmt(inv/6)+' ₽';
}

// ---------- КАЛЬКУЛЯТОР ПОЛКИ ----------
const SHELF=[
 ['Дракон подвижный 18 см',45,3.0,550],['Дракон большой 28 см',110,5.5,890],
 ['Змейка подвижная',25,1.5,250],['Фиджет-крутилка',18,1.0,250],
 ['Осьминог-настроение',22,1.3,300],['Мини-фигурка (корзинка)',8,0.5,150],
 ['Брелок с именем',8,0.5,350],['Крючок на дверь',12,0.7,150],
 ['Клипсы для пакетов, 5 шт',15,0.8,200],['Подставка под телефон',25,1.3,350],
 ['Держатель щёток',40,2.0,300],['Органайзер для мелочи',70,3.5,500],
 ['Ваза-спираль',55,2.0,600],['Ночник',90,4.5,900],
 ['Топпер на торт',7,0.4,500],['Вырубка для печенья',12,0.6,250],
 ['Держатель ценника',10,0.5,250],['Подарочный набор',120,6.0,1200]
];
function buildShelf(){
 const tb=document.getElementById('sh_body'); if(!tb) return;
 tb.innerHTML=SHELF.map((r,i)=>`<tr>
 <td class="lbl">${r[0]}</td><td class="out">${r[1]}</td><td class="out">${r[2]}</td>
 <td class="out" id="sh_c${i}">—</td>
 <td><input type="number" data-k="shp${i}" id="sh_p${i}" value="${r[3]}"></td>
 <td class="out" id="sh_m${i}">—</td><td class="out" id="sh_h${i}">—</td>
 <td><input type="number" data-k="shs${i}" id="sh_s${i}" placeholder="0"></td>
 <td class="out" id="sh_v${i}" style="font-size:11.5px">—</td></tr>`).join('');
}
function calcShelf(){
 const sp=num('c_sp')||1600, sw=num('c_sw')||1000, am=num('c_am')||12, el=num('c_el')||3.5, br=num('c_br')||8;
 let totRev=0,totProf=0;
 SHELF.forEach((r,i)=>{
  const cost=(r[1]/sw*sp+r[2]*(am+el))*(1+br/100);
  const price=num('sh_p'+i), sold=num('sh_s'+i);
  const marg=price-cost, ph=r[2]>0?marg/r[2]:0;
  totRev+=price*sold; totProf+=marg*sold;
  document.getElementById('sh_c'+i).textContent=fmt(cost);
  document.getElementById('sh_m'+i).textContent=fmt(marg);
  document.getElementById('sh_h'+i).textContent=fmt(ph);
  const v=document.getElementById('sh_v'+i);
  const s=document.getElementById('sh_s'+i).value;
  if(s===''){v.textContent='—';v.style.color='#6b7891';}
  else if(sold>=3){v.textContent='МАСШТАБИРОВАТЬ';v.style.color='#14663a';}
  else if(sold>=1){v.textContent='оставить';v.style.color='#8a5a06';}
  else{v.textContent='убрать с полки';v.style.color='#96261a';}
 });
 document.getElementById('sh_rev').textContent=fmt(totRev)+' ₽';
 document.getElementById('sh_prof').textContent=fmt(totProf)+' ₽';
}

function calcAll(){try{calcOrder()}catch(e){} try{calcPay()}catch(e){} try{calcShelf()}catch(e){}}

// ---------- init ----------
buildShelf();
bindSave();
initChecks();
calcAll();
upd();
