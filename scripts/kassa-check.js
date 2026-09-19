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

  // 16. Превью долгим тапом: кассир держит товар без этикетки и не видит ни
  //     цену, ни остаток, ни артикул. Долгое нажатие открывает карточку, а
  //     следующий за ним тап закрывает её и НЕ кладёт товар в корзину.
  checkAsync('долгое нажатие показывает карточку и не кладёт товар в корзину', async () => {
    const pv = createKassa({ transport: 'stub' });
    pv.run();
    pv.ev('state.items=[{id:"s1",name:"Плёнка матовая",qty:7,price:350,sku:"PL-01",'
      + 'barcode:"4600001",category_name:"Плёнки",photo_url:""}];state.cart={};');
    const tile = { getAttribute: (k) => (k === 'data-id' ? 's1' : null) };
    const down = { target: { closest: (sel) => (sel === '.tile' ? tile : null) }, clientX: 5, clientY: 5 };
    pv.ev('$("grid")._h.pointerdown')
      .call(null, down);
    await new Promise((r) => setTimeout(r, 700));
    if (!pv.ev('!!previewBox')) throw new Error('долгое нажатие не открыло карточку');
    const card = pv.ev('document.body.children').slice(-1)[0];
    const text = JSON.stringify(card);
    if (!/Плёнка матовая/.test(text)) throw new Error('в карточке нет названия');
    // Тап при открытом превью — закрыть, а не продать. closest() отвечает
    // только на .tile: на .qbtn обработчик выходит раньше, и сценарий
    // проверял бы не тот путь.
    const tapOn = { target: { closest: (sel) => (sel === '.tile' ? tile : null) } };
    pv.ev('$("grid")._h.click').call(null, tapOn);
    if (pv.ev('!!previewBox')) throw new Error('тап не закрыл карточку');
    if (pv.ev('Object.keys(state.cart).length') !== 0) {
      throw new Error('тап по открытому превью положил товар в корзину');
    }
    // Короткий тап по-прежнему продаёт.
    pv.ev('$("grid")._h.click').call(null, tapOn);
    if (pv.ev('state.cart["s1"]') !== 1) throw new Error('короткий тап перестал продавать');
  });

  // 18.5 (М3): товар с вариациями — одна плитка на витрине, тап открывает
  // шторку выбора, и в корзину едет именно выбранная вариация. Продажа товара
  // без вариаций не получает ни одного лишнего нажатия (условие прожарки).
  checkAsync('вариантная плитка открывает шторку, кладёт выбранное и не трогает обычную продажу', async () => {
    const pv = createKassa({ transport: 'stub' });
    pv.run();
    pv.ev('state.items=['
      + '{id:"s1",nom_id:"nom1",variant_id:"v1",variant_label:"Чёрный / L",'
      + 'variant_color_hex:"#111111",variant_color:"Чёрный",variant_size:"L",'
      + 'variant_material:"PLA",name:"Адресник",qty:3,price:500,sku:"A-1" },'
      + '{id:"s2",nom_id:"nom1",variant_id:"v2",variant_label:"Белый / M",'
      + 'variant_color_hex:"#eeeeee",variant_color:"Белый",variant_size:"M",'
      + 'variant_material:"PLA",name:"Адресник",qty:2,price:500,sku:"A-2" }'
      + '];state.cart={};renderGrid();');
    const html = pv.ev('$("grid").innerHTML');
    const tiles = (html.match(/data-group=/g) || []).length + (html.match(/data-id=/g) || []).length;
    if (tiles !== 1) throw new Error('плиток на витрине ' + tiles + ', ждали одну групповую');
    if (!/data-group="nom1"/.test(html)) throw new Error('плитка вариаций не собралась в группу');
    if (!/вариантов: 2/.test(html)) throw new Error('плитка не сообщает число вариантов');
    // Тап по групповой плитке: шторка открылась, корзина пуста.
    const tile = { getAttribute: (k) => (k === 'data-group' ? 'nom1' : null) };
    pv.ev('$("grid")._h.click').call(null, {
      target: { closest: (sel) => (sel === '.tile' ? tile : null) },
    });
    if (pv.ev('Object.keys(state.cart).length') !== 0) {
      throw new Error('тап по групповой плитке молча положил товар');
    }
    if (!pv.ev('$("varModal")._classes.has("on")')) {
      throw new Error('шторка вариантов не открылась');
    }
    const sheet = pv.ev('$("varList").innerHTML');
    if (!/Белый \/ M/.test(sheet) || !/Чёрный \/ L/.test(sheet)) {
      throw new Error('в шторке не обе вариации');
    }
    // Выбор «Белый / M» кладёт именно его и закрывает шторку.
    const chip = { getAttribute: (k) => (k === 'data-vid' ? 's2' : null) };
    pv.ev('$("varList")._h.click').call(null, {
      target: { closest: (sel) => (sel.indexOf('button') === 0 ? chip : null) },
    });
    if (pv.ev('state.cart["s2"]') !== 1) throw new Error('выбор вариации положил не её');
    if (pv.ev('state.cart["s1"]')) throw new Error('в корзину попала и невыбранная вариация');
    if (pv.ev('$("varModal")._classes.has("on")')) throw new Error('шторка не закрылась после выбора');
    // Товар без вариаций — ровно один тап в корзину, как и было.
    pv.ev('state.items=[{id:"s9",name:"Плёнка",qty:5,price:350}];state.cart={};renderGrid();');
    const plainHtml = pv.ev('$("grid").innerHTML');
    if (/data-group=/.test(plainHtml)) throw new Error('обычный товар получил группу');
    const plainTile = { getAttribute: (k) => (k === 'data-id' ? 's9' : null) };
    pv.ev('$("grid")._h.click').call(null, {
      target: { closest: (sel) => (sel === '.tile' ? plainTile : null) },
    });
    if (pv.ev('state.cart["s9"]') !== 1) throw new Error('обычная продажа сломалась');
    if (pv.ev('$("varModal")._classes.has("on")')) throw new Error('обычный товар открыл шторку');
  });

  // М4: карусель на групповой плитке — первый кадр всегда общее фото товара,
  // дальше фото вариаций; один кадр — статика (условие владельца).
  checkAsync('карусель групповой плитки: общее фото первым, мало кадров — статика', async () => {
    const pv = createKassa({ transport: 'stub' });
    pv.run();
    pv.ev('state.items=['
      + '{id:"s1",nom_id:"nom1",variant_id:"v1",variant_label:"Чёрный / L",'
      + 'photo_url:"/api/nomenclature/photo.jpg?id=nom1",'
      + 'variant_photo_url:"/api/nomenclature/variant/photo.jpg?id=v1",'
      + 'name:"Адресник",qty:3,price:500},'
      + '{id:"s2",nom_id:"nom1",variant_id:"v2",variant_label:"Белый / M",'
      + 'photo_url:"/api/nomenclature/photo.jpg?id=nom1",'
      + 'variant_photo_url:"/api/nomenclature/variant/photo.jpg?id=v2",'
      + 'name:"Адресник",qty:2,price:500}'
      + '];state.cart={};renderGrid();');
    const html = pv.ev('$("grid").innerHTML');
    const m = html.match(/data-carpix="([^"]+)"/);
    if (!m) throw new Error('плитка с двумя кадрами не получила карусель');
    const frames = JSON.parse(m[1].replace(/&quot;/g, '"'));
    if (frames[0] !== '/api/nomenclature/photo.jpg?id=nom1') {
      throw new Error('первый кадр — не общее фото товара (' + frames[0] + ')');
    }
    if (frames.length !== 3 || frames[1] !== '/api/nomenclature/variant/photo.jpg?id=v1') {
      throw new Error('кадры не в порядке общее → вариации: ' + JSON.stringify(frames));
    }
    if (!/data-cur=/.test(html)) throw new Error('нет метки текущего кадра');
    // Одна вариация с фото → один кадр → атрибута карусели нет вообще.
    pv.ev('state.items=['
      + '{id:"s3",nom_id:"nom9",variant_id:"v9",variant_label:"Один",'
      + 'photo_url:"/api/nomenclature/photo.jpg?id=nom9",'
      + 'variant_photo_url:"",name:"Одиночка",qty:1,price:100},'
      + '{id:"s4",nom_id:"nom9",variant_id:"v8",variant_label:"Два",'
      + 'photo_url:"/api/nomenclature/photo.jpg?id=nom9",'
      + 'variant_photo_url:"",name:"Одиночка",qty:1,price:100}'
      + '];state.cart={};renderGrid();');
    if (/data-carpix=/.test(pv.ev('$("grid").innerHTML'))) {
      throw new Error('единственный кадр всё ещё крутит карусель');
    }
  });

  // 17. Жест отменяется, если палец уехал: это прокрутка витрины, а не просьба
  //     показать карточку.
  checkAsync('прокрутка витрины не открывает карточку', async () => {
    const pv = createKassa({ transport: 'stub' });
    pv.run();
    pv.ev('state.items=[{id:"s1",name:"Плёнка",qty:7,price:350}];');
    const tile = { getAttribute: () => 's1' };
    pv.ev('$("grid")._h.pointerdown').call(null, {
      target: { closest: () => tile }, clientX: 5, clientY: 5,
    });
    pv.ev('$("grid")._h.pointermove').call(null, { clientX: 5, clientY: 90 });
    await new Promise((r) => setTimeout(r, 700));
    if (pv.ev('!!previewBox')) throw new Error('прокрутка открыла карточку товара');
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

  // 17.0.26: витрина кассы — только стеллаж. Проверяем не сервер (он покрыт
  // юнит-тестами), а то, что кассир об этом узнаёт словами: без подсказки
  // человек ищет товар со склада и решает, что касса сломалась.
  checkAsync('витрина = стеллаж: касса говорит про склад и не показывает лишнее', async () => {
    const shelf = createKassa({
      transport: 'stub',
      respond: (url) => {
        if (String(url).indexOf('/api/cashier/catalog') < 0) return {};
        return { body: {
          items: [{ id: 's1', name: 'Адресник', price: 500, qty: 2, shelf_qty: 2,
                    stock_qty: 0, status: 'ok', unit: 'шт' }],
          categories: [], shelf_only: true, stock_positions: 3,
          sbp_enabled: false, sbp: {}, shop_cash: { in_shop: 1000 },
          shift_mode: 'auto', npd: {}, ring: false,
        } };
      },
    });
    shelf.run();
    await shelf.ev('loadCatalog()');
    const hint = String(shelf.ev('document.getElementById("subHint").textContent') || '');
    if (hint.indexOf('только стеллаж') < 0) {
      throw new Error('в подсказке нет режима «только стеллаж»: ' + hint);
    }
    if (hint.indexOf('3') < 0) {
      throw new Error('касса не сказала, сколько товаров осталось на складах: ' + hint);
    }
    if (shelf.ev('state.items.length') !== 1) {
      throw new Error('витрина показала не один товар со стеллажа');
    }

    // Режим выключен — подсказка про склад не появляется вовсе.
    const wide = createKassa({
      transport: 'stub',
      respond: (url) => {
        if (String(url).indexOf('/api/cashier/catalog') < 0) return {};
        return { body: {
          items: [{ id: 'stock:nom1', name: 'Органайзер', price: 700, qty: 5,
                    shelf_qty: 0, stock_qty: 5, status: 'ok', unit: 'шт' }],
          categories: [], shelf_only: false, stock_positions: 0,
          sbp_enabled: false, sbp: {}, shop_cash: { in_shop: 0 },
          shift_mode: 'auto', npd: {}, ring: false,
        } };
      },
    });
    wide.run();
    await wide.ev('loadCatalog()');
    const plain = String(wide.ev('document.getElementById("subHint").textContent') || '');
    if (plain.indexOf('только стеллаж') >= 0) {
      throw new Error('подсказка про стеллаж осталась при выключенном режиме');
    }
  });

  // 18.8 (срез 3): вкладка «Заказы» — выдача готовых заказов у прилавка.
  // Деньги идут только штатными маршрутами: наличные/долг/уже-оплачено —
  // /api/order/fulfill с payment_action, СБП — /api/sbp/create на точную
  // сумму остатка. Автовыдачи до прихода денег быть не должно.
  const hoOrder = (id, debt, extra) => Object.assign({
    id, number: '1042', product: 'Подставка', customer_name: 'Иван',
    phone: '+7 900 000-00-00', status: 'ready', price: 1200, paid: 300,
    prepaid: 0, due: '2026-09-20',
    economics: { price: 1200, paid: 300, debt },
  }, extra || {});
  const hoKassa = () => createKassa({
    transport: 'stub',
    respond: (url) => {
      const u = String(url);
      if (u.indexOf('/api/orders?status=ready') >= 0) {
        return { body: { orders: [hoOrder('ord1', 900)] } };
      }
      if (u.indexOf('/api/cashier/incoming') >= 0) return { body: { payments: [] } };
      if (u.indexOf('/api/order/fulfill') >= 0) return { body: { ok: true } };
      if (u.indexOf('/api/sbp/create') >= 0) return { body: { id: 'sbp1', number: '57' } };
      return {};
    },
  });
  // Клик по кнопке выдачи: делегирование вешено на #ordersBox, кнопка —
  // подделка с атрибутами и classList (withBusy крутит и то и другое).
  const hoTap = (kassa, act, oid = 'ord1', onum = '1042') => {
    // Страница зовёт голый confirm(): в браузере это глобальное, в vm-контексте
    // оно живёт только на window — подставляем то, что дал бы браузер.
    kassa.ev('if(typeof confirm==="undefined")confirm=window.confirm;');
    const attrs = { 'data-oact': act, 'data-oid': oid, 'data-onum': onum };
    const btn = {
      getAttribute: (n) => (n in attrs ? attrs[n] : null),
      classList: { contains: () => false, add() {}, remove() {} },
      disabled: false,
    };
    kassa.ev('document.getElementById("ordersBox")._h.click')
      .call(null, { target: { closest: (sel) => (sel === '[data-oact]' ? btn : null) } });
  };

  checkAsync('«Заказы»: готовый заказ с остатком, счётчик на вкладке', async () => {
    const k2 = hoKassa();
    k2.run();
    k2.ev('setTab("orders");');
    await settle();
    const html = String(k2.ev('document.getElementById("ordersBox").innerHTML') || '');
    if (html.indexOf('№1042') < 0) throw new Error('нет номера заказа: ' + html.slice(0, 120));
    if (html.indexOf('Иван') < 0 || html.indexOf('+7 900 000-00-00') < 0) {
      throw new Error('не видно клиента и телефон: ' + html.slice(0, 200));
    }
    if (html.indexOf('900 ₽') < 0) throw new Error('остаток не посчитан: ' + html.slice(0, 200));
    if (html.indexOf('data-oact="paid"') >= 0) {
      throw new Error('«Уже оплачено» показано при ненулевом остатке');
    }
    if (k2.ev('document.getElementById("tabOrdersLbl").textContent') !== 'Заказы · 1') {
      throw new Error('счётчика на вкладке нет: '
        + k2.ev('document.getElementById("tabOrdersLbl").textContent'));
    }
  });

  checkAsync('выдача наличными: fulfill с received/cash и подтверждением передачи', async () => {
    const k2 = hoKassa();
    k2.run();
    k2.ev('setTab("orders");');
    await settle();
    k2.ev('var _toasts=[];toast=function(t){_toasts.push(t);};');
    hoTap(k2, 'cash');
    await settle();
    const fills = k2.callsFor('/api/order/fulfill');
    if (fills.length !== 1) throw new Error(`вызовов fulfill ${fills.length}, ждали 1`);
    const body = JSON.parse(fills[0].body);
    if (body.id !== 'ord1' || body.handoff_confirmed !== true
        || body.payment_action !== 'received' || body.payment_method !== 'cash') {
      throw new Error('пейлоад fulfill не тот: ' + fills[0].body);
    }
    if (!/выдан · 900 ₽ в кассе/.test(k2.ev('_toasts').join('|'))) {
      throw new Error('кассир не услышал про деньги в кассе: ' + k2.ev('_toasts'));
    }
    if (k2.callsFor('/api/sbp/create').length) throw new Error('наличные создали СБП-платёж');
  });

  checkAsync('выдача СБП: платёж на точный остаток, без автовыдачи', async () => {
    const k2 = hoKassa();
    k2.run();
    k2.ev('setTab("orders");');
    await settle();
    k2.ev('var _toasts=[];toast=function(t){_toasts.push(t);};');
    hoTap(k2, 'sbp');
    await settle();
    const creates = k2.callsFor('/api/sbp/create');
    if (creates.length !== 1) throw new Error(`вызовов СБП ${creates.length}, ждали 1`);
    const body = JSON.parse(creates[0].body);
    if (body.amount !== 900 || body.order_id !== 'ord1') {
      throw new Error('платёж не на остаток заказа: ' + creates[0].body);
    }
    if (String(body.request_id).indexOf('cash-ho-ord1-') !== 0) {
      throw new Error('ключ запроса не привязан к заказу: ' + body.request_id);
    }
    if (k2.callsFor('/api/order/fulfill').length) {
      throw new Error('заказ выдан до прихода денег');
    }
    if (!/СБП-платёж №57 создан/.test(k2.ev('_toasts').join('|'))) {
      throw new Error('кассир не узнал о созданном платеже: ' + k2.ev('_toasts'));
    }
  });

  checkAsync('выдача в долг: fulfill с debt, денег не берём', async () => {
    const k2 = hoKassa();
    k2.run();
    k2.ev('setTab("orders");');
    await settle();
    hoTap(k2, 'debt');
    await settle();
    const fills = k2.callsFor('/api/order/fulfill');
    if (fills.length !== 1) throw new Error(`вызовов fulfill ${fills.length}, ждали 1`);
    const body = JSON.parse(fills[0].body);
    if (body.payment_action !== 'debt' || body.payment_method !== '') {
      throw new Error('долг должен идти payment_action=debt: ' + fills[0].body);
    }
    if (k2.callsFor('/api/sbp/create').length) throw new Error('долг создал платёж');
  });

  checkAsync('нулевой остаток: «Уже оплачено» и выдача без движения денег', async () => {
    const k2 = createKassa({
      transport: 'stub',
      respond: (url) => {
        const u = String(url);
        if (u.indexOf('/api/orders?status=ready') >= 0) {
          return { body: { orders: [hoOrder('ord2', 0, { id: 'ord2', number: '1043', paid: 1200 })] } };
        }
        if (u.indexOf('/api/order/fulfill') >= 0) return { body: { ok: true } };
        return {};
      },
    });
    k2.run();
    k2.ev('setTab("orders");');
    await settle();
    const html = String(k2.ev('document.getElementById("ordersBox").innerHTML') || '');
    if (html.indexOf('data-oact="paid"') < 0) {
      throw new Error('кнопки «Уже оплачено» нет: ' + html.slice(0, 200));
    }
    if (html.indexOf('data-oact="cash"') >= 0 || html.indexOf('data-oact="sbp"') >= 0) {
      throw new Error('оплаченному заказу всё ещё предлагают платить');
    }
    k2.ev('var _toasts=[];toast=function(t){_toasts.push(t);};');
    hoTap(k2, 'paid', 'ord2', '1043');
    await settle();
    const fills = k2.callsFor('/api/order/fulfill');
    if (fills.length !== 1) throw new Error(`вызовов fulfill ${fills.length}, ждали 1`);
    const body = JSON.parse(fills[0].body);
    if (body.id !== 'ord2' || body.payment_action !== 'none' || body.handoff_confirmed !== true) {
      throw new Error('выдача «уже оплачено» не та: ' + fills[0].body);
    }
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
