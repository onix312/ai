"use strict";
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var st = { initData:"", me:null, screen:"home", ostatus:"all", shelf:[], orders:[], queue:[], printers:[], inbox:[], summary:null };

  function esc(s){ return String(s==null?"":s).replace(/[&<>\"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];}); }
  function toast(t){ var m=$("msg"); if(!m) return; m.textContent=t; m.classList.add("on"); clearTimeout(toast._t); toast._t=setTimeout(function(){m.classList.remove("on");},2600); }
  function money(v){ var n=Number(v)||0; return (Math.round(n*100)/100).toLocaleString("ru-RU")+" ₽"; }

  function tgInitData(){
    try{
      if(window.Telegram && Telegram.WebApp && Telegram.WebApp.initData) return Telegram.WebApp.initData;
    }catch(e){}
    return "";
  }

  function api(path, opts){
    opts = opts || {};
    var method = opts.method || "GET";
    var body = opts.body || null;
    var url = path;
    // initData передаём заголовком и дублируем query для GET (на случай если Ctx.headers нет)
    var initData = st.initData || tgInitData() || "";
    var headers = { "Accept":"application/json" };
    if(initData) headers["X-Telegram-Init-Data"] = initData;
    if(body){ headers["Content-Type"]="application/json"; }
    // Для GET добавляем initData в query если есть, чтобы пройти через старый Ctx
    if(method==="GET" && initData){
      url += (url.indexOf("?")>=0 ? "&" : "?") + "initData=" + encodeURIComponent(initData);
    }
    // Тестовый режим: ?test_user_id=
    try{
      var m = /[?&]test_user_id=([^&]+)/.exec(location.search);
      if(m && !initData){
        url += (url.indexOf("?")>=0 ? "&" : "?") + "test_user_id=" + encodeURIComponent(decodeURIComponent(m[1]));
      }
    }catch(e){}
    return fetch(url, { method:method, headers:headers, body: body ? JSON.stringify(body) : null, cache:"no-store" })
      .then(function(r){ return r.json().then(function(d){ if(!r.ok) throw new Error(d.error||("ошибка "+r.status)); return d; }); });
  }

  function renderWho(){
    var who = $("who");
    var net = $("net");
    if(!who) return;
    if(!st.me){ who.textContent="нет доступа"; if(net){net.textContent="нет связи"; net.className="pill bad";} return; }
    var s = st.me.staff || {};
    who.textContent = (s.name||"") + " · " + (s.role_name||s.role||"");
    if(net){ net.textContent="связь есть"; net.className="pill ok"; }
    // Расширяем табы по роли
    var tabs = document.querySelector(".tabs");
    if(tabs && s.role){
      var role = s.role;
      var extra = "";
      if(role==="owner" || role==="manager"){
        extra += '<button class="tab" data-screen="inbox">Inbox<span class="badge" id="badge_inbox" hidden>0</span></button>';
        extra += '<button class="tab" data-screen="money">Деньги</button>';
      }
      if(role==="owner"){
        extra += '<button class="tab" data-screen="team">Команда</button>';
      }
      // Если уже добавлены — не дублируем
      if(!tabs.querySelector('[data-screen="inbox"]') && extra) {
        // Заменяем сетку: 5 -> auto
        tabs.style.gridTemplateColumns = "repeat(7,1fr)";
        var tmp = document.createElement("div");
        tmp.innerHTML = extra;
        while(tmp.firstChild) tabs.appendChild(tmp.firstChild);
      }
    }
  }

  function renderHome(){
    var box = $("home_stats");
    if(!box) return;
    var sum = st.summary || {};
    var farm = sum.farm || {};
    var html = "";
    html += '<div class="card"><div class="kv"><span>Принтеры</span><b>'+esc((farm.online||0)+"/"+(farm.total||0))+'</b></div><div class="kv"><span>Печатает</span><b>'+esc(farm.printing||0)+'</b></div><div class="kv"><span>Очередь</span><b>'+esc(sum.queue||0)+'</b></div></div>';
    html += '<div class="card"><div class="kv"><span>Готово к выдаче</span><b>'+esc(sum.orders_ready||0)+'</b></div><div class="kv"><span>Inbox</span><b>'+esc(sum.inbox||0)+'</b></div><div class="kv"><span>Низкий остаток</span><b>'+esc(sum.low_stock||0)+'</b></div><div class="kv"><span>Сегодня</span><b>'+esc(money(sum.today_money||0))+'</b></div></div>';
    box.innerHTML = html;

    var alerts = $("home_alerts");
    if(alerts){
      var parts = [];
      if((sum.low_stock||0)>0) parts.push("Низкий остаток: "+sum.low_stock+" поз.");
      if((sum.inbox||0)>0) parts.push("Непрочитанные в inbox: "+sum.inbox);
      if((sum.orders_ready||0)>0) parts.push("Готовы к выдаче: "+sum.orders_ready);
      if(parts.length){ alerts.hidden=false; alerts.innerHTML="<h3>Внимание</h3><div style='font-size:13px;line-height:1.5'>"+parts.map(esc).join("<br>")+"</div>"; }
      else alerts.hidden=true;
    }
    var bO = $("badge_orders");
    if(bO){ if((sum.orders_ready||0)>0){ bO.hidden=false; bO.textContent=sum.orders_ready; } else bO.hidden=true; }
    var bI = $("badge_inbox");
    if(bI){ if((sum.inbox||0)>0){ bI.hidden=false; bI.textContent=sum.inbox; } else bI.hidden=true; }
  }

  function renderShelf(){
    var q = ($("shelf_search") && $("shelf_search").value || "").toLowerCase().trim();
    var list = st.shelf || [];
    if(q) list = list.filter(function(it){ return (String(it.name||"").toLowerCase().indexOf(q)>=0) || (String(it.sku||"").toLowerCase().indexOf(q)>=0); });
    var box = $("shelf_list");
    if(!box) return;
    if(!list.length){ box.innerHTML='<div class="empty"><b>Полка пуста</b>Нет позиций или ничего не нашлось</div>'; return; }
    box.innerHTML = list.slice(0,80).map(function(it){
      var low = Number(it.qty||0) <= Number(it.min_qty||2);
      return '<div class="row"><div style="flex:1"><b>'+esc(it.name||it.id)+'</b><small>'+esc((it.category_name||"")+" · "+(it.qty||0)+" шт"+(it.price?" · "+money(it.price):""))+'</small></div>'+(low?'<span class="pill warn">мало</span>':'')+'</div>';
    }).join("");
  }

  function renderOrders(){
    var box = $("orders_list");
    if(!box) return;
    var list = st.orders || [];
    if(st.ostatus!=="all") list = list.filter(function(o){ return String(o.status||"")===st.ostatus; });
    if(!list.length){ box.innerHTML='<div class="empty"><b>Заказов нет</b>В этом фильтре пусто</div>'; return; }
    box.innerHTML = list.slice(0,50).map(function(o){
      return '<div class="row"><div style="flex:1"><b>№'+esc(o.number||o.id)+' · '+esc(o.product||"")+'</b><small>'+esc((o.status||"")+" · "+(o.customer_name||"")+" · "+money(o.price||0))+'</small></div><span class="pill">'+esc(o.status||"")+'</span></div>';
    }).join("");
  }

  function renderQueue(){
    var box = $("queue_list");
    if(!box) return;
    var list = st.queue || [];
    if(!list.length){ box.innerHTML='<div class="empty"><b>Очередь пуста</b>Принтеры свободны</div>'; return; }
    box.innerHTML = list.map(function(j){
      var title = j.name || j.file || "задание";
      var meta = [];
      if(j.order && j.order.number) meta.push("заказ №"+j.order.number);
      if(j.est_minutes) meta.push(Math.round(j.est_minutes)+" мин");
      return '<div class="row"><div style="flex:1"><b>'+esc(title)+'</b><small>'+esc(meta.join(" · ")|| (j.state||""))+'</small></div><span class="pill">'+esc(j.state||"")+'</span></div>';
    }).join("");
  }

  function renderPrinters(){
    var box = $("printers_list");
    if(!box) return;
    var list = st.printers || [];
    if(!list.length){ box.innerHTML='<div class="empty"><b>Принтеров нет</b>Добавьте в панели</div>'; return; }
    box.innerHTML = list.map(function(p){
      var conn = p.connected ? "в сети" : "нет связи";
      return '<div class="row"><div style="flex:1"><b>'+esc(p.name||p.id)+'</b><small>'+esc((p.state||"")+" · "+conn)+'</small></div><span class="pill '+(p.connected?"ok":"bad")+'">'+esc(conn)+'</span></div>';
    }).join("");
  }

  function renderInbox(){
    var box = $("inbox_list");
    if(!box) return;
    var list = st.inbox || [];
    if(!list.length){ box.innerHTML='<div class="empty"><b>Inbox пуст</b>Нет диалогов</div>'; return; }
    box.innerHTML = list.map(function(c){
      return '<div class="row"><div style="flex:1"><b>'+esc(c.name||c.chat_id)+'</b><small>'+esc((c.phone||"")+" · непрочитано "+(c.unread||0))+'</small></div>'+((c.unread||0)>0?'<span class="pill warn">'+esc(c.unread)+'</span>':'')+'</div>';
    }).join("");
  }

  function renderMoney(){
    var box = $("money_box");
    if(!box) return;
    if(!st.finance){ box.innerHTML='<div class="empty">Загрузка…</div>'; return; }
    var t = st.finance.today || {};
    var w = st.finance.week || {};
    box.innerHTML = '<div class="card"><div class="kv"><span>Сегодня доход</span><b>'+esc(money(t.income||0))+'</b></div><div class="kv"><span>Расход</span><b>'+esc(money(t.expense||0))+'</b></div><div class="kv"><span>Неделя доход</span><b>'+esc(money(w.income||0))+'</b></div></div>';
  }

  function renderTeam(){
    var box = $("team_box");
    if(!box) return;
    if(!st.team){ box.innerHTML='<div class="empty">Загрузка…</div>'; return; }
    var staff = st.team.staff || [];
    var inv = st.team.invites || [];
    var html = '<div class="card"><h3>Команда</h3><div class="list">'+staff.map(function(s){ return '<div class="row"><div style="flex:1"><b>'+esc(s.name||"")+' · '+esc(s.role||"")+'</b><small>chat '+esc(s.chat_id||"")+' · tg '+esc(s.tg_user_id||"")+'</small></div><span class="pill '+(s.active?'ok':'bad')+'">'+(s.active?'активен':'откл')+'</span></div>'; }).join("")+'</div></div>';
    if(inv.length) html += '<div class="card"><h3>Приглашения</h3><div class="list">'+inv.map(function(i){ return '<div class="row"><div style="flex:1"><b>'+esc(i.code)+' · '+esc(i.role)+'</b><small>'+esc(i.name||"")+' · '+(i.used?'исп':'акт')+'</small></div></div>'; }).join("")+'</div></div>';
    box.innerHTML = html;
  }

  function showScreen(name){
    st.screen = name;
    ["home","shelf","orders","queue","printers","inbox","money","team"].forEach(function(s){
      var el = $("screen_"+s);
      if(el) el.hidden = (s!==name);
    });
    document.querySelectorAll(".tab").forEach(function(b){
      b.classList.toggle("on", b.getAttribute("data-screen")===name);
    });
    try{ localStorage.setItem("staff_screen", name); }catch(e){}
    // lazy load
    if(name==="shelf" && !st.shelf.length) loadShelf();
    if(name==="orders") loadOrders();
    if(name==="queue") loadQueue();
    if(name==="printers") loadPrinters();
    if(name==="inbox") loadInbox();
    if(name==="money") loadMoney();
    if(name==="team") loadTeam();
    // Telegram BackButton
    try{
      if(window.Telegram && Telegram.WebApp){
        if(name==="home") Telegram.WebApp.BackButton.hide();
        else Telegram.WebApp.BackButton.show();
      }
    }catch(e){}
  }

  function loadMe(){
    return api("/api/staff/me").then(function(d){
      if(!d.ok){ throw new Error(d.error||"Нет доступа"); }
      st.me = d;
      renderWho();
      return d;
    });
  }
  function loadSummary(){
    return api("/api/staff/summary").then(function(d){
      if(!d.ok) return;
      st.summary = d;
      renderHome();
    }).catch(function(){});
  }
  function loadShelf(){
    return api("/api/staff/shelf").then(function(d){
      if(d.ok){ st.shelf = d.items||[]; renderShelf(); }
    }).catch(function(){});
  }
  function loadOrders(){
    return api("/api/staff/orders?status=all&limit=50").then(function(d){
      if(d.ok){ st.orders = d.orders||[]; renderOrders(); }
    }).catch(function(){});
  }
  function loadQueue(){
    return api("/api/staff/queue").then(function(d){
      if(d.ok){ st.queue = d.queue||[]; renderQueue(); }
    }).catch(function(){});
  }
  function loadPrinters(){
    return api("/api/staff/printers").then(function(d){
      if(d.ok){ st.printers = d.printers||[]; renderPrinters(); }
    }).catch(function(){});
  }
  function loadInbox(){
    return api("/api/staff/inbox").then(function(d){
      if(d.ok){ st.inbox = d.chats||[]; renderInbox(); }
    }).catch(function(){});
  }
  function loadMoney(){
    return api("/api/staff/finance").then(function(d){
      if(d.ok){ st.finance = d; renderMoney(); }
      else { var box=$("money_box"); if(box) box.innerHTML='<div class="empty"><b>'+esc(d.error||"Нет доступа")+'</b></div>'; }
    }).catch(function(){});
  }
  function loadTeam(){
    return api("/api/staff/team").then(function(d){
      if(d.ok){ st.team = d; renderTeam(); }
      else { var box=$("team_box"); if(box) box.innerHTML='<div class="empty"><b>'+esc(d.error||"Нет доступа")+'</b></div>'; }
    }).catch(function(){});
  }

  function bind(){
    document.addEventListener("click", function(e){
      var tab = e.target.closest && e.target.closest(".tab");
      if(tab){ showScreen(tab.getAttribute("data-screen")); return; }
      var go = e.target.closest && e.target.closest("[data-go]");
      if(go){ showScreen(go.getAttribute("data-go")); return; }
      var os = e.target.closest && e.target.closest("[data-ostatus]");
      if(os){ st.ostatus = os.getAttribute("data-ostatus"); renderOrders(); document.querySelectorAll("[data-ostatus]").forEach(function(b){ b.classList.toggle("go", b.getAttribute("data-ostatus")===st.ostatus); }); return; }
    });
    var ss = $("shelf_search");
    if(ss) ss.addEventListener("input", renderShelf);
    try{
      if(window.Telegram && Telegram.WebApp){
        Telegram.WebApp.ready();
        Telegram.WebApp.expand();
        Telegram.WebApp.BackButton.onClick(function(){ showScreen("home"); });
      }
    }catch(e){}
    // Сохранённый экран
    try{ var saved = localStorage.getItem("staff_screen"); if(saved) st.screen = saved; }catch(e){}
  }

  function init(){
    st.initData = tgInitData();
    bind();
    showScreen(st.screen||"home");
    loadMe().then(function(){
      loadSummary();
      loadShelf();
      loadOrders();
      loadQueue();
      loadPrinters();
    }).catch(function(err){
      var who=$("who"); if(who) who.textContent=err.message||"нет доступа";
      var net=$("net"); if(net){ net.textContent="нет доступа"; net.className="pill bad"; }
      toast(err.message||"Нет доступа — откройте через Telegram");
    });
    // Обновление каждые 15с
    setInterval(function(){ if(document.hidden) return; loadSummary(); if(st.screen==="queue") loadQueue(); if(st.screen==="printers") loadPrinters(); }, 15000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
