#!/usr/bin/env node
/**
 * Замер «касса ↔ ПК» на живом коннекторе без телефона.
 *
 * Отличие от scripts/kassa-drill.py: там серверная половина (руками по HTTP), а
 * здесь работает САМА страница кассы — тот же инлайн-скрипт, что открывается в
 * WebView, только в песочнице node и с настоящими сокетами. Поэтому видно то,
 * чего по HTTP не видно: сколько идёт «нажал Оплатить → можно продавать
 * следующему», что попадает в офлайн-очередь при обрыве в момент продажи и
 * переживает ли очередь смену адреса сервера.
 *
 * Стенд должен быть изолированным: скрипт создаёт продажи.
 *
 *   node scripts/kassa-lan.js --base http://127.0.0.1:8767 --code 1234
 *   node scripts/kassa-lan.js --json > /tmp/lan.json
 */
'use strict';
const { createKassa, startRottenProxy } = require('./kassa-harness');

const argv = process.argv.slice(2);
function opt(name, fallback) {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
}
const flag = (name) => argv.includes(name);

const BASE = opt('--base', 'http://127.0.0.1:8767').replace(/\/+$/, '');
const CODE = opt('--code', '1234');
const PROXY_PORT = Number(opt('--proxy-port', '8791'));
const JSON_OUT = flag('--json');

const results = [];
function report(label, value, note) {
  results.push({ label, value, note: note || '' });
  if (!JSON_OUT) {
    console.log(`  ${label.padEnd(46)} ${String(value).padStart(9)}   ${note || ''}`);
  }
}
const ms = (n) => `${Math.round(n)} мс`;

async function api(path, body) {
  const init = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : { method: 'GET' };
  const t0 = Date.now();
  const res = await fetch(BASE + path, init);
  const text = await res.text();
  let json = {};
  try { json = text ? JSON.parse(text) : {}; } catch (e) { json = { raw: text.slice(0, 200) }; }
  return { status: res.status, json, ms: Date.now() - t0, bytes: Buffer.byteLength(text, 'utf8') };
}

