#!/usr/bin/env node
/**
 * Headless-стенд страницы кассы: проверяем то, что нельзя увидеть в браузере
 * без телефона — что скрипт вообще поднимается на заглушке DOM и что
 * офлайн-очередь держит ТОТ ЖЕ номер продажи (риск F1 из замера 17.0.13).
 *
 * node --check ловит только синтаксис, а опечатка в имени функции или
 * обращение к несуществующему узлу видны лишь при выполнении.
 * Замеры на живом коннекторе — отдельно, scripts/kassa-lan.js.
 */
'use strict';
const { createKassa } = require('./kassa-harness');

const problems = [];
const pending = [];
function check(name, fn) {
  try {
    const result = fn();
    if (result === false) throw new Error('вернулось false');
    console.log(`  ok  ${name}`);
  } catch (e) {
    problems.push(`${name}: ${e.message}`);
    console.error(`  FAIL ${name}: ${e.stack || e.message}`);
  }
}
function checkAsync(name, fn) {
  pending.push(Promise.resolve().then(fn).then(
    () => console.log(`  ok  ${name}`),
    (e) => {
      problems.push(`${name}: ${e.message}`);
      console.error(`  FAIL ${name}: ${e.stack || e.message}`);
    },
  ));
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 25));

console.log('Стенд кассы: запуск скрипта на заглушке DOM');
const k = createKassa({ origin: 'http://127.0.0.1:8766', transport: 'stub' });
try {
  k.run();
  console.log('  ok  скрипт загрузился без исключений');
} catch (e) {
  problems.push(`загрузка: ${e.message}`);
  console.error(`  FAIL загрузка: ${e.stack || e.message}`);
}

