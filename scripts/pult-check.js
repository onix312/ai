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
  const el = {
    id, hidden: false, textContent: '', innerHTML: '', src: '', className: '', disabled: false,
    value: '', files: [], dataset: {}, style: {}, attrs: {}, handlers: {}, clicks: 0,
    classList: (() => {
      const set = new Set();
      return {
        add(...names) { names.forEach((n) => set.add(n)); },
        remove(...names) { names.forEach((n) => set.delete(n)); },
        toggle(name, on) { if (on === undefined) { set.has(name) ? set.delete(name) : set.add(name); }
                           else if (on) { set.add(name); } else { set.delete(name); } },
        contains(name) { return set.has(name); },
      };
    })(),
    removeAttribute(name) { delete this.attrs[name]; },
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(type, fn) { (el.handlers[type] = el.handlers[type] || []).push(fn); },
    // Кнопка выбора файла зовёт input.click() — считаем нажатия, чтобы проверка
    // «смена файла действительно открывает выбор» была не на словах.
    click() { el.clicks += 1; el.handlers.click && el.handlers.click.forEach((fn) => fn({})); },
    closest() { return null; },
  };
  return el;
}

function buildPage() {
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
  const store = {};
  ids.forEach((id) => { store[id] = mkEl(id); });
  const tabs = ['park', 'queue', 'auto', 'file', 'ams', 'cam'].map((name) => {
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
  return { store, document, tabs, click: (event) => clickHandler(event),
           windowHandlers: {} };
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
    // 18.0.8/18.0.10: сводка несёт отчёт автономности и факт последней печати.
    autonomy: {
      auto_queue: false, safety_gate: false, armed: false, quiet: false,
      reasons: ['Автозапуск выключен (auto_queue): задания запускает оператор.'],
      printers: [{ id: 'prn1', name: 'Цех-1', ready: false, job_id: 'job1',
                   reason: 'принтер занят: печатает', failed_streak: 0 }],
      next: { job: { id: 'job1', name: 'Подставка', plate: 2, est_minutes: 90,
                     est_grams: 40, priority: 5, due: '2026-09-20',
                     order: { number: '1042', product: 'Подставка' } },
              printer: { id: 'prn1', name: 'Цех-1' },
              why: ['приоритет 5 — выше остальных', 'срок 2026-09-20',
                    'материал PETG уже заправлен — без смены катушки'],
              ready: false, reason: 'принтер занят: печатает' },
      rules: ['Порядок: ручной приоритет → срок → кто раньше встал в очередь.',
              'Сначала задания, материал которых уже заправлен в AMS — меньше смен катушки.'],
      queue_waiting: 2,
    },
    last_done: { id: 'job7', name: 'Готовое', printer_id: 'prn1', plate: 2, state: 'done',
                 finished_at: '2026-09-14T07:10:00+00:00', plan_minutes: 96, plan_grams: 41.2,
                 minutes: 97, grams: 40.8, progress: 100, result: 'ok', idle_min: 35,
                 order: { number: '1042', product: 'Подставка' } },
  };
}

/* Форма из браузера: страница кладёт в неё файл и поля. Запоминаем имена
   полей — по ним видно, что уходило на сервер, а что нет. */
class FakeFormData {
  constructor() { this.entries = []; }
  append(name, value, filename) {
    this.entries.push({ name, value, filename });
  }
}

function fakeFile(name, size) {
  return { name, size: size == null ? 2048 : size };
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
    storage: opts.storage || {},
    failFetch: !!opts.failFetch,
    estimate: opts.estimate || {
      ok: true, file: 'stand.3mf', grams: 41.2, minutes: 96, hours: 1.6,
      material: 'PETG', color: '#1F2937',
      estimate: { minutes: 96, grams: 41.2, total_grams: 41.2, total_minutes: 96,
                  material: 'PETG', color: '#1F2937', plate_count: 1, plates: [] },
    },
    job: opts.job || { ok: true, file: 'stand.3mf', job: { id: 'job9', name: 'stand', est_grams: 41.2 } },
    plate: opts.plate || { ok: true, b64: 'iVBORw0KGgo=' },
    uploadStatus: opts.uploadStatus || 200,
    uploadError: opts.uploadError || '',
    uploadNetworkFail: !!opts.uploadNetworkFail,
    uploads: [],
    autoMap: opts.autoMap || null,
    // Превью разбора брака (GET /api/defect/recovery): список причин —
    // с сервера, и именно по нему страница наполняет селект.
    defect: opts.defect || {
      reasons: {
        detached: 'Деталь отклеилась', clog: 'Засор сопла', shift: 'Смещение слоёв',
        runout: 'Закончился пластик', warp: 'Деформация',
        quality: 'Не прошло контроль качества', support: 'Ошибка поддержек',
        wrong_material: 'Неверный материал', power: 'Сбой питания/связи',
        other: 'Другое',
      },
      reason: '', can_reprint: true, blockers: [], repeat_risk: false,
      already_recorded: false,
    },
  };
  const sandbox = {
    console,
    document: page.document,
    navigator: {},
    FormData: FakeFormData,
    XMLHttpRequest: class {
      constructor() {
        const self = this;
        this.upload = { addEventListener(type, fn) { if (type === 'progress') self._progress = fn; } };
        this.status = 0;
        this.responseText = '';
      }
      open(method, url) { this.method = method; this.url = url; }
      setRequestHeader() {}
      send(form) {
        const body = {};
        (form.entries || []).forEach((item) => { body[item.name] = item.value; });
        this._body = body;
        env.uploads.push({ method: this.method, url: this.url, body, form: form.entries || [] });
        if (this.upload && this._progress) {
          this._progress({ lengthComputable: true, loaded: 512, total: 2048 });
        }
        if (env.uploadNetworkFail) { this.onerror && this.onerror(); return; }
        if (env.uploadStatus >= 400) {
          this.status = env.uploadStatus;
          this.responseText = JSON.stringify({ error: env.uploadError || 'Проверка не прошла' });
          this.onload && this.onload();
          return;
        }
        this.status = 200;
        let answer = env.estimate;
        if (String(this.url || '').indexOf('/api/jobs/upload') >= 0) {
          answer = Object.assign({}, env.job);
          const job = Object.assign({}, env.job.job);
          if (body.plate) job.plate = Number(body.plate);
          if (body.printer_id) job.printer_id = body.printer_id;
          answer.job = job;
        }
        this.responseText = JSON.stringify(answer);
        this.onload && this.onload();
      }
      abort() { this.onabort && this.onabort(); }
    },
    location: { origin: 'http://127.0.0.1:8790', search: opts.search || '' },
    localStorage: {
      getItem(key) { return Object.prototype.hasOwnProperty.call(env.storage, key) ? env.storage[key] : null; },
      setItem(key, value) { env.storage[key] = String(value); },
    },
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
      if (url.indexOf('/api/pult/summary') === 0) {
        if (opts.noSummary) {
          return Promise.resolve({ ok: false, status: 404,
            json: () => Promise.resolve({ error: 'Неизвестный маршрут' }) });
        }
        // Сводка 18.0.4 = снимок парка + память слотов + катушки склада.
        const summary = Object.assign({}, env.state, {
          ams: env.ams, spools: env.spools.spools,
        });
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(summary) });
      }
      if (url === '/api/state') {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.state) });
      }
      if (url.indexOf('/api/printer/preflight') === 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.preflight) });
      }
      if (url === '/api/printer/ams/auto-map') {
        // Как отвечает сервер (`auto_ams_map`): слот на материал и живые треи.
        // По умолчанию считаем по фикстуре парка — PETG в слоте 1.
        const trays = ((env.state.printers[0] || {}).ams || {}).trays || [];
        const answer = env.autoMap || {
          mapping: trays.length ? [Number(trays[0].slot || 0)] : [-1],
          trays, required: (payload && payload.required) || [],
        };
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(answer) });
      }
      if (url.indexOf('/api/ams/memory') === 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.ams) });
      }
      if (url.indexOf('/api/defect/recovery') === 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(env.defect) });
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
  // Страница вешает обработчики и на окно (перетаскивание файла в любое место).
  sandbox.addEventListener = (type, fn) => {
    page.windowHandlers[type] = (page.windowHandlers[type] || []).concat(fn);
  };
  vm.createContext(sandbox);
  vm.runInContext(pageScript, sandbox, { filename: 'control.html:inline' });

  const target = (map) => ({ closest: (sel) => map[sel] || null });
  return {
    store: page.store,
    requests,
    storage: env.storage,
    prompts,
    uploads: env.uploads,
    env,
    // Нажать кнопку и бросить файл — так же, как это делает браузер.
    fire(id, type, event) {
      const el = page.store[id];
      const fns = (el && el.handlers[type]) || [];
      fns.forEach((fn) => fn(event || {}));
    },
    fireWindow(type, event) {
      (page.windowHandlers[type] || []).forEach((fn) => fn(event || {}));
    },
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
    check('пульт ходит только в свой коннектор и одним запросом',
      page.requests.length === 1 && page.requests[0].url.indexOf('/api/pult/summary') === 0,
      JSON.stringify(page.requests.map((r) => r.url)));
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

  /* 6.1 Сводка одним запросом (18.0.4) */
  {
    const page = runPage({ ams: AMS_FIXTURE, spools: { spools: [] } });
    await wait(60);
    check('сводка: обновление парка — один запрос вместо четырёх',
      page.requests.length === 1 && page.requests[0].url.indexOf('/api/pult/summary') === 0,
      JSON.stringify(page.requests.map((r) => r.url)));

    page.requests.length = 0;
    page.clickTab('ams');
    await wait(80);
    check('сводка: экран AMS не ходит за памятью и складом отдельно',
      page.requests.every((r) => r.url.indexOf('/api/pult/summary') === 0),
      JSON.stringify(page.requests.map((r) => r.url)));
    check('сводка: катушки склада приехали вместе со сводкой (шторка без запроса)',
      page.store['pk_slots'].innerHTML.indexOf('data-ams=') > 0,
      text(page.store['pk_slots'].innerHTML).slice(0, 120));

    page.requests.length = 0;
    page.clickAms('pick', '0');
    await wait(80);
    check('сводка: шторка выбора катушки открылась без запроса к складу',
      page.requests.length === 0, JSON.stringify(page.requests.map((r) => r.url)));
  }
  {
    // Старый коннектор (сводки нет): пульт обязан работать, а не показывать пустоту
    const page = runPage({ noSummary: true, ams: AMS_FIXTURE, spools: { spools: [] } });
    await wait(80);
    const urls = page.requests.map((r) => r.url);
    const park = text(page.store['pk_tiles'].innerHTML);
    check('откат: при 404 сводки пульт читает /api/state и рисует парк',
      urls.indexOf('/api/state') >= 0 && park.indexOf('Цех-1') >= 0, JSON.stringify(urls));
    check('откат: память слотов берётся отдельным адресом',
      urls.some((u) => u.indexOf('/api/ams/memory') === 0), JSON.stringify(urls));
  }

  /* 6.2 Ярлык приложения открывает нужный экран (манифест, 18.0.4) */
  {
    const page = runPage({ search: '?screen=queue' });
    await wait(60);
    check('ярлык: ?screen=queue открывает очередь сразу',
      page.store['pk_screen_queue'].hidden === false
      && page.store['pk_screen_park'].hidden === true,
      JSON.stringify({ queue: page.store['pk_screen_queue'].hidden,
                       park: page.store['pk_screen_park'].hidden }));

    const junk = runPage({ search: '?screen=admin' });
    await wait(60);
    check('ярлык: чужой ?screen= игнорируется — открывается парк',
      junk.store['pk_screen_park'].hidden === false
      && junk.store['pk_screen_queue'].hidden === true,
      JSON.stringify({ park: junk.store['pk_screen_park'].hidden }));
  }

  /* 6.3 Офлайн-память (18.0.5): последняя удачная сводка переживает перезагрузку */
  {
    // Успешный такт кладёт сводку в память телефона…
    const live = runPage({});
    await wait(60);
    const saved = live.storage['pult_last'];
    check('офлайн: удачная сводка запомнена в телефоне', !!saved,
      JSON.stringify(Object.keys(live.storage)));
    let parsed = null;
    try { parsed = JSON.parse(saved); } catch (e) {}
    check('офлайн: в памяти лежит время снимка и сам снимок',
      !!parsed && typeof parsed.saved === 'number' && Array.isArray(parsed.data.printers),
      String(saved).slice(0, 80));

    // …а следующий запуск без связи показывает её, а не пустоту
    const offline = runPage({ failFetch: true, storage: { pult_last: saved } });
    await wait(80);
    const park = offline.store['pk_tiles'].innerHTML || '';
    check('офлайн: без сети плитки рисуются из памяти', park.indexOf('data-printer=') >= 0,
      park.slice(0, 90));
    check('офлайн: на плашке честное «связи нет» и адрес без ответа',
      offline.store['pk_net'].textContent === 'связи нет'
      && offline.store['pk_offline'].hidden === false,
      JSON.stringify({ net: offline.store['pk_net'].textContent,
                       off: offline.store['pk_offline'].hidden }));
    check('офлайн: в тексте плашки видно, что это последние данные',
      String(offline.store['pk_offline_text'].textContent).indexOf('последние данные') >= 0,
      String(offline.store['pk_offline_text'].textContent));
    check('офлайн: возраст снимка — из памяти, а не «данных ещё нет»',
      String(offline.store['pk_at'].textContent).indexOf('обновлено') >= 0,
      String(offline.store['pk_at'].textContent));
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

  /* 8. Экран «Файл»: загрузка как в слайсере (18.0.6) */
  {
    const page = runPage({});
    await wait(60);
    page.clickTab('file');
    check('файл: вкладка открывает свой экран, а не остаётся на парке',
      page.store['pk_screen_file'].hidden === false
      && page.store['pk_screen_park'].hidden === true,
      JSON.stringify({ file: page.store['pk_screen_file'].hidden,
                       park: page.store['pk_screen_park'].hidden }));

    page.fire('pk_b_pick', 'click');
    check('файл: «Выбрать файл» открывает выбор файла (input.click)',
      page.store['pk_file_input'].clicks === 1, String(page.store['pk_file_input'].clicks));

    page.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    page.fire('pk_file_input', 'change');
    await wait(60);
    const up = page.uploads[0];
    check('файл: выбранный 3MF уходит на оценку в /api/estimate/upload',
      !!up && up.method === 'POST' && up.url === '/api/estimate/upload'
      && up.body.file && up.body.file.name === 'stand.3mf',
      JSON.stringify(up && up.url));
    const card = text(page.store['pk_est'].innerHTML);
    check('файл: оценка показана как в панели — минуты, граммы, материал, цвет',
      card.indexOf('1 ч 36 мин') >= 0 && card.indexOf('41,2 г') >= 0
      && card.indexOf('PETG') >= 0 && card.indexOf('#1F2937') >= 0, card.slice(0, 200));
    check('файл: превью плиты спрошено у коннектора по имени файла',
      page.requests.some((r) => r.url.indexOf('/api/jobs/plate?name=stand.3mf') === 0),
      JSON.stringify(page.requests.map((r) => r.url)));
    check('файл: кнопка отправки называет то, что уйдёт в очередь',
      text(page.store['pk_est'].innerHTML).indexOf('В очередь · 41,2 г · 1 ч 36 мин') >= 0,
      text(page.store['pk_est'].innerHTML).slice(-120));
    check('файл: видно, на какой принтер встанет задание (выбранный на пульте)',
      text(page.store['pk_est'].innerHTML).indexOf('Печатать на: Цех-1') >= 0
      && page.store['pk_est'].innerHTML.indexOf('data-printer=""') >= 0,
      text(page.store['pk_est'].innerHTML).slice(0, 220));

    page.requests.length = 0;
    page.prompts.length = 0;
    page.fire('pk_est', 'click', { target: { closest: () => ({ id: 'pk_b_queue' }) } });
    await wait(60);
    const send = page.uploads[1];
    check('файл: отправка идёт в /api/jobs/upload с плитой и выбранным принтером',
      !!send && send.url === '/api/jobs/upload' && Number(send.body.plate) === 1
      && send.body.printer_id === 'prn1',
      JSON.stringify(send && send.body));
    check('файл: плита и принтер уходят одним запросом — файл не льётся дважды',
      page.uploads.length === 2, String(page.uploads.length));

    // Оператору нужен и обратный случай: оставить выбор очереди.
    const any = runPage({});
    await wait(60);
    any.clickTab('file');
    any.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    any.fire('pk_file_input', 'change');
    await wait(60);
    any.fire('pk_est', 'click', { target: { closest: () => ({ dataset: { printer: '' },
      className: 'pk-chip', id: '' }) } });
    await wait(20);
    const anyCard = text(any.store['pk_est'].innerHTML);
    check('файл: чипс «любой принтер» подписывает карточку честно',
      anyCard.indexOf('решит очередь') >= 0, anyCard.slice(0, 200));
    any.fire('pk_est', 'click', { target: { closest: () => ({ id: 'pk_b_queue' }) } });
    await wait(60);
    const free = any.uploads[1];
    check('файл: «любой принтер» оставляет выбор очереди (принтер не пришпилен)',
      !!free && free.body.printer_id === undefined,
      JSON.stringify(free && free.body));
    check('файл: после отправки карточка очищена и сказано про очередь',
      text(page.store['pk_est'].innerHTML) === ''
      && String(page.store['pk_msg'].textContent).indexOf('В очереди') >= 0,
      JSON.stringify({ card: text(page.store['pk_est'].innerHTML),
                       msg: page.store['pk_msg'].textContent }));
  }

  /* 8.1 Плиты: оператор печатает ту, что выбрал */
  {
    const plates = {
      ok: true, file: 'box.3mf', saved: 'box.3mf',
      estimate: {
        total_grams: 100, total_minutes: 200, material: 'PLA', color: '#111111',
        plate_count: 2,
        plates: [
          { plate_index: 1, grams: 40, minutes: 80, filaments: [{ type: 'PLA', color: '#111111' }] },
          { plate_index: 2, grams: 60, minutes: 120, filaments: [{ type: 'PETG', color: '#00A0FF' }] },
        ],
      },
    };
    const page = runPage({ estimate: plates });
    await wait(60);
    page.clickTab('file');
    page.store['pk_file_input'].files = [fakeFile('box.3mf')];
    page.fire('pk_file_input', 'change');
    await wait(60);
    check('плиты: чипсы нарисованы по числу плит в файле',
      (page.store['pk_est'].innerHTML.match(/data-plate=/g) || []).length === 2,
      page.store['pk_est'].innerHTML.slice(0, 160));
    check('плиты: по умолчанию показана первая плита, а не сумма',
      text(page.store['pk_est'].innerHTML).indexOf('40,0 г') >= 0
      && text(page.store['pk_est'].innerHTML).indexOf('100,0 г') < 0,
      text(page.store['pk_est'].innerHTML).slice(0, 200));
    page.fire('pk_plates', 'click', { target: { closest: () => ({ dataset: { plate: '2' } }) } });
    await wait(20);
    const card = text(page.store['pk_est'].innerHTML);
    check('плиты: выбор второй плиты меняет цифры на её собственные',
      card.indexOf('60,0 г') >= 0 && card.indexOf('2 ч') >= 0 && card.indexOf('PETG') >= 0, card.slice(0, 220));
    page.prompts.length = 0;
    page.fire('pk_est', 'click', { target: { closest: () => ({ id: 'pk_b_queue' }) } });
    await wait(60);
    const send = page.uploads[1];
    check('плиты: в очередь уходит выбранная плита и оператора спрашивают',
      !!send && Number(send.body.plate) === 2
      && (page.prompts[0] || '').indexOf('плиту 2') >= 0,
      JSON.stringify({ plate: send && send.body.plate, ask: page.prompts[0] }));
  }

  /* 8.1.1 Материал и цвет: у реального 3MF их даёт плита, а не сводка.
     Живая проба показала: у slice_info верхнего уровня material/color пустые,
     а в плитах — заполнены. Оператор обязан увидеть PETG и цвет, а не «—». */
  {
    const fromPlates = {
      ok: true, file: 'box.3mf', saved: 'box.3mf',
      estimate: {
        total_grams: 64.6, total_minutes: 176.4, plate_count: 2, material: '', color: '',
        plates: [
          { plate_index: 1, grams: 23.4, minutes: 80.4,
            filaments: [{ type: 'PETG', color: '#1F2937' }] },
          { plate_index: 2, grams: 41.2, minutes: 96,
            filaments: [{ type: 'PETG', color: '#1F2937' }] },
        ],
      },
    };
    const page = runPage({ estimate: fromPlates });
    await wait(60);
    page.clickTab('file');
    page.store['pk_file_input'].files = [fakeFile('box.3mf')];
    page.fire('pk_file_input', 'change');
    await wait(60);
    const card = text(page.store['pk_est'].innerHTML);
    check('материал и цвет: берутся из плиты, если в сводке файла их нет',
      card.indexOf('PETG') >= 0 && card.indexOf('#1F2937') >= 0, card.slice(0, 220));
  }

  /* 8.2 Честность: нет данных слайсера — говорим словами, цифр не выдумываем */
  {
    const empty = { ok: true, file: 'raw.3mf', saved: 'raw.3mf', estimate: {} };
    const page = runPage({ estimate: empty });
    await wait(60);
    page.clickTab('file');
    page.store['pk_file_input'].files = [fakeFile('raw.3mf')];
    page.fire('pk_file_input', 'change');
    await wait(60);
    const card = text(page.store['pk_est'].innerHTML);
    check('честность: пустая оценка — прочерки, а не нули',
      card.indexOf('—') >= 0 && card.indexOf('0,0 г') < 0, card.slice(0, 200));
    check('честность: сказано, почему оценки нет и что делать',
      card.indexOf('нет данных слайсера') >= 0 && card.indexOf('впишите') < 0
      && card.indexOf('вписать') >= 0, card.slice(-260));
    check('честность: в очередь всё равно можно — кнопка без выдуманных граммов',
      text(page.store['pk_est'].innerHTML).indexOf('В очередь на печать') >= 0,
      text(page.store['pk_est'].innerHTML).slice(-120));
  }

  /* 8.3 Отказы: чужой файл, слишком большой, ошибка сервера, обрыв сети */
  {
    const bad = runPage({});
    await wait(60);
    bad.clickTab('file');
    bad.store['pk_file_input'].files = [fakeFile('model.stl')];
    bad.fire('pk_file_input', 'change');
    await wait(60);
    check('отказы: .stl отклоняем сами — в сеть ничего не уходит',
      bad.uploads.length === 0 && String(bad.store['pk_msg'].textContent).indexOf('3MF') >= 0,
      JSON.stringify({ up: bad.uploads.length, msg: bad.store['pk_msg'].textContent }));

    const big = runPage({});
    await wait(60);
    big.clickTab('file');
    big.store['pk_file_input'].files = [fakeFile('huge.3mf', 401 * 1024 * 1024)];
    big.fire('pk_file_input', 'change');
    await wait(60);
    check('отказы: файл больше 400 МБ не отправляем и говорим почему',
      big.uploads.length === 0 && String(big.store['pk_msg'].textContent).indexOf('400 МБ') >= 0,
      String(big.store['pk_msg'].textContent));

    const refused = runPage({ uploadStatus: 400, uploadError: 'Поддерживаются только 3MF и G-code' });
    await wait(60);
    refused.clickTab('file');
    refused.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    refused.fire('pk_file_input', 'change');
    await wait(60);
    check('отказы: отказ сервера показан его же словами',
      String(refused.store['pk_msg'].textContent).indexOf('Поддерживаются только 3MF') >= 0,
      String(refused.store['pk_msg'].textContent));

    const dead = runPage({ uploadNetworkFail: true });
    await wait(60);
    dead.clickTab('file');
    dead.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    dead.fire('pk_file_input', 'change');
    await wait(60);
    check('отказы: обрыв сети — «сеть недоступна», страница не падает',
      String(dead.store['pk_msg'].textContent).indexOf('сеть недоступна') >= 0,
      String(dead.store['pk_msg'].textContent));
  }

  /* 8.4 Перетаскивание: файл в любое место окна и в зону загрузки */
  {
    const page = runPage({});
    await wait(60);
    page.fireWindow('drop', { preventDefault() {},
      dataTransfer: { files: [fakeFile('dropped.3mf')] } });
    await wait(60);
    check('перетаскивание: файл в окно открывает экран «Файл» и считается',
      page.store['pk_screen_file'].hidden === false && page.uploads.length === 1
      && page.uploads[0].body.file.name === 'dropped.3mf',
      JSON.stringify({ screen: page.store['pk_screen_file'].hidden, up: page.uploads.length }));

    const zone = runPage({});
    await wait(60);
    zone.clickTab('file');
    zone.fire('pk_drop', 'dragover', { preventDefault() {} });
    check('перетаскивание: над зоной загрузки загорается подсветка',
      zone.store['pk_drop'].classList.contains('pk-over') === true,
      String(zone.store['pk_drop'].classList.contains('pk-over')));
    zone.fire('pk_drop', 'drop', { preventDefault() {},
      dataTransfer: { files: [fakeFile('zone.3mf')] } });
    await wait(60);
    check('перетаскивание: файл, брошенный в зону, тоже уходит на оценку',
      zone.uploads.length === 1 && zone.uploads[0].body.file.name === 'zone.3mf',
      JSON.stringify(zone.uploads.map((u) => u.body.file && u.body.file.name)));
    check('перетаскивание: после броска подсветка гаснет',
      zone.store['pk_drop'].classList.contains('pk-over') === false,
      String(zone.store['pk_drop'].classList.contains('pk-over')));
  }

  /* 9. Раскладка AMS и запуск (18.0.7) */
  {
    const page = runPage({});
    await wait(60);
    page.clickTab('file');
    page.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    page.fire('pk_file_input', 'change');
    await wait(80);
    const auto = page.posts().filter((r) => r.url === '/api/printer/ams/auto-map')[0];
    check('раскладка: материалы файла уходят на авто-мап сервера',
      !!auto && auto.payload.printer_id === 'prn1'
      && (auto.payload.required || [])[0]
      && String(auto.payload.required[0].type).toUpperCase() === 'PETG',
      JSON.stringify(auto && auto.payload));
    const card = text(page.store['pk_est'].innerHTML);
    check('раскладка: на карточке видно «материал → слот» и выбранный слот',
      card.indexOf('Материалы файла → слоты AMS') >= 0
      && page.store['pk_est'].innerHTML.indexOf('data-slot=\"0\"') >= 0
      && /слот 1 · PETG/.test(page.store['pk_est'].innerHTML), card.slice(0, 260));
    check('раскладка: назван итог раскладки словами',
      String(page.store['pk_msg'].textContent).indexOf('AMS: слоты 1') >= 0,
      String(page.store['pk_msg'].textContent));

    // Отправка: раскладка обязана уехать вместе с заданием.
    page.requests.length = 0;
    page.fire('pk_est', 'click', { target: { closest: () => ({ id: 'pk_b_queue' }) } });
    await wait(80);
    const send = page.uploads[1];
    check('раскладка: с заданием уходит ams_mapping «0»',
      !!send && send.body.ams_mapping === '0', JSON.stringify(send && send.body));

    // Оператор против раскладки: «не из AMS» — поле не отправляем вовсе.
    const free = runPage({});
    await wait(60);
    free.clickTab('file');
    free.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    free.fire('pk_file_input', 'change');
    await wait(80);
    free.fire('pk_est', 'click', { target: { closest: () => ({ dataset: { fil: '0', slot: '-1' },
      className: 'pk-chip' }) } });
    await wait(20);
    check('раскладка: «не из AMS» видно словами, слот не выдуман',
      text(free.store['pk_est'].innerHTML).indexOf('ни один слот не назначен') >= 0,
      text(free.store['pk_est'].innerHTML).slice(0, 200));
    free.requests.length = 0;
    free.fire('pk_est', 'click', { target: { closest: () => ({ id: 'pk_b_queue' }) } });
    await wait(80);
    const noMap = free.uploads[1];
    check('раскладка: без назначенных слотов поле не отправляем — решает принтер',
      !!noMap && noMap.body.ams_mapping === undefined, JSON.stringify(noMap && noMap.body));

    // AMS не видно: честная надпись вместо пустых чипсов.
    const noAms = runPage({ autoMap: { mapping: [-1], trays: [], required: [] } });
    await wait(60);
    noAms.clickTab('file');
    noAms.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    noAms.fire('pk_file_input', 'change');
    await wait(80);
    check('раскладка: без AMS сказано, что слоты выберет принтер, и чипсов нет',
      text(noAms.store['pk_est'].innerHTML).indexOf('AMS на этом принтере не видно') >= 0
      && noAms.store['pk_est'].innerHTML.indexOf('data-slot=') < 0,
      text(noAms.store['pk_est'].innerHTML).slice(0, 200));
  }

  /* 9.1 «Отправить и запустить»: Preflight с раскладкой, подтверждение, старт */
  {
    const page = runPage({});
    await wait(60);
    page.clickTab('file');
    page.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    page.fire('pk_file_input', 'change');
    await wait(80);
    check('запуск: на выбранном принтере кнопка есть',
      page.store['pk_est'].innerHTML.indexOf('id=\"pk_b_start\"') >= 0,
      page.store['pk_est'].innerHTML.slice(-200));

    page.requests.length = 0;
    page.prompts.length = 0;
    page.fire('pk_est', 'click', { target: { closest: () => ({ id: 'pk_b_start' }) } });
    await wait(120);
    const pre = page.requests.filter((r) => r.url.indexOf('/api/printer/preflight') === 0)[0];
    check('запуск: Preflight получает раскладку AMS из карточки',
      !!pre && pre.url.indexOf('printer_id=prn1') > 0 && pre.url.indexOf('plate=1') > 0
      && decodeURIComponent(pre.url).indexOf('mapping=[0]') > 0, pre && pre.url);
    const start = page.posts().filter((r) => r.url === '/api/jobs/start')[0];
    check('запуск: старт с подтверждением и идемпотентным ключом',
      !!start && start.payload.confirmed === true
      && /^pult-job9-\d+$/.test(String(start.payload.start_request_id || '')),
      JSON.stringify(start && start.payload));
    check('запуск: файл ушёл вместе с плитой, принтером и раскладкой',
      page.uploads.length === 2 && page.uploads[1].body.ams_mapping === '0'
      && Number(page.uploads[1].body.plate) === 1
      && page.uploads[1].body.printer_id === 'prn1',
      JSON.stringify(page.uploads.map((u) => u.body)));

    // Отказ оператора: старта нет.
    const refuse = runPage({ answers: { 'Запустить': false } });
    await wait(60);
    refuse.clickTab('file');
    refuse.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    refuse.fire('pk_file_input', 'change');
    await wait(80);
    refuse.requests.length = 0;
    refuse.fire('pk_est', 'click', { target: { closest: () => ({ id: 'pk_b_start' }) } });
    await wait(120);
    check('запуск: отказ оператора — принтер не тревожим',
      refuse.posts().filter((r) => r.url === '/api/jobs/start').length === 0,
      JSON.stringify(refuse.posts().map((r) => r.url)));

    // Блокировка Preflight: стоп и объяснение.
    const blocked = runPage({ preflight: { blocks: [{ title: 'Пластик не тот' }], warns: [] } });
    await wait(60);
    blocked.clickTab('file');
    blocked.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    blocked.fire('pk_file_input', 'change');
    await wait(80);
    blocked.requests.length = 0;
    blocked.fire('pk_est', 'click', { target: { closest: () => ({ id: 'pk_b_start' }) } });
    await wait(120);
    check('запуск: блокировка Preflight — стоп и причина словами',
      blocked.posts().filter((r) => r.url === '/api/jobs/start').length === 0
      && String(blocked.store['pk_msg'].textContent).indexOf('Пластик не тот') >= 0,
      String(blocked.store['pk_msg'].textContent));

    // «Любой принтер»: запускать не на чем — кнопки нет.
    const any = runPage({});
    await wait(60);
    any.clickTab('file');
    any.store['pk_file_input'].files = [fakeFile('stand.3mf')];
    any.fire('pk_file_input', 'change');
    await wait(80);
    any.fire('pk_est', 'click', { target: { closest: () => ({ dataset: { printer: '' },
      className: 'pk-chip', id: '' }) } });
    await wait(40);
    check('запуск: на «любом принтере» кнопки запуска нет (печатать не на чем)',
      any.store['pk_est'].innerHTML.indexOf('id=\"pk_b_start\"') < 0,
      any.store['pk_est'].innerHTML.slice(-160));
  }

  /* 9.2 Запуск из очереди знает раскладку задания */
  {
    const page = runPage({});
    await wait(60);
    page.requests.length = 0;
    page.clickJobAct('start', 'job1');
    await wait(100);
    const pre = page.requests.filter((r) => r.url.indexOf('/api/printer/preflight') === 0)[0];
    check('очередь: Preflight получает раскладку из задания (не теряет её)',
      !!pre && pre.url.indexOf('mapping=') > 0, pre && pre.url);
  }

  /* 10. Экран «Авто»: почему очередь идёт сама или стоит (18.0.8) */
  {
    const page = runPage({});
    await wait(60);
    page.clickTab('auto');
    await wait(80);
    const chips = text(page.store['pk_auto_state'].innerHTML);
    const body = text(page.store['pk_auto_body'].innerHTML);
    check('авто: состояние автозапуска, допуска и тихих часов — чипсами',
      chips.indexOf('Автозапуск: выключен') >= 0 && chips.indexOf('Без присмотра: запрещён') >= 0
      && chips.indexOf('Тихие часы: нет') >= 0, chips);
    check('авто: «почему стоит» — словами сервера, а не догадкой страницы',
      body.indexOf('Автозапуск выключен (auto_queue)') >= 0, body.slice(0, 160));
    check('авто: следующее задание с планом и принтером',
      body.indexOf('Следующее') >= 0 && body.indexOf('Подставка') >= 0
      && body.indexOf('плита 2') >= 0 && body.indexOf('Цех-1') >= 0, body.slice(0, 200));
    check('авто: объяснение выбора — теми же словами, что у сервера',
      body.indexOf('приоритет 5') >= 0 && body.indexOf('материал PETG уже заправлен') >= 0,
      body.slice(0, 260));
    check('авто: причина ожидания видна («принтер занят»)',
      body.indexOf('принтер занят: печатает') >= 0, body.slice(0, 260));
    check('авто: правила очереди показаны оператору',
      body.indexOf('Правила очереди') >= 0 && body.indexOf('ручной приоритет') >= 0,
      body.slice(-220));
    check('авто: экран ничего не запускает и не пишет',
      page.posts().length === 0, JSON.stringify(page.posts().map((r) => r.url)));

    // Включённый автозапуск: пилюля «идёт сама», запуска по-прежнему нет.
    const armed = makeState('IDLE', true);
    armed.autonomy.auto_queue = true;
    armed.autonomy.safety_gate = true;
    armed.autonomy.armed = true;
    armed.autonomy.reasons = [];
    armed.autonomy.next.ready = true;
    armed.autonomy.next.reason = '';
    armed.autonomy.printers[0].ready = true;
    armed.autonomy.printers[0].reason = 'готов к запуску';
    const on = runPage({ state: armed });
    await wait(60);
    on.clickTab('auto');
    await wait(80);
    const onBody = text(on.store['pk_auto_body'].innerHTML);
    check('авто: включённые флаги читаются как «очередь идёт сама»',
      on.store['pk_auto_now'].textContent === 'очередь идёт сама'
      && text(on.store['pk_auto_state'].innerHTML).indexOf('Автозапуск: включён') >= 0,
      on.store['pk_auto_now'].textContent);
    check('авто: готовое задание показано как «очередь запустит сама»',
      onBody.indexOf('Готово к запуску') >= 0 && onBody.indexOf('очередь запустит сама') >= 0,
      onBody.slice(0, 200));
    check('авто: включённый автозапуск не превращается в кнопку на странице',
      on.posts().length === 0 && on.store['pk_auto_body'].innerHTML.indexOf('data-cmd') < 0,
      JSON.stringify(on.posts().map((r) => r.url)));

    // Предохранитель 18.0.9: два сбоя подряд встали поперёк автозапуска.
    const streaked = makeState('IDLE', true);
    streaked.autonomy.printers[0] = {
      id: 'prn1', name: 'Цех-1', ready: false, job_id: 'job1', failed_streak: 2,
      reason: '2 сорванные печати подряд — автозапуск встал и ждёт ручного запуска' };
    streaked.autonomy.next.ready = false;
    streaked.autonomy.next.reason = '2 сорванные печати подряд — автозапуск встал и ждёт ручного запуска';
    const streakedPage = runPage({ state: streaked });
    await wait(60);
    streakedPage.clickTab('auto');
    await wait(80);
    const streakedBody = text(streakedPage.store['pk_auto_body'].innerHTML);
    check('предохранитель: причина остановки автозапуска видна на экране «Авто»',
      streakedBody.indexOf('сорванные печати подряд') >= 0
      && streakedBody.indexOf('сорванных печатей подряд: 2') >= 0, streakedBody.slice(0, 240));
    check('предохранитель: на плитке парка сказано, что автозапуск встал',
      text(streakedPage.store['pk_tiles'].innerHTML).indexOf('Автозапуск встал') >= 0
      && text(streakedPage.store['pk_tiles'].innerHTML).indexOf('вручную') >= 0,
      text(streakedPage.store['pk_tiles'].innerHTML).slice(0, 200));
    check('предохранитель: страница и здесь ничего не запускает сама',
      streakedPage.posts().length === 0,
      JSON.stringify(streakedPage.posts().map((r) => r.url)));

    // Парка нет, а очередь не пуста: не «заданий нет», а «печатать не на чем».
    const noPark = makeState('IDLE', true);
    noPark.autonomy.printers = [];
    noPark.autonomy.next = {};
    const parkless = runPage({ state: noPark });
    await wait(60);
    parkless.clickTab('auto');
    await wait(80);
    const parklessBody = text(parkless.store['pk_auto_body'].innerHTML);
    check('авто: пустой парк и полная очередь — «печатать не на чем», а не «заданий нет»',
      parklessBody.indexOf('печатать не на чем') >= 0
      && parklessBody.indexOf('2 задания') >= 0, parklessBody.slice(0, 200));

    // Старый коннектор: блока нет — честное «данных нет», без выдумок.
    const legacy = makeState('IDLE', true);
    delete legacy.autonomy;
    const oldPage = runPage({ state: legacy });
    await wait(60);
    oldPage.clickTab('auto');
    await wait(80);
    check('авто: без отчёта в сводке экран говорит «данных нет», а не рисует нули',
      text(oldPage.store['pk_auto_body'].innerHTML).indexOf('Данных нет') >= 0,
      text(oldPage.store['pk_auto_body'].innerHTML).slice(0, 120));
  }

  /* 11. «Факт против плана» после финиша (18.0.10) */
  {
    const page = runPage({});
    await wait(60);
    const fact = text(page.store['pk_fact'].innerHTML);
    check('факт: план и факт рядом — время и пластик из сводки, без выдумок',
      fact.indexOf('обещал слайсер') >= 0 && fact.indexOf('1 ч 36 мин') >= 0
      && fact.indexOf('печатал по факту') >= 0 && fact.indexOf('1 ч 37 мин') >= 0
      && fact.indexOf('41,2 г') >= 0 && fact.indexOf('40,8 г') >= 0, fact.slice(0, 240));
    check('факт: расхождение с планом названо словами и не спрятано',
      fact.indexOf('Факт отличается от плана на 0,4 г') >= 0, fact.slice(0, 260));
    check('факт: простой с момента финиша посчитан сервером',
      fact.indexOf('Принтер стоит 35 мин') >= 0, fact.slice(0, 300));
    check('факт: кнопка «Снял детали» есть и ведёт в проверенный маршрут',
      page.store['pk_fact'].innerHTML.indexOf('id="pk_b_removed"') >= 0,
      page.store['pk_fact'].innerHTML.slice(-160));

    page.requests.length = 0;
    page.clickId('pk_b_removed');
    await wait(100);
    const posts = page.posts();
    check('факт: «Снял детали» — POST с этим принтером и подтверждением в тосте',
      posts.length === 1 && posts[0].url === '/api/printer/part-removed'
      && posts[0].payload.printer_id === 'prn1'
      && String(page.store['pk_msg'].textContent).indexOf('деталь снята') >= 0,
      JSON.stringify(posts.map((r) => [r.url, r.payload])));

    // Печатей не было: карточки нет, а не нули «0 г / 0 мин».
    const empty = makeState('IDLE', true);
    empty.last_done = {};
    const clean = runPage({ state: empty });
    await wait(60);
    check('факт: без завершённых печатей карточка не показывается',
      text(clean.store['pk_fact'].innerHTML) === '', text(clean.store['pk_fact'].innerHTML));

    // Выбран другой принтер: чужой финиш на его экране не висит.
    const other = makeState('IDLE', true);
    const secondTile = JSON.parse(JSON.stringify(other.printers[0]));
    secondTile.id = 'prn2';
    secondTile.name = 'Цех-2';
    other.printers = [secondTile, other.printers[0]];   // prn2 выбирается первым
    other.farm.total = 2;
    const second = runPage({ state: other });
    await wait(60);
    check('факт: карточка показывается только для выбранного принтера',
      text(second.store['pk_fact'].innerHTML) === '',
      text(second.store['pk_fact'].innerHTML).slice(0, 120));
  }

  /* 12. Финал заказа у станка (18.8): приёмка и брак на карточке «Печать закончена» */
  {
    const state = makeState('IDLE', true);
    state.last_done = Object.assign({}, state.last_done, {
      order: { id: 'ord1', number: '1042', product: 'Подставка', status: 'post' },
    });
    const page = runPage({ state });
    await wait(60);
    const fact = text(page.store['pk_fact'].innerHTML);
    check('финал: заказ на карточке — номер и изделие, а не незримая строка',
      fact.indexOf('Заказ №1042') >= 0 && fact.indexOf('Подставка') >= 0, fact.slice(0, 300));
    check('финал: «Готов к выдаче» и «Брак» рядом с «Снял детали»',
      page.store['pk_fact'].innerHTML.indexOf('id="pk_b_accept"') >= 0
      && page.store['pk_fact'].innerHTML.indexOf('id="pk_b_defect"') >= 0
      && page.store['pk_fact'].innerHTML.indexOf('id="pk_b_removed"') >= 0,
      page.store['pk_fact'].innerHTML.slice(-300));

    page.requests.length = 0;
    page.clickId('pk_b_accept');
    await wait(120);
    const acceptPosts = page.posts().filter((r) => r.url === '/api/order/accept');
    check('финал: приёмка уходит в /api/order/accept с quality_confirmed',
      acceptPosts.length === 1 && acceptPosts[0].payload.id === 'ord1'
      && acceptPosts[0].payload.quality_confirmed === true,
      JSON.stringify(acceptPosts));

    // Заказ уже «Готов»: приёмку не предлагаем, вместо неё пометка.
    const readyState = makeState('IDLE', true);
    readyState.last_done = Object.assign({}, readyState.last_done, {
      order: { id: 'ord1', number: '1042', product: 'Подставка', status: 'ready' },
    });
    const readyPage = runPage({ state: readyState });
    await wait(60);
    const readyFact = text(readyPage.store['pk_fact'].innerHTML);
    check('финал: готовому заказу не предлагают приёмку повторно',
      readyPage.store['pk_fact'].innerHTML.indexOf('id="pk_b_accept"') < 0
      && readyFact.indexOf('готов к выдаче') >= 0, readyFact.slice(0, 300));
    check('финал: брак доступен и у готового заказа',
      readyPage.store['pk_fact'].innerHTML.indexOf('id="pk_b_defect"') >= 0,
      readyPage.store['pk_fact'].innerHTML.slice(-200));

    // Брак: форма открывается, причины — с сервера, запись — с request_id.
    page.requests.length = 0;
    page.clickId('pk_b_defect');
    await wait(120);
    const recoveryGets = page.requests.filter((r) => r.url.indexOf('/api/defect/recovery') === 0);
    check('финал: список причин брака берётся с сервера по id задания',
      recoveryGets.length === 1 && recoveryGets[0].url.indexOf('job_id=job7') >= 0
      && page.store['pk_defect_wrap'].hidden === false,
      JSON.stringify(recoveryGets));
    check('финал: селект наполнен серверными причинами',
      page.store['pk_defect_reason'].innerHTML.indexOf('value="quality"') >= 0
      && page.store['pk_defect_reason'].innerHTML.indexOf('Не прошло контроль качества') >= 0,
      page.store['pk_defect_reason'].innerHTML.slice(0, 160));
    page.store['pk_defect_reason'].value = 'quality';
    page.store['pk_defect_note'].value = 'поправить профиль';
    page.clickId('pk_b_defect_ok');
    await wait(120);
    const defectPosts = page.posts().filter((r) => r.url === '/api/defect/recover');
    check('финал: запись брака подтверждена, с request_id, без повторной печати',
      defectPosts.length === 1 && defectPosts[0].payload.defect_confirmed === true
      && defectPosts[0].payload.reason === 'quality'
      && defectPosts[0].payload.job_id === 'job7'
      && String(defectPosts[0].payload.request_id).indexOf('pult-job7-') === 0
      && defectPosts[0].payload.reprint_confirmed === false,
      JSON.stringify(defectPosts));
    check('финал: после записи форма закрывается',
      page.store['pk_defect_wrap'].hidden === true);

    // Печать без заказа: приёмки нет, но брак остаётся (брак — про задание).
    const noOrder = makeState('IDLE', true);
    noOrder.last_done = Object.assign({}, noOrder.last_done, { order: null });
    const noOrderPage = runPage({ state: noOrder });
    await wait(60);
    const noOrderHtml = noOrderPage.store['pk_fact'].innerHTML;
    check('финал: печать без заказа — без приёмки, с браком',
      noOrderHtml.indexOf('id="pk_b_accept"') < 0
      && noOrderHtml.indexOf('id="pk_b_defect"') >= 0,
      noOrderHtml.slice(-240));

    // Брак уже записан по заданию: повторный разбор не предлагаем.
    const doneDefect = makeState('IDLE', true);
    doneDefect.last_done = Object.assign({}, doneDefect.last_done, {
      order: { id: 'ord1', number: '1042', product: 'Подставка', status: 'post' },
      defect_reason: 'warp', defect_title: 'Деформация',
    });
    const defectPage = runPage({ state: doneDefect });
    await wait(60);
    const defectHtml = defectPage.store['pk_fact'].innerHTML;
    check('финал: записанный брак виден, повторного разбора и приёмки нет',
      defectHtml.indexOf('Брак записан: Деформация') >= 0
      && defectHtml.indexOf('id="pk_b_defect"') < 0
      && defectHtml.indexOf('id="pk_b_accept"') < 0,
      defectHtml.slice(-300));
  }

  console.log(`\n${failed ? 'FAILED' : 'OK'}: стенд пульта — ${passed - failed < 0 ? 0 : passed} проверок пройдено`
    + (failed ? `, провалено ${failed}` : ''));
  process.exit(failed ? 1 : 0);
})();
