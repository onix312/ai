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
      ams: { trays: [{ id: '00', present: true, type: 'PLA' }, { id: '01', present: false }] },
      camera: { available: true },
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

  /* 4. Обрыв связи: плашка и свежесть */
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

  /* 5. Экраны и выбор принтера */
  {
    const page = runPage({});
    await wait(60);
    page.clickTile('prn1');
    page.clickTab('queue');
    check('экраны: выбор принтера и переключение вкладки не падают',
      page.store['pk_cmd_name'].textContent === 'Цех-1' && page.store['pk_screen_queue'].hidden === false,
      page.store['pk_cmd_name'].textContent);
    check('камера и AMS: кадр и счётчик слотов живые',
      page.store['pk_cam'].src.indexOf('/api/printer/camera.jpg?printer_id=prn1') === 0
      && page.store['pk_ams_now'].textContent.indexOf('1 из 2') > 0,
      page.store['pk_cam'].src + ' / ' + page.store['pk_ams_now'].textContent);
  }

  console.log(`\n${failed ? 'FAILED' : 'OK'}: стенд пульта — ${passed - failed < 0 ? 0 : passed} проверок пройдено`
    + (failed ? `, провалено ${failed}` : ''));
  process.exit(failed ? 1 : 0);
})();