if (problems.length === 0) {
  console.log('\nСценарии 17.0.13');

  // 1. Обрыв на ответе: очередь обязана взять номер уже отправленного запроса.
  check('offAccept сохраняет request_id отправленного запроса', () => {
    k.ev('state.cart={"s1":1};state._allItems=[{id:"s1",name:"Адресник",price:500}];'
      + 'offQueue=[];offAccept("cash","ui-12345",true);');
    const queue = k.ev('offQueue');
    if (queue.length !== 1) throw new Error(`в очереди ${queue.length} записей`);
    if (queue[0].id !== 'ui-12345') throw new Error(`номер стал ${queue[0].id}`);
    if (!queue[0].pending) throw new Error('запись не помечена «результат неизвестен»');
  });

  // 2. Обычная офлайн-продажа (без отправленного запроса) получает свой номер.
  check('offAccept без request_id придумывает номер очереди', () => {
    k.ev('state.cart={"s1":1};offQueue=[];offAccept("cash");');
    const queue = k.ev('offQueue');
    if (!/^of-/.test(queue[0].id)) throw new Error(`номер ${queue[0].id} не похож на очередь`);
  });

  // 3. Запросы не висят вечно: таймаут помечает связь мёртвой и включает офлайн.
  check('таймаут запроса помечает канал недоступным', () => {
    const dead = createKassa({
      transport: 'stub',
      respond: () => ({ reject: Object.assign(new Error('aborted'), { name: 'AbortError' }) }),
    });
    dead.run();
    dead.ev('LINK.ok=true;state.offline=false;');
    return dead.ev('api("/api/cashier/catalog").then(function(){return "ok";},'
      + 'function(e){if(!e.offline)throw new Error("ошибка не помечена offline");'
      + 'if(LINK.ok)throw new Error("канал остался зелёным");return "offline";});');
  });

  // 4. Сверка цен видит расхождение между экраном кассира и каталогом.
  check('priceDiffs находит изменившуюся цену', () => {
    k.ev('state.cart={"s1":2};state.cartSeen={"s1":500};'
      + 'state._allItems=[{id:"s1",name:"Адресник",price:550}];');
    const diffs = k.ev('priceDiffs()');
    if (diffs.length !== 1 || diffs[0].now !== 550 || diffs[0].was !== 500) {
      throw new Error(JSON.stringify(diffs));
    }
    k.ev('state.cartSeen={"s1":550};');
    if (k.ev('priceDiffs()').length) {
      throw new Error('цена, которую кассир видел, совпадает с каталогом — предупреждения быть не должно');
    }
  });

  // 5. Событие каталога и пинг потока обрабатываются без исключений.
  check('поток: кадры ping и catalog_changed не ломают страницу', () => {
    k.ev('loggedIn=function(){return true;};connectMoney();');
    const es = k.sources[k.sources.length - 1];
    if (!es) throw new Error('EventSource не открылся');
    es.emitNamed('ping', { at: 'now' });
    es.emitNamed('catalog_changed', { reason: 'catalog_save' });
    es.emitNamed('event', { id: 1, data: { signal: 'paid', amount: 500 } });
  });

  // 6. Смена адреса сервера = другой origin: localStorage страницы пуст, но
  //    копия очереди живёт в памяти оболочки и возвращается на новом адресе.
  check('очередь переживает смену адреса (копия в оболочке)', () => {
    const stash = { value: '' };
    const app = {
      queueSave: (json) => { stash.value = json || ''; },
      queueLoad: () => stash.value,
    };
    const first = createKassa({ transport: 'stub', bridge: app });
    first.run();
    first.ev('state.cart={"s1":1};state._allItems=[{id:"s1",name:"Адресник",price:500}];'
      + 'offQueue=[];offAccept("cash");');
    const queued = first.ev('offQueue');
    if (queued.length !== 1) throw new Error(`в очередь не попало: ${queued.length}`);
    if (JSON.parse(stash.value || '[]').length !== 1) {
      throw new Error('копия в памяти оболочки не обновилась');
    }
    // Новый адрес: другая память страницы, та же оболочка.
    const second = createKassa({ transport: 'stub', bridge: app });
    second.run();
    const restored = second.ev('offQueue');
    if (restored.length !== 1 || restored[0].id !== queued[0].id) {
      throw new Error(`на новом адресе очередь ${restored.length}, номер `
        + `${restored[0] && restored[0].id} вместо ${queued[0].id}`);
    }
  });

  // 7. Индикатор связи рисуется для трёх состояний.
  check('netState переключает точку и полосу', () => {
    k.ev('netMark(false,"тест");');
    const dot = k.ev('$("netDot")');
    const bar = k.ev('$("linkBar")');
    if (!dot._classes.has('off')) throw new Error('точка не покраснела');
    if (bar.hidden) throw new Error('полоса «нет связи» скрыта');
    k.ev('netMark(true);');
    if (dot._classes.has('off')) throw new Error('точка не позеленела после возврата связи');
  });

  // 8. Дефект оболочки 17.0.14: «Поток событий молчит» висел вечно.
  //    В приложении EventSource поднимается не всегда, касса честно уходила на
  //    страховочный опрос раз в 25 с — деньги приходили, но опрос «живость»
  //    канала не отмечал, и плашка не скрывалась до перезапуска кассы.
  check('опрос снимает «поток событий молчит»', () => {
    k.ev('LINK.ok=true;LINK.stream=0;LINK.poll=0;state.offline=false;netState();');
    const bar = k.ev('$("linkBar")');
    if (bar.hidden) throw new Error('ни потока, ни опроса — плашка обязана предупреждать');
    if (!bar._classes.has('warn')) throw new Error('плашка не помечена как «молчит»');
    // Сервер ответил на опрос — канал жив, предупреждению не место.
    k.ev('LINK.poll=Date.now();netState();');
    if (!bar.hidden) throw new Error('после успешного опроса плашка не скрылась');
    if (bar._classes.has('warn')) throw new Error('класс «молчит» остался после опроса');
    // И обратно: опрос устарел вместе с потоком — предупреждение возвращается.
    k.ev('LINK.poll=Date.now()-POLL_QUIET-1000;netState();');
    if (bar.hidden) throw new Error('устаревший опрос не должен считаться живым каналом');
  });

  // 9. Полоса «не проведено» обязана скрываться, как только очередь пуста.
  //    Претензия владельца была «не скрывается»: если записи ушли, а полоса
  //    осталась, кассир вечно видит несуществующий долг перед учётом.
  check('полоса офлайн-очереди скрывается при пустой очереди', () => {
    k.ev('offQueue=[];offProg={busy:false,done:0,total:0,failed:0};'
      + 'state.cart={"s1":1};state._allItems=[{id:"s1",name:"Адресник",price:500}];'
      + 'offAccept("cash");offRender();');
    const bar = k.ev('$("offBar")');
    if (bar.hidden) throw new Error('в очереди запись, а полоса скрыта');
    if (k.ev('offTotals().n') !== 1) throw new Error('в очереди не одна запись');
    // Записи ушли — полоса обязана исчезнуть, а не остаться висеть.
    k.ev('offQueue=[];offRender();');
    if (!bar.hidden) throw new Error('очередь пуста, а полоса не скрылась');
  });

  // 10. «Не показывать» рядом со «Скачать». Претензия владельца 12.09.2026 —
  //     «не работает кнопка»: обработчик был на месте, баннер не исчезал,
  //     потому что `hidden` перебивался CSS страницы (контракт на CSS живёт в
  //     test_site_markup.test_hidden_attribute_actually_hides_bars — у этой
  //     заглушки DOM свойства hidden есть, а стилей нет). Здесь своя половина:
  //     клик прячет баннер, а отказ переживает перезапуск страницы.
  check('«Не показывать» прячет баннер и запоминает отказ', () => {
    const box = k.ev('$("installHint")');
    k.ev('var b=$("installOff");'
      + 'if(!b._h||!b._h.click)throw new Error("обработчик не повешен");'
      + 'b._h.click.call(b);');
    if (!box.hidden) throw new Error('баннер не скрылся после «Не показывать»');
    if (k.storage.getItem('cashier_install_off') !== '1') {
      throw new Error('отказ не запомнен в localStorage');
    }
    // Перезапуск с той же «памятью телефона»: баннер не показывается и сборку
    // больше не спрашивает.
    const again = createKassa({ transport: 'stub', storage: k.storage });
    again.run();
    again.ev('installHint();');
    if (!again.ev('$("installHint")').hidden) throw new Error('баннер снова показан после отказа');
    if (again.callsFor('/api/app/android').length) {
      throw new Error('после отказа касса всё равно спрашивает сборку');
    }
  });

  console.log('\nСценарии 17.0.16');

  // 11. Кассир печатает без «ё»: «пленка» обязана находить «Плёнка матовая».
  //     Раньше сравнение шло по toLowerCase() и товар не находился, хотя лежал
  //     на витрине — продавец уходил искать его глазами.
  check('поиск не требует «ё» и лишних пробелов', () => {
    k.ev('state.items=[{id:"s1",name:"Плёнка матовая",qty:5,price:300,barcode:"4600001"},'
      + '{id:"s2",name:"PLA  серый",qty:5,price:900,barcode:"4600002"}];state.cat="";');
    k.ev('state.search="пленка";');
    let ids = k.ev('filteredItems().map(function(x){return x.id;})');
    if (ids.length !== 1 || ids[0] !== 's1') {
      throw new Error(`«пленка» нашла ${JSON.stringify(ids)} вместо [s1]`);
    }
    k.ev('state.search="  PLA   СЕр  ";');
    ids = k.ev('filteredItems().map(function(x){return x.id;})');
    if (ids.length !== 1 || ids[0] !== 's2') {
      throw new Error(`«  PLA   СЕр  » нашла ${JSON.stringify(ids)} вместо [s2]`);
    }
    k.ev('state.search="4600001";');
    ids = k.ev('filteredItems().map(function(x){return x.id;})');
    if (ids.length !== 1 || ids[0] !== 's1') throw new Error('поиск по баркоду сломался');
  });

  // 12. Баннер «Скачать приложение» больше не собирают строкой HTML: версия,
  //     sha256 и changelog приходят из полей сборки, и innerHTML в кассе — это
  //     риск развалить разметку вместе с кнопкой «Скачать».
  checkAsync('баннер приложения собирается узлами, а не innerHTML', async () => {
    const fresh = createKassa({ transport: 'stub' });
    fresh.run();
    const before = fresh.ev('$("installText")');
    before.innerHTML = 'МЁРТВАЯ РАЗМЕТКА';
    fresh.ev('api=function(){return Promise.resolve({available:true,'
      + 'url:"/app/NOZZA.apk",version:"17.0.15",size_mb:4,sha256:"abcdef123456789",'
      + 'changelog:"строка <img src=x onerror=alert(1)>"});};'
      + 'installHint();');
    await settle();
    {
      const box = fresh.ev('$("installText")');
      if (box.innerHTML !== 'МЁРТВАЯ РАЗМЕТКА') {
        throw new Error('баннер снова пишут через innerHTML');
      }
      if (!/Приложение кассы · 17.0.15/.test(box.textContent)) {
        throw new Error(`в тексте нет версии: ${box.textContent}`);
      }
      // Отпечаток показывается сокращённо — первые 12 символов.
      if (!/sha256 abcdef123456…/.test(box.textContent)) {
        throw new Error(`нет сокращённого sha256: ${box.textContent}`);
      }
      const ups = box.children.filter((c) => c.className === 'upd');
      if (ups.length !== 2) throw new Error(`строк «что нового» ${ups.length}, ждали 2`);
      if (!/<img src=x onerror=alert\(1\)>/.test(ups[0].textContent)) {
        throw new Error('changelog не остался обычным текстом');
      }
    }
  });

  // 13. Страховочный опрос раз в 25 с нужен ровно тогда, когда потока нет:
  //     при живом SSE (ping раз в 20 с) он только грузил ПК и роутер.
  check('опрос раз в 25 с молчит, пока поток живой', () => {
    const timers = [];
    const timed = createKassa({
      transport: 'stub',
      setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    });
    timed.run();
    timed.ev('loggedIn=function(){return true;};var _rb=0;'
      + 'refreshBadge=function(){_rb++;};startMoneyPoll();');
    const tick = timers.find((t) => t.ms === 25000);
    if (!tick) throw new Error('страховочный интервал не заведён');
    timed.ev('LINK.stream=Date.now();');
    tick.fn();
    if (timed.ev('_rb') !== 0) throw new Error('при живом потоке опрос всё равно пошёл');
    timed.ev('LINK.stream=Date.now()-STREAM_QUIET-1000;');
    tick.fn();
    if (timed.ev('_rb') !== 1) throw new Error('без потока страховочный опрос не сработал');
  });

  // 14. Корзина не трогает индикатор связи: netState() на каждое «+»/«−» —
  //     лишняя запись в DOM и лишний Date.now() на каждом касании.
  check('renderCart не перерисовывает индикатор связи', () => {
    const quiet = createKassa({ transport: 'stub' });
    quiet.run();
    quiet.ev('var _ns=0;var _realNetState=netState;'
      + 'netState=function(){_ns++;return _realNetState();};'
      + 'state.cart={"s1":1};state._allItems=[{id:"s1",name:"Адресник",price:500}];'
      + 'state.items=state._allItems;renderCart();');
    if (quiet.ev('_ns') !== 0) throw new Error(`renderCart вызвал netState ${quiet.ev('_ns')} раз`);
  });

  // 15. Очередь больше 200 записей: в память телефона пишутся последние 200,
  //     и кассир обязан об этом услышать, а не узнать утром по потерянным продажам.
  check('очередь больше лимита предупреждает кассира', () => {
    const full = createKassa({ transport: 'stub' });
    full.run();
    full.ev('var _toasts=[];toast=function(t){_toasts.push(t);};'
      + 'offQueue=[];for(var i=0;i<OFF_MAX+3;i++)offQueue.push({id:"of-"+i,at:"2026-09-13T10:00:00"});'
      + 'offSave();');
    if (full.ev('_toasts').length !== 1) {
      throw new Error(`предупреждений ${full.ev('_toasts').length}, ждали 1`);
    }
    if (!/203/.test(full.ev('_toasts')[0])) {
      throw new Error(`в предупреждении нет числа продаж: ${full.ev('_toasts')[0]}`);
    }
    if (JSON.parse(full.storage.getItem('cashier_offline_q') || '[]').length !== 200) {
      throw new Error('в память телефона записано не 200 записей');
    }
    // В памяти страницы очередь осталась целиком — на сервер уйдут все 203.
    if (full.ev('offQueue.length') !== 203) throw new Error('из памяти страницы записи пропали');
  });
}

function report() {
  if (problems.length) {
    console.error(`\nFAIL: стенд кассы — ${problems.length} ошибок`);
    process.exit(1);
  }
  console.log('\nOK: стенд кассы пройден');
}
/* Асинхронные сценарии ждут своего исхода до отчёта: проверка, вернувшая
   promise, раньше считалась пройденной сразу, а её ошибка терялась. */
Promise.all(pending).then(report, report);
