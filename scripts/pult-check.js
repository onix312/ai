#!/usr/bin/env node
/* Headless-стенд пульта цеха (18.0).
 *
 * Зачем: страница site/control.html — не только вёрстка, но и логика команд.
 * `node --check` видит синтаксис, а «пульт отправляет серверу ровно то, что
 * тот принимает» (confirmed, preflight, start_request_id) проверяется только
 * выполнением. Браузера в проекте нет, поэтому берём настоящий инлайн-скрипт
 * страницы, подставляем заглушку DOM и записываем все запросы.
 *
 * Первый прогон этого стенда сразу нашёл настоящую ошибку: в call() не было
 * resolve, и все GET-запросы висели до таймаута — страница показывала «связи
 * нет» при живом коннекторе (проверка «данные отрисовались» это ловит).
 *
 * Запуск: node scripts/pult-check.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const PAGE = path.join(ROOT, 'site', 'control.html');
const html = fs.readFileSync(PAGE, 'utf8');
const pageScript = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1])[0];
if (!pageScript || pageScript.indexOf('/api/state') < 0) {
  console.error('НЕ НАЙДЕН инлайн-скрипт страницы пульта в site/control.html');
  process.exit(1);
}

let failed = 0;
let passed = 0;
function check(label, condition, detail) {
  if (condition) {
    passed += 1;
    console.log(`  ok  ${label}`);
  } else {
    failed += 1;
    console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`);
  }
}

/* ------------------------------------------------------------- заглушка DOM */
function mkEl(id) {
  return {
    id, hidden: false, textContent: '', innerHTML: '', src: '', className: '', disabled: false,
    dataset: {}, style: {}, attrs: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    removeAttribute(name) { delete this.attrs[name]; },
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener() {},
    closest() { return null; },
  };
}

function buildPage() {
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
  const store = {};
  ids.forEach((id) => { store[id] = mkEl(id); });
  const tabs = ['park', 'queue', 'ams', 'cam'].map((name) => {
    const el = mkEl('tab_' + name);
    el.dataset.screen = name;
    return el;
  });
  let clickHandler = null;
  const document = {
    hidden: false,
    body: mkEl('body'),
    querySelector(sel) {
      const m = /^#([\w-]+)$/.exec(sel);
      return m ? (store[m[1]] || null) : null;
    },
    querySelectorAll(sel) { return sel === '.pk-tab' ? tabs : []; },
    addEventListener(type, fn) { if (type === 'click') clickHandler = fn; },
    getElementById(id) { return store[id] || null; },
    createElement() { return mkEl('new'); },
  };
  return { store, document, tabs, click: (event) => clickHandler(event) };
}

/* --------------------------------------------------------------- фикстуры */
function makeState(printerState, connected) {
  return {
    at: new Date().toISOString(),
    printers: [{
      id: 'prn1', name: 'Цех-1', model: 'P1S',
      connection: { connected, configured: true, mode: 'local', last_error: '' },
      printer: { state: printerState, state_label: 'Печатает', progress: 42, remaining_min: 65,
                 task: 'Подставка', speed_level: 2, eta: Math.floor(Date.now() / 1000) + 3900 },
      temperature: { nozzle: 215, bed: 60 },
      // Как отдаёт parse_ams_trays: слот, человеческая подпись, цвет, остаток.
      ams: { trays: [
        { id: '00', unit: 0, slot: 0, label: 'Слот 1', type: 'PETG', color: '#1F2937',
          remain: 64, uuid: 'ABC123', active: true, present: true },
        { id: '01', unit: 0, slot: 1, label: 'Слот 2', type: '', color: '#CBD5E1',
          remain: null, uuid: '', active: false, present: false },
      ] },
      camera: { available: true, age: 0.8, fps: 2, demo: false, shots: 4, error: '' },
      light: 'off',
      guard: { alerts: [] },
      job: { order: { number: '1042', product: 'Подставка' } },
      maintenance: {},
    }],
    queue: [
      { id: 'job1', name: 'Подставка', state: 'queued', plate: 1, est_minutes: 90, est_grams: 40,
        priority: 5, printer_id: '', file: 'stand.gcode.3mf' },
      { id: 'job2', name: 'Номерок', state: 'running', plate: 2, printer_id: 'prn1', file: 'tag.3mf' },
    ],
    farm: { total: 1, online: connected ? 1 : 0, printing: printerState === 'RUNNING' ? 1 : 0, queued: 1 },
  };
}