async function main() {
  if (!JSON_OUT) {
    console.log(`Стенд кассы на живом коннекторе: ${BASE}\n`);
    console.log('== 1. Канал и денежные маршруты (через страницу, теми же запросами) ==');
  }
  const health = await api('/api/health');
  if (health.status !== 200) {
    console.error(`Стенд ${BASE} не отвечает (${health.status}). Запустите коннектор и повторите.`);
    process.exit(2);
  }
  report('/api/health', `${health.json.version || '?'} · ${ms(health.ms)}`, `ответ ${health.bytes} Б`);

  // Копия очереди «в памяти оболочки» — как PfApp в приложении кассы.
  const stash = { value: '' };
  const bridge = {
    queueSave: (json) => { stash.value = json || ''; },
    queueLoad: () => stash.value,
  };
  const k = createKassa({ origin: BASE, transport: 'real', bridge });
  k.run();

  // Вход кассира — тем же кодом страницы (login() внутри неё).
  const login = await k.api('/api/cashier/login', { code: CODE });
  if (login.status !== 200 || !login.json.token) {
    console.error(`Вход кассира не удался: ${login.status} ${JSON.stringify(login.json).slice(0, 200)}`);
    console.error('Нужен стенд с кодом кассы (--code) и хотя бы одним товаром на полке.');
    process.exit(2);
  }
  k.ev(`state.token=${JSON.stringify(login.json.token)};state.name=${JSON.stringify(login.json.name || 'кассир')};`
    + `state.role=${JSON.stringify(login.json.role || 'employee')};`);
  report('POST /api/cashier/login', ms(login.ms), `роль ${login.json.role || '?'}`);

  const catalog = await k.api(`/api/cashier/catalog?token=${encodeURIComponent(login.json.token)}`);
  k.ev(`state._allCatalog=${JSON.stringify(catalog.json)};`
    + 'state._allItems=(state._allCatalog.items||[]);state.items=state._allItems.filter(function(i){return (Number(i.qty)||0)>0;});');
  const items = catalog.json.items || [];
  const inStock = items.filter((i) => (Number(i.qty) || 0) > 0);
  report('GET /api/cashier/catalog', ms(catalog.ms),
    `${items.length} поз. (${inStock.length} в наличии) · ${catalog.bytes} Б`);
  if (!items.length || !inStock.length) {
    const forced = opt('--item', '');
    if (!forced) {
      console.error('Продавать нечем: на полке кассы нет остатка. Пополните товар или укажите --item <id>.');
      process.exit(2);
    }
    k.ev(`state.items=(state._allItems||[]).filter(function(i){return i.id===${JSON.stringify(forced)};});`);
  }

  // Продажа «как кассир»: кладём товар в корзину и жмём Оплатить.
  const buy = async (kassa, label) => {
    const it = (kassa.ev('state.items') || [])[0];
    kassa.ev(`state.cart={${JSON.stringify(it.id)}:1};state.cartSeen={${JSON.stringify(it.id)}:${Number(it.price) || 0}};`
      + 'state.offline=false;state.saleRequest={cash:"",sbp:""};');
    const t0 = Date.now();
    kassa.ev('sellCart("cash")');
    // страница не возвращает промис продажи: ждём, пока корзина опустеет
    const done = await kassa.waitFor('!cartHasItems() || state.offline', 20000);
    const total = Date.now() - t0;
    if (!done) throw new Error('продажа не завершилась за 20 с');
    const sellCall = kassa.callsFor('/api/cashier/sell').pop() || {};
    if (sellCall.status && sellCall.status !== 200) {
      throw new Error(`сервер отказал в продаже: ${sellCall.status} ${String(sellCall.reply || '').slice(0, 160)}`);
    }
    const catalogCalls = kassa.callsFor('/api/cashier/catalog');
    report(label, ms(total), `сам запрос ${ms(sellCall.ms || 0)}, после продажи перечитан каталог `
      + `${catalogCalls.length ? ms(catalogCalls[catalogCalls.length - 1].ms) : 'нет'}`);
    return { total, sellCall };
  };
  await buy(k, 'Продажа: «Оплатить» → можно продавать дальше');

  const sessions1 = await api('/api/cashier/sessions');
  const mine = (sessions1.json.sessions || [])[0] || {};
  report('GET /api/cashier/sessions', ms(sessions1.ms),
    `касс на связи ${sessions1.json.online}/${sessions1.json.total}, молчит ${Math.round(mine.silent_seconds || 0)} с`);

  // ---------------------------------------------------------------- 2. обрыв
  if (!JSON_OUT) console.log('\n== 2. Обрыв ровно в момент продажи (запрос ушёл, ответ потерян) ==');
  const proxy = await startRottenProxy({ listen: PROXY_PORT, target: Number(BASE.split(':').pop()) });
  const before = k.callsFor('/api/cashier/sell').length;
  k.setOrigin(`http://127.0.0.1:${PROXY_PORT}`);
  k.ev('navigator.onLine=true;state.offline=false;');
  let it = (k.ev('state.items') || [])[0];
  k.ev(`state.cart={${JSON.stringify(it.id)}:1};state.cartSeen={${JSON.stringify(it.id)}:${Number(it.price) || 0}};`
    + 'state.saleRequest={cash:"",sbp:""};');
  const t0 = Date.now();
  k.ev('sellCart("cash")');
  await k.waitFor('state.offline', 20000);
  const failMs = Date.now() - t0;
  const sent = (k.callsFor('/api/cashier/sell')[before] || {});
  let sentId = '';
  try { sentId = JSON.parse(sent.body || '{}').request_id || ''; } catch (e) {}
  const queue = k.ev('offQueue') || [];
  const entry = queue[queue.length - 1] || {};
  report('обрыв: ошибка доехала до кассы', ms(failMs), sent.status ? `HTTP ${sent.status}` : 'сеть оборвалась');
  report('в очереди номер тот же, что ушёл', entry.id === sentId ? 'да' : 'НЕТ',
    `ушёл ${sentId || '—'} · в очереди ${entry.id || '—'}`);
  report('запись помечена «результат неизвестен»', entry.pending ? 'да' : 'нет');
  report('очередь лежит в памяти телефона',
    (k.storage.getItem('cashier_offline_q') || '').includes(entry.id || '—') ? 'да' : 'НЕТ');
  proxy.close();
  k.setOrigin(BASE);

  // --------------------------------------------- 3. смена адреса сервера (IP)
  if (!JSON_OUT) console.log('\n== 3. Роутер выдал ПК новый адрес: переживёт ли очередь ==');
  report('копия очереди в памяти оболочки', JSON.parse(stash.value || '[]').length,
    'столько продаж переживут смену адреса — копию страница пишет при каждом изменении');

  // Новый адрес = другой origin: у страницы своя (пустая) память, оболочка та же.
  const NEW_BASE = `http://127.0.0.1:${PROXY_PORT}`;
  const moved = createKassa({ origin: NEW_BASE, transport: 'real', bridge });
  moved.run();
  moved.ev(`state.token=${JSON.stringify(login.json.token)};`);
  const movedQueue = moved.ev('offQueue') || [];
  report('очередь на новом адресе с копией', movedQueue.length,
    movedQueue.length ? `номер ${movedQueue[0].id}` : 'пусто — очередь была бы потеряна');

  const naked = createKassa({ origin: NEW_BASE, transport: 'real' });
  naked.run();
  report('то же без копии в оболочке', (naked.ev('offQueue') || []).length,
    'так очередь исчезала до этой правки');

  const salesBefore = await api(`/api/cashier/shift/sales?token=${encodeURIComponent(login.json.token)}`);
  const rows = (salesBefore.json.sales || salesBefore.json.items || []);
  const known = rows.filter((r) => String(r.request_id || '') === sentId).length;
  report('на ПК продажа уже записана', known ? 'да' : 'пока нет',
    'ответ не доехал, но сервер мог её провести');

  // Связь вернулась на новом адресе: выгружаем очередь (тот же номер продажи).
  const movedBack = createKassa({ origin: BASE, transport: 'real', bridge });
  movedBack.run();
  movedBack.ev(`state.token=${JSON.stringify(login.json.token)};`);
  movedBack.ev('offFlush(true)');
  await movedBack.sleep(400);
  const salesAfter = await api(`/api/cashier/shift/sales?token=${encodeURIComponent(login.json.token)}`);
  const rowsAfter = (salesAfter.json.sales || salesAfter.json.items || []);
  const withId = rowsAfter.filter((r) => String(r.request_id || '') === sentId);
  report('строк с этим номером после выгрузки', withId.length,
    `строк было ${rows.length}, стало ${rowsAfter.length} — вторая продажа не появилась`);

  // ------------------------------------------------------------ 4. поток SSE
  if (!JSON_OUT) console.log('\n== 4. Живой поток событий (касса понимает, что связь есть) ==');
  const stream = await api('/api/health');
  const t1 = Date.now();
  let pingAt = 0;
  const frames = [];
  const es = new k.context.EventSource('/api/stream');
  k.context.window.addEventListener || 0;
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 66000);
    const orig = es._frame.bind(es);
    es._frame = (raw) => {
      orig(raw);
      const line = raw.split('\n')[0];
      if (!/^event:/.test(line)) return;          // retry: и : ping — не событие
      if (/^event: telemetry/.test(line)) return; // «я жив» раз в 15 с — не то, по чему касса решает
      frames.push({ at: Date.now() - t1, raw: line });
      if (frames.length >= 1 && /^event: ping/.test(line)) { clearTimeout(timer); resolve(); }
    };
  });
  es.close();
  pingAt = frames.length ? frames[frames.length - 1].at : 0;
  const pings = frames.filter((f) => /^event: ping/.test(f.raw));
  report('первый именованный ping в потоке', pings.length ? ms(pings[0].at) : 'нет за 66 с',
    pings.length ? 'касса видит, что поток жив, не дожидаясь платежа'
      : 'поток молчит — касса считала бы связь потерянной');
  void stream;

  if (JSON_OUT) {
    console.log(JSON.stringify({ base: BASE, at: new Date().toISOString(), results }, null, 2));
  } else {
    console.log('\nЗамер закончен. Стенд:', BASE);
  }
}

main().catch((e) => {
  console.error('Замер упал:', e && e.stack || e);
  process.exit(1);
});