function runPage(options) {
  const opts = options || {};
  const page = buildPage();
  const requests = [];
  const prompts = [];
  const env = {
    state: opts.state || makeState('RUNNING', true),
    preflight: opts.preflight || { blocks: [], warns: [] },
    ams: opts.ams || { printer_id: 'prn1', printers: ['prn1'], stale_min: 30, slots: [] },
    spools: opts.spools || { spools: [] },
    shot: opts.shot || { ok: true, shot: { id: 'shot1', at: Date.now() / 1000, note: 'Снимок с пульта цеха' } },
    answers: opts.answers || {},
    failFetch: !!opts.failFetch,
  };
  const sandbox = {
    console,
    document: page.document,
    navigator: {},
    location: { origin: 'http://127.0.0.1:8790' },
    localStorage: { getItem() { return null; }, setItem() {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
    AbortController, Promise, Date, JSON, Math, String, Number, Object, Array, Boolean,
    isFinite, parseFloat, parseInt, encodeURIComponent,
    confirm(message) {
      prompts.push(message);
      const answers = Object.keys(env.answers);
      for (let i = 0; i < answers.length; i += 1) {
        if (message.indexOf(answers[i]) >= 0) return env.answers[answers[i]];
      }
      return true;
    },
    fetch(url, request) {
      const method = (request && request.method) || 'GET';
      const payload = request && request.body ? JSON.parse(request.body) : null;
      requests.push({ method, url, payload });
      if (env.failFetch) return Promise.reject(new Error('нет ответа'));
      if (url === '/api/state') {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.state) });
      }
      if (url.indexOf('/api/printer/preflight') === 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.preflight) });
      }
      if (url.indexOf('/api/ams/memory') === 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.ams) });
      }
      if (url === '/api/spools') {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.spools) });
      }
      if (url === '/api/printer/snapshot') {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.shot) });
      }
      if (opts.failUrl && url.indexOf(opts.failUrl) === 0) {
        return Promise.resolve({ ok: false, status: 400,
          json: () => Promise.resolve({ error: 'Проверка не прошла' }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(pageScript, sandbox, { filename: 'control.html:inline' });

  const target = (map) => ({ closest: (sel) => map[sel] || null });
  return {
    store: page.store,
    requests,
    prompts,
    posts: () => requests.filter((r) => r.method === 'POST'),
    clickParkCmd(cmd) {
      page.click({ target: target({ '[data-cmd]': { dataset: { cmd, on: 'prn1' }, disabled: false } }) });
    },
    clickJobAct(act, jobId) {
      const holder = { dataset: { job: jobId } };
      const button = { dataset: { jobAct: act }, disabled: false,
        closest: (sel) => (sel === '[data-job]' ? holder : null) };
      page.click({ target: { closest: (sel) => (sel === '[data-job-act]' ? button : null) } });
    },
    clickTile(printerId) {
      page.click({ target: { closest: (sel) => (sel === '[data-printer]'
        ? { dataset: { printer: printerId } } : null) } });
    },
    clickTab(screen) {
      page.click({ target: { closest: (sel) => (sel === '.pk-tab'
        ? { dataset: { screen }, classList: { toggle() {} } } : null) } });
    },
    clickAms(action, slot, spool) {
      page.click({ target: { closest: (sel) => (sel === '[data-ams]'
        ? { dataset: { ams: action, slot, spool }, disabled: false } : null) } });
    },
    clickId(id) {
      page.click({ target: { closest: (sel) => (sel === '#' + id ? { id } : null) } });
    },
  };
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const text = (html) => String(html).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

/* ------------------------------------------------------------------ прогон */
(async () => {
  console.log('Стенд пульта цеха: site/control.html (18.0)\n');

  /* 1. Парк: данные отрисовались и это видно словами */
  {
    const page = runPage({});
    await wait(60);
    const tiles = text(page.store['pk_tiles'].innerHTML);
    check('парк: карточка принтера отрисована', tiles.indexOf('Цех-1') >= 0, tiles.slice(0, 120));
    check('парк: состояние с сервера (state_label)', tiles.indexOf('Печатает') >= 0 && tiles.indexOf('42%') >= 0, tiles);
    check('парк: остаток времени и счётчик AMS', tiles.indexOf('осталось') >= 0 && tiles.indexOf('1/2') >= 0, tiles);
    check('парк: сводка «в сети 1 из 1»', page.store['pk_farm'].textContent.indexOf('в сети 1 из 1') >= 0,
      page.store['pk_farm'].textContent);
    check('сеть: пилюля «связь есть», плашка скрыта',
      page.store['pk_net'].textContent === 'связь есть'
      && page.store['pk_net'].className === 'pk-pill ok'
      && page.store['pk_offline'].hidden === true,
      page.store['pk_net'].textContent + '/' + page.store['pk_net'].className);
    check('свежесть: ISO-время разобрано (не «1970»/«2026»)', /обновлено в \d\d:\d\d/.test(page.store['pk_at'].textContent),
      page.store['pk_at'].textContent);
    const cards = (page.store['pk_jobs'].innerHTML.match(/data-job="/g) || []).length;
    check('очередь: карточки заданий и бейдж ожидающих',
      cards === 2 && page.store['pk_badge_queue'].textContent === '1'
      && page.store['pk_badge_queue'].hidden === false,
      'карточек ' + cards + ', бейдж ' + page.store['pk_badge_queue'].textContent);
    const queue = text(page.store['pk_jobs'].innerHTML);
    check('очередь: видно, что печатается и что ждёт',
      queue.indexOf('печатается на «Цех-1»') >= 0 && queue.indexOf('ждёт принтер') >= 0, queue);
    check('очередь: у ожидающего задания запуск/выше/ниже/отмена',
      page.store['pk_jobs'].innerHTML.indexOf('data-job-act="start"') >= 0
      && page.store['pk_jobs'].innerHTML.indexOf('data-job-act="cancel"') >= 0);
    check('пульт ходит только в свой коннектор',
      page.requests.every((r) => r.url.indexOf('/api/') === 0), JSON.stringify(page.requests.map((r) => r.url)));
  }

  /* 2. Команды печати: подтверждение и confirmed:true */
  {
    const page = runPage({});
    await wait(60);
    page.requests.length = 0;
    page.prompts.length = 0;
    page.clickParkCmd('pause');
    await wait(60);
    const body = page.posts()[0] && page.posts()[0].payload;
    check('пауза: POST /api/printer/command с confirmed:true',
      body && body.command === 'pause' && body.printer_id === 'prn1' && body.confirmed === true,
      JSON.stringify(body));
    check('пауза: оператора спрашивают', (page.prompts[0] || '').indexOf('Пауза?') === 0, page.prompts[0]);

    const paused = runPage({ state: makeState('PAUSE', true) });
    await wait(60);
    paused.requests.length = 0;
    paused.prompts.length = 0;
    paused.clickParkCmd('resume');
    await wait(60);
    const resumeBody = paused.posts()[0] && paused.posts()[0].payload;
    check('продолжение: уходит с confirmed:true и без лишнего вопроса',
      resumeBody && resumeBody.command === 'resume' && resumeBody.confirmed === true && paused.prompts.length === 0,
      JSON.stringify(resumeBody));

    const refusing = runPage({ answers: { 'Остановить печать?': false } });
    await wait(60);
    refusing.requests.length = 0;
    refusing.prompts.length = 0;
    refusing.clickParkCmd('stop');
    await wait(60);
    check('стоп: отказ оператора — ни одного запроса на принтер',
      refusing.posts().length === 0 && (refusing.prompts[0] || '').indexOf('Остановить печать?') === 0,
      JSON.stringify(refusing.prompts));

    const free = runPage({ state: makeState('IDLE', true) });
    await wait(60);
    await wait(20);
    check('свободный принтер: кнопки команд выключены',
      free.store['pk_b_pause'].disabled === true && free.store['pk_b_resume'].disabled === true
      && free.store['pk_b_stop'].disabled === true,
      JSON.stringify({ p: free.store['pk_b_pause'].disabled, r: free.store['pk_b_resume'].disabled }));

    const offline = runPage({ state: makeState('RUNNING', false) });
    await wait(60);
    check('нет связи с принтером: команды недоступны',
      offline.store['pk_b_pause'].disabled === true
      && String(offline.store['pk_cmd_hint'].textContent).indexOf('нет связи') >= 0,
      String(offline.store['pk_cmd_hint'].textContent));
  }

  /* 3. Запуск задания: preflight → подтверждение → старт */
  {
    const page = runPage({ preflight: { blocks: [], warns: [{ title: 'Пластик не тот' }] } });
    await wait(60);
    page.requests.length = 0;
    page.prompts.length = 0;
    page.clickJobAct('start', 'job1');
    await wait(80);
    const pre = page.requests.filter((r) => r.method === 'GET' && r.url.indexOf('/api/printer/preflight') === 0)[0];
    check('запуск: сначала preflight на выбранном принтере',
      !!pre && pre.url.indexOf('printer_id=prn1') > 0 && pre.url.indexOf('plate=1') > 0, pre && pre.url);
    const body = page.posts()[0] && page.posts()[0].payload;
    check('запуск: /api/jobs/start с подтверждением и идемпотентным ключом',
      !!body && body.id === 'job1' && body.confirmed === true
      && /^pult-job1-\d+$/.test(String(body.start_request_id || '')),
      JSON.stringify(body));
    check('запуск: предупреждения preflight подтверждены оператором',
      !!body && body.preflight_acknowledged === true, JSON.stringify(body));
    check('запуск: оператор видит вопрос с предупреждением',
      (page.prompts[0] || '').indexOf('Предупреждения') === 0 && (page.prompts[0] || '').indexOf('Пластик не тот') > 0,
      page.prompts[0]);
  }
  {
    const page = runPage({ preflight: { blocks: [{ title: 'Плита занята' }], warns: [] } });
    await wait(60);
    page.requests.length = 0;
    page.clickJobAct('start', 'job1');
    await wait(80);
    check('запуск: блокировка preflight останавливает старт',
      page.posts().filter((p) => p.url === '/api/jobs/start').length === 0
      && page.store['pk_msg'].textContent.indexOf('Плита занята') > 0,
      page.store['pk_msg'].textContent);
  }
  {
    const page = runPage({ state: makeState('IDLE', true) });
    await wait(60);
    page.requests.length = 0;
    page.clickJobAct('up', 'job1');
    await wait(60);
    page.clickJobAct('down', 'job1');
    await wait(60);
    page.clickJobAct('cancel', 'job1');
    await wait(60);
    const calls = page.posts().map((p) => p.url + ' ' + JSON.stringify(p.payload));
    check('очередь: перестановка и отмена идут своими маршрутами',
      calls.join(' | ') === [
        '/api/jobs/reorder {"id":"job1","direction":"up"}',
        '/api/jobs/reorder {"id":"job1","direction":"down"}',
        '/api/jobs/cancel {"id":"job1"}',
      ].join(' | '),
      calls.join(' | '));
  }
  {
    const page = runPage({ failUrl: '/api/jobs/start' });
    await wait(60);
    page.clickJobAct('start', 'job1');
    await wait(80);
    check('запуск: ошибка сервера показана оператору словами',
      page.store['pk_msg'].textContent.indexOf('Проверка не прошла') >= 0, page.store['pk_msg'].textContent);
  }

  /* 4. AMS: слоты из двух источников, привязка, «как в прошлый раз», забыть */
  const AMS_FIXTURE = {
    printer_id: 'prn1', stale_min: 30,
    slots: [
      { slot: '0', spool_id: 'spool-1', material: 'PETG', color_name: 'Чёрный',
        color_hex: '1F2937', grams_left: 640.0, remain_pct: 64.0, state: 'live',
        seen_at: new Date().toISOString(), stale: false, label: 'Слот 1' },
      { slot: '3', spool_id: '', material: 'PLA', color_name: 'Белый',
        color_hex: 'F8FAFC', grams_left: 0.0, remain_pct: 0.0, state: 'empty',
        seen_at: '2026-09-01T10:00:00+00:00', stale: true, label: 'Слот 4' },
    ],
  };
  {
    const page = runPage({ ams: AMS_FIXTURE });
    await wait(60);
    page.clickTab('ams');
    await wait(80);
    const slots = text(page.store['pk_slots'].innerHTML);
    check('AMS: слоты собраны и подписаны словами',
      slots.indexOf('Слот 1') >= 0 && slots.indexOf('PETG Чёрный') >= 0
      && slots.indexOf('64%') >= 0 && slots.indexOf('640 г') >= 0, slots.slice(0, 160));
    check('AMS: у пустого слота видно память и её возраст',
      slots.indexOf('Слот 4') >= 0 && slots.indexOf('память несвежая') >= 0, slots);
    check('AMS: у привязанного слота есть «Отвязать» и «Другая катушка»',
      page.store['pk_slots'].innerHTML.indexOf('data-ams="unbind"') >= 0
      && page.store['pk_slots'].innerHTML.indexOf('data-ams="pick"') >= 0);
    check('AMS: живой слот принтера и память сведены в одну карточку',
      page.store['pk_slots'].innerHTML.indexOf('живой слот') >= 0,
      page.store['pk_slots'].innerHTML.slice(0, 200));
  }
  {
    const page = runPage({ ams: AMS_FIXTURE, spools: { spools: [
      { id: 'spool-1', material: 'PETG', color_name: 'Чёрный', color_hex: '1F2937',
        remaining_grams: 640, archived: 0, printer_id: 'prn1', ams_slot: '0' },
      { id: 'spool-9', material: 'PLA', color_name: 'Синий', color_hex: '1D4ED8',
        remaining_grams: 320, archived: 0, printer_id: '', ams_slot: '' },
    ] } });
    await wait(60);
    page.clickTab('ams');
    await wait(80);
    page.clickAms('pick', '3');
    await wait(80);
    const sheet = text(page.store['pk_sheet_list'].innerHTML);
    check('AMS: шторка предлагает свободные катушки со склада',
      sheet.indexOf('PLA Синий') >= 0 && sheet.indexOf('320 г') >= 0
      && sheet.indexOf('на складе') >= 0, sheet.slice(0, 140));
    check('AMS: у слота с памятью-пластиком шторка предлагает привязку',
      page.store['pk_sheet_act'].innerHTML.indexOf('data-ams="unbind"') >= 0
      || page.store['pk_sheet_list'].innerHTML.indexOf('data-ams="bind"') >= 0,
      page.store['pk_sheet_act'].innerHTML.slice(0, 160));
    page.requests.length = 0;
    page.clickAms('bind', '3', 'spool-9');
    await wait(80);
    const body = page.posts()[0] && page.posts()[0].payload;
    check('AMS: привязка уходит с push_ams и подтверждением оператора',
      !!body && body.url === undefined && body.id === 'spool-9' && body.ams_slot === '3'
      && body.push_ams === true && body.confirmed === true && body.printer_id === 'prn1',
      JSON.stringify(body));
  }
  {
    // Принтер молчит, а память знает: слот пуст в телеметрии, катушка в базе —
    // вот тогда и нужна кнопка «как в прошлый раз».
    const silent = makeState('OFFLINE', false);
    silent.printers[0].ams = { trays: [] };
    const page = runPage({ state: silent, ams: AMS_FIXTURE, spools: { spools: [] } });
    await wait(60);
    page.clickTab('ams');
    await wait(80);
    const card = page.store['pk_slots'].innerHTML;
    check('AMS: у выключенного принтера видна память базы и «как в прошлый раз»',
      card.indexOf('data-ams="again"') >= 0 && card.indexOf('PETG Чёрный') >= 0,
      text(card).slice(0, 140));
    page.requests.length = 0;
    page.clickAms('again', '0');
    await wait(80);
    const body = page.posts()[0] && page.posts()[0].payload;
    check('AMS: «как в прошлый раз» привязывает ту же катушку',
      !!body && body.id === 'spool-1' && body.ams_slot === '0', JSON.stringify(body));
  }
  {
    const page = runPage({ ams: AMS_FIXTURE });
    await wait(60);
    page.clickTab('ams');
    await wait(80);
    page.requests.length = 0;
    page.prompts.length = 0;
    page.clickAms('forget', '0');
    await wait(80);
    const body = page.posts()[0] && page.posts()[0].payload;
    check('AMS: «Забыть» чистит только память слота и предупреждает об этом',
      !!body && body.slot === '0' && body.printer_id === 'prn1'
      && (page.prompts[0] || '').indexOf('Привязки катушек') > 0, JSON.stringify(body));
    page.requests.length = 0;
    page.clickAms('unbind', '0');
    await wait(80);
    const unbind = page.posts()[0] && page.posts()[0].payload;
    check('AMS: отвязка отправляет пустой слот (катушка на склад)',
      !!unbind && unbind.id === 'spool-1' && unbind.ams_slot === '', JSON.stringify(unbind));
  }
  {
    const page = runPage({ ams: AMS_FIXTURE });
    await wait(60);
    page.clickTab('ams');
    await wait(80);
    page.requests.length = 0;
    page.clickId('pk_ams_sync');
    await wait(80);
    check('AMS: «Синхронизировать» просит принтер сверить катушки',
      page.posts()[0] && page.posts()[0].url === '/api/printer/ams/sync'
      && page.posts()[0].payload.printer_id === 'prn1', JSON.stringify(page.posts()[0]));
  }
  {
    // Спор о слоте: сервер отвечает «уже занят» → пульт предлагает замену
    const page = runPage({ ams: AMS_FIXTURE, spools: { spools: [] },
      failUrl: '__none__' });
    await wait(60);
    page.clickTab('ams');
    await wait(80);
    page.clickAms('pick', '3');
    await wait(80);
    page.clickAms('bind', '3', 'spool-9');
    await wait(80);
    check('AMS: без свободных катушек шторка говорит, где взять пластик',
      text(page.store['pk_sheet_list'].innerHTML).indexOf('Свободных катушек') >= 0,
      text(page.store['pk_sheet_list'].innerHTML));
  }

  /* 5. Камера: кадр, снимок в журнал, свет */
  {
    const page = runPage({});
    await wait(60);
    page.clickTab('cam');
    await wait(80);
    check('камера: кадр запрошен у выбранного принтера',
      page.store['pk_cam'].src.indexOf('/api/printer/camera.jpg?printer_id=prn1') === 0,
      page.store['pk_cam'].src);
    check('камера: подпись честная (возраст кадра и число снимков)',
      String(page.store['pk_cam_sub'].textContent).indexOf('кадр с «Цех-1»') >= 0
      && String(page.store['pk_cam_note'].textContent).indexOf('снимков в журнале') >= 0,
      page.store['pk_cam_sub'].textContent + ' / ' + page.store['pk_cam_note'].textContent);
    page.requests.length = 0;
    page.clickId('pk_b_shot');
    await wait(80);
    const shot = page.posts()[0];
    check('камера: снимок уходит в журнал с пометкой',
      !!shot && shot.url === '/api/printer/snapshot'
      && shot.payload.printer_id === 'prn1' && shot.payload.note.indexOf('пульт') > 0,
      JSON.stringify(shot && shot.payload));
    page.requests.length = 0;
    page.clickId('pk_b_light');
    await wait(80);
    const light = page.posts()[0];
    check('камера: свет включается одной кнопкой (значение — от текущего состояния)',
      !!light && light.url === '/api/printer/command'
      && light.payload.command === 'light' && light.payload.value === true,
      JSON.stringify(light && light.payload));
  }
  {
    const offCam = makeState('IDLE', true);
    offCam.printers[0].camera = { available: false, error: 'Камера не отвечает', shots: 0, demo: false };
    const page = runPage({ state: offCam });
    await wait(60);
    page.clickTab('cam');
    await wait(80);
    check('камера: недоступный кадр объясняется словами, кнопка снимка выключена',
      page.store['pk_cam'].src === '' && page.store['pk_b_shot'].disabled === true
      && String(page.store['pk_cam_note'].textContent).indexOf('Камера не отвечает') >= 0,
      page.store['pk_cam_note'].textContent);
  }
  {
    const demoCam = makeState('RUNNING', true);
    demoCam.printers[0].camera = { available: true, demo: true, age: 1.2, shots: 3 };
    const page = runPage({ state: demoCam });
    await wait(60);
    page.clickTab('cam');
    await wait(80);
    check('камера: демо-режим помечен, а не выдаётся за цех',
      String(page.store['pk_cam_note'].textContent).indexOf('демо-режим') >= 0,
      page.store['pk_cam_note'].textContent);
  }

  /* 6. Обрыв связи: плашка и свежесть */
  {
    const page = runPage({ failFetch: true });
    await wait(60);
    check('обрыв: пилюля «связи нет» и плашка видна',
      page.store['pk_net'].textContent === 'связи нет' && page.store['pk_offline'].hidden === false,
      page.store['pk_net'].textContent);
    check('обрыв: текст плашки объясняет, что данные старые',
      page.store['pk_offline_text'].textContent.indexOf('Коннектор не отвечает') === 0,
      page.store['pk_offline_text'].textContent);
  }

  /* 7. Экраны и выбор принтера */
  {
    const page = runPage({});
    await wait(60);
    page.clickTile('prn1');
    page.clickTab('queue');
    check('экраны: выбор принтера и переключение вкладки не падают',
      page.store['pk_cmd_name'].textContent === 'Цех-1' && page.store['pk_screen_queue'].hidden === false,
      page.store['pk_cmd_name'].textContent);
    check('парк: счётчик AMS в карточке живой',
      text(page.store['pk_tiles'].innerHTML).indexOf('1/2') >= 0,
      text(page.store['pk_tiles'].innerHTML).slice(0, 120));
  }

  console.log(`\n${failed ? 'FAILED' : 'OK'}: стенд пульта — ${passed - failed < 0 ? 0 : passed} проверок пройдено`
    + (failed ? `, провалено ${failed}` : ''));
  process.exit(failed ? 1 : 0);
})();
