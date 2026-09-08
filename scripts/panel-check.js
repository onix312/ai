#!/usr/bin/env node
/* PrintFlow 14.0 — headless-стенд панели (идея 94, находки Б1/Б2).

   Зачем. `node --check` проверяет только синтаксис: файл с вызовом
   необъявленной переменной проходит его на «отлично», а в браузере падает
   в первом же обработчике. Так в панель уехали `debounce is not defined`
   (marketing.js) и `fail is not defined` (ops10.js) — разделы при этом
   выглядели целыми, но не работали.

   Что делает стенд. Грузит все скрипты `site/assets/*.js` в порядке
   `index.html` (ленивые — после остальных, как это и происходит при входе
   в раздел) в песочнице `vm` с заглушкой DOM, затем:
     1. fires DOMContentLoaded/load;
     2. переключает все разделы через PF.go;
     3. рассылает события ядра (ready/data/live/bootstrap/finance/…);
     4. дёргает каждый обработчик, который модули навесили на элементы.

   Блокирующим считается только ReferenceError — обращение к
   необъявленной переменной. Это детерминированный признак «модуль забыл
   импортировать хелпер», он не зависит от полноты заглушки DOM.
   Остальные ошибки печатаются в режиме --verbose как справочные.

   Запуск:  node scripts/panel-check.js [--verbose]
   Код возврата: 0 — чисто, 2 — найдены необъявленные переменные. */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const SITE = path.join(ROOT, 'site');
const ASSETS = path.join(SITE, 'assets');
const VERBOSE = process.argv.includes('--verbose');

/* ============================================================ заглушка DOM
   Цель — не «настоящий DOM», а достаточно терпеливый, чтобы модули
   отработали свои bind()/render() до конца. Любой неизвестный метод
   возвращает цепочку, любой неизвестный элемент — новый стаб. */
const ELEMENTS = new Map();
const CHAIN = { _c: {} };
CHAIN.get = (prop) => {
  if (!(prop in CHAIN._c)) CHAIN._c[prop] = () => CHAIN._c;
  return CHAIN._c[prop];
};

function makeElement(key) {
  const base = {
    id: key || '', tagName: 'DIV', nodeName: 'DIV', nodeType: 1,
    style: { setProperty(){}, getPropertyValue(){return ''}, removeProperty(){}, getPropertyPriority(){return ''} }, dataset: {}, value: '', textContent: '', innerHTML: '',
    hidden: false, checked: false, open: false, disabled: false, href: '', src: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [], childNodes: [], files: [], options: [], length: 0,
    parentNode: null, parentElement: null, firstChild: null, lastChild: null,
    nextSibling: null, previousSibling: null,
    offsetWidth: 800, offsetHeight: 600, clientWidth: 800, clientHeight: 600,
    scrollTop: 0, scrollLeft: 0, scrollHeight: 0, readyState: 'complete',
    _listeners: [],
    appendChild(child) { return child || makeElement(); },
    removeChild() {}, insertBefore(child) { return child || makeElement(); },
    remove() {}, replaceChildren() {}, append() {}, prepend() {}, before() {}, after() {},
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {}, hasAttribute() { return false; },
    addEventListener(type, fn) { base._listeners.push([type, fn]); },
    removeEventListener() {},
    focus() {}, blur() {}, click() {}, select() {}, showModal() {}, close() {}, show() {},
    querySelector() { return makeElement(); }, querySelectorAll() { return []; },
    closest() { return null; }, matches() { return false; }, contains() { return false; },
    getBoundingClientRect() {
      return { left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, x: 0, y: 0 };
    },
    getClientRects() { return []; },
    scrollIntoView() {}, scrollTo() {},
    animate() { return { finished: Promise.resolve(), cancel() {}, onfinish: null }; },
    cloneNode() { return makeElement(key); },
    getContext() { return null; },
    play() { return Promise.resolve(); }, pause() {},
  };
  base.parentElement = base; base.parentNode = base; base.firstChild = base; base.lastChild = base;
  return new Proxy(base, {
    get(target, prop) { return prop in target ? target[prop] : CHAIN.get(prop); },
    set(target, prop, value) { target[prop] = value; return true; },
  });
}
const element = (key) => {
  if (!ELEMENTS.has(key)) ELEMENTS.set(key, makeElement(key));
  return ELEMENTS.get(key);
};

const storage = {};
const localStorage = {
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: (k) => { delete storage[k]; },
  clear: () => { Object.keys(storage).forEach((k) => delete storage[k]); },
};

const document = { _h: {} };
const problems = [];
let rafCalls = 0;

const win = {
  localStorage, sessionStorage: localStorage, console,
  location: { hash: '', href: 'http://localhost:8080/', origin: 'http://localhost:8080',
    search: '', pathname: '/', reload() {} },
  history: { scrollRestoration: '', pushState() {}, replaceState() {}, back() {} },
  navigator: { userAgent: 'printflow-panel-check', language: 'ru-RU', onLine: true,
    clipboard: { writeText: () => Promise.resolve() } },
  matchMedia: () => ({ matches: false, media: '', addEventListener() {}, removeEventListener() {},
    addListener() {}, removeListener() {} }),
  addEventListener(type, fn) {
    win._listeners = win._listeners || {};
    win._listeners[type] = (win._listeners[type] || []).concat(fn);
  },
  removeEventListener() {},
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  requestAnimationFrame: (fn) => {
    rafCalls++;
    if (rafCalls > 400) return 0;
    return setTimeout(() => {
      try { fn(Date.now()); } catch (e) { problems.push(['rAF', e]); }
    }, 0);
  },
  cancelAnimationFrame: clearTimeout,
  fetch: () => Promise.resolve({ ok: true, status: 200, headers: { get: () => null },
    json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
  URL, URLSearchParams, TextEncoder, TextDecoder,
  Blob: class {}, File: class {}, FormData: class { append() {} },
  FileReader: class { readAsDataURL() {} readAsText() {} readAsArrayBuffer() {} },
  Worker: class { postMessage() {} terminate() {} addEventListener() {} },
  Image: class { constructor() { this.style = {}; } },
  Audio: class { play() { return Promise.resolve(); } pause() {} },
  performance: { now: () => Date.now() },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  scrollTo() {}, scrollBy() {}, open() { return null; }, print() {},
  Notification: function () {}, isSecureContext: true, devicePixelRatio: 1,
  innerWidth: 1440, innerHeight: 900, screen: { width: 1440, height: 900 },
  EventSource: undefined,
  MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
  ResizeObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { observe() {} disconnect() {} unobserve() {} },
  alert() {}, confirm() { return true; }, prompt() { return ''; },
  crypto: { randomUUID: () => 'panel-check', getRandomValues: (a) => a },
};
win.window = win; win.self = win; win.globalThis = win; win.top = win; win.parent = win;
win.CustomEvent = class CustomEvent {
  constructor(type, options) {
    this.type = type;
    this.detail = (options || {}).detail;
    this.bubbles = !!(options || {}).bubbles;
  }
};
win.Event = win.CustomEvent;
win.KeyboardEvent = win.CustomEvent;
win.MouseEvent = win.CustomEvent;
win.EventTarget = class {
  constructor() { this._handlers = {}; }
  addEventListener(type, fn) { (this._handlers[type] = this._handlers[type] || []).push(fn); }
  removeEventListener() {}
  dispatchEvent(event) {
    (this._handlers[event.type] || []).forEach((fn) => {
      try { fn(event); } catch (e) { problems.push([event.type || '?', e]); }
    });
    return true;
  }
};

Object.assign(document, {
  documentElement: element('html'), head: element('head'), body: element('body'),
  readyState: 'loading', title: '', activeElement: element('body'),
  scrollingElement: element('html'),
  getElementById: (id) => element(id),
  createElement: (tag) => makeElement('new:' + tag),
  createElementNS: (ns, tag) => makeElement('ns:' + tag),
  createTextNode: () => makeElement('text'),
  createDocumentFragment: () => makeElement('frag'),
  querySelector: (selector) => element('q:' + selector),
  querySelectorAll: () => [],
  addEventListener(type, fn) { (document._h[type] = document._h[type] || []).push(fn); },
  removeEventListener() {},
});
win.document = new Proxy(document, {
  get(target, prop) { return prop in target ? target[prop] : CHAIN.get(prop); },
  set(target, prop, value) { target[prop] = value; return true; },
});

process.on('unhandledRejection', (reason) => {
  problems.push(['unhandledRejection', reason instanceof Error ? reason : new Error(String(reason))]);
});

/* ========================================================== что грузим */
function scriptOrder() {
  const html = fs.readFileSync(path.join(SITE, 'index.html'), 'utf8');
  const eager = [];
  const pattern = /<script src="assets\/([^"?]+)(?:\?[^"]*)?"><\/script>/g;
  let match;
  while ((match = pattern.exec(html)) !== null) eager.push(match[1]);
  const all = fs.readdirSync(ASSETS).filter((name) => name.endsWith('.js'));
  const lazy = all.filter((name) => !eager.includes(name));
  return { eager, lazy, all };
}

const ctx = vm.createContext(win);
const { eager, lazy, all } = scriptOrder();

function loadFile(name) {
  const source = fs.readFileSync(path.join(ASSETS, name), 'utf8');
  try {
    vm.runInContext(source, ctx, { filename: 'assets/' + name });
    return null;
  } catch (e) {
    return { file: name, error: e };
  }
}

const loadErrors = [];
eager.forEach((name) => { const err = loadFile(name); if (err) loadErrors.push(err); });

/* DOMContentLoaded/load — как в браузере после разбора страницы. */
function fireDocumentEvents() {
  ['DOMContentLoaded', 'load'].forEach((type) => {
    ((win._listeners || {})[type] || []).forEach((fn) => {
      try { fn({ type }); } catch (e) { problems.push(['window.' + type, e]); }
    });
    (document._h[type] || []).forEach((fn) => {
      try { fn({ type }); } catch (e) { problems.push(['document.' + type, e]); }
    });
  });
}
fireDocumentEvents();

/* Ленивые модули — как при первом входе в раздел. */
lazy.forEach((name) => { const err = loadFile(name); if (err) loadErrors.push(err); });

/* ====================================================== фазы исполнения */
const VIEWS = ['dashboard', 'printers', 'queue', 'orders', 'customers', 'products', 'batches',
  'documents', 'warehouses', 'shelf', 'finance', 'inventory', 'niches', 'calc', 'marketing',
  'clientbot', 'library', 'settings', 'ops10'];

function phaseViews() {
  if (!ctx.PF || typeof ctx.PF.go !== 'function') return;
  VIEWS.forEach((view) => {
    try { ctx.PF.go(view); } catch (e) { problems.push(['go(' + view + ')', e]); }
  });
}
function phaseEvents() {
  if (!ctx.PF || typeof ctx.PF.emit !== 'function') return;
  [['ready', {}], ['data', {}], ['live', { printers: [], farm: {} }], ['bootstrap', {}],
    ['finance', {}], ['events', []], ['money', {}], ['printers', []],
    ['notify', { title: 'x', detail: 'y' }], ['view', { view: 'dashboard' }]]
    .forEach(([name, payload]) => {
      try { ctx.PF.emit(name, payload); } catch (e) { problems.push(['emit(' + name + ')', e]); }
    });
}
function phaseClicks() {
  ELEMENTS.forEach((el, key) => {
    (el._listeners || []).forEach(([type, fn]) => {
      try {
        fn({ type, target: el, currentTarget: el, preventDefault() {}, stopPropagation() {},
          key: 'Enter', code: 'Enter', detail: {}, checked: true, value: '1', button: 0,
          clientX: 10, clientY: 10, closest: () => el,
          dataTransfer: { files: [], items: [], getData: () => '' } });
      } catch (e) { problems.push(['listener ' + key + '[' + type + ']', e]); }
    });
  });
}

phaseViews();
phaseEvents();
phaseClicks();

function phaseLiveRealistic() {
  if (!ctx.PF || !ctx.PF.modules || !ctx.PF.modules.printer || typeof ctx.PF.modules.printer.renderLive !== 'function') return;
  try {
    const livePrinter = {
      id: 'p1',
      name: 'P1S Test',
      connection: { connected: true, mode: 'lan', host: '192.168.1.100', last_error: '', last_message: new Date().toISOString() },
      printer: { state: 'IDLE', state_label: 'Готов', progress: 0, task: '', layer: 0, total_layers: 0, remaining_min: 0, eta: null, speed_level: 2, wifi: '-65 dBm', firmware: '01.08.00.00', problems: [], severity: '' },
      temperature: { nozzle: 205, nozzle_target: 210, bed: 60, bed_target: 60, chamber: 32 },
      fans: { part: 30, aux: 0, chamber: 0 },
      ams: {
        temperature: 38.6,
        humidity: 42,
        trays: [
          { id: '00', unit: 0, slot: 0, label: 'AMS 1 \u00b7 \u0441\u043b\u043e\u0442 1', type: 'PLA', color: '#ff0000', remain: 80, uuid: 'uuid-pla-red', active: false, present: true, bambulab: true, generic: false },
          { id: '01', unit: 0, slot: 1, label: 'AMS 1 \u00b7 \u0441\u043b\u043e\u0442 2', type: 'PETG', color: '#00ff00', remain: 5, uuid: '', active: true, present: true, bambulab: false, generic: true },
          { id: '02', unit: 0, slot: 2, label: 'AMS 1 \u00b7 \u0441\u043b\u043e\u0442 3', type: '', color: '#cbd5e1', remain: null, uuid: '', active: false, present: false, bambulab: false, generic: false },
          { id: '03', unit: 0, slot: 3, label: 'AMS 1 \u00b7 \u0441\u043b\u043e\u0442 4', type: 'ABS', color: '#0000ff', remain: 50, uuid: 'uuid-abs-blue', active: false, present: true, bambulab: true, generic: false }
        ]
      },
      camera: { available: true, demo: false, age: 1, shots: 3, fps: 15, error: '' },
      guard: { alerts: [{ title: '\u0422\u0435\u0441\u0442 \u0442\u0440\u0435\u0432\u043e\u0433\u0438', severity: 'warn', reason: '\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0441\u0442\u0435\u043d\u0434\u0430', advice: '', at: new Date().toISOString(), actions: [] }] },
      maintenance: { hours: 123, due: 1, soon: 1, tasks: [{ id: 't1', task: '\u0421\u043c\u0430\u0437\u043a\u0430', percent: 90, due: true, soon: false, left_hours: -2, every_hours: 100, last_at: new Date().toISOString() }] },
      job: null
    };
    ctx.PF.state.spools = [
      { id: 'spool-1', material: 'PLA', color_name: '\u041a\u0440\u0430\u0441\u043d\u044b\u0439', color_hex: '#ff0000', remaining_grams: 850, brand: 'Bambu', archived: 0, printer_id: 'p1', ams_slot: '0', tray_uuid: 'uuid-pla-red', percent: 85, location: 'ams' },
      { id: 'spool-2', material: 'ABS', color_name: '\u0421\u0438\u043d\u0438\u0439', color_hex: '#0000ff', remaining_grams: 400, brand: 'Generic', archived: 0, printer_id: 'p1', ams_slot: '3', tray_uuid: 'uuid-abs-blue', percent: 40, location: 'ams' },
      { id: 'spool-3', material: 'PETG', color_name: '\u0417\u0435\u043b\u0435\u043d\u044b\u0439', color_hex: '#00ff00', remaining_grams: 120, brand: 'Test', archived: 0, printer_id: '', ams_slot: '', tray_uuid: '', percent: 20, location: 'warehouse' }
    ];
    ctx.PF.state.jobs = {
      queue: [
        { id: 'job-1', name: '\u0422\u0435\u0441\u0442\u043e\u0432\u0430\u044f \u0434\u0435\u0442\u0430\u043b\u044c', file: 'test.3mf', printer_id: 'p1', state: 'queued', plate: 1, priority: 0, ams_mapping: '[]', order_id: 'order-1', created_at: new Date().toISOString() },
        { id: 'job-2', name: '\u0414\u0440\u0443\u0433\u0430\u044f \u0434\u0435\u0442\u0430\u043b\u044c', file: 'other.3mf', printer_id: '', state: 'queued', plate: 1, priority: 1, ams_mapping: '[]', created_at: new Date().toISOString() }
      ],
      history: []
    };
    ctx.PF.state.orders = [
      { id: 'order-1', number: '1001', product: '\u0422\u0435\u0441\u0442', status: 'new', material: 'PLA', color: '\u043a\u0440\u0430\u0441\u043d\u044b\u0439', file: 'test.3mf' }
    ];
    if (!ctx.PF.state.printers || !ctx.PF.state.printers.length) {
      ctx.PF.state.printers = [{ id: 'p1', name: 'P1S Test', model: 'P1S', mode: 'lan', host: '192.168.1.100', serial: 'TEST123', has_ams: 1, enabled: 1 }];
    }
    ctx.PF.state.live = {
      printers: [livePrinter],
      active: livePrinter,
      farm: { printing: 0, load: 0, queued: 2 }
    };
    ctx.PF.state.activePrinter = 'p1';
    try { ctx.PF.go('printers'); } catch (e) { if (e && e.name === 'ReferenceError') throw e; }
    try { ctx.PF.modules.printer.renderLive(); } catch (e) { problems.push(['renderLive realistic', e]); }
    try {
      const maybeTabs = ctx.PF.modules.printer.renderTabs || (typeof ctx.renderTabs === 'function' ? ctx.renderTabs : null);
      if (maybeTabs) maybeTabs();
    } catch (e) { problems.push(['renderTabs realistic', e]); }
    try { vm.runInContext('if (typeof renderTabs === "function") renderTabs();', ctx); } catch (e) { if (e && e.name === 'ReferenceError') problems.push(['renderTabs global realistic', e]); }
  } catch (e) {
    problems.push(['live-realistic setup', e]);
  }
}
phaseLiveRealistic();

/* ================================================================ отчёт */
setTimeout(() => {
  const isReference = (entry) => {
    const err = entry.error || entry[1];
    return err && err.name === 'ReferenceError';
  };
  const getError = (entry) => entry.error || entry[1];
  const blocking = [];
  loadErrors.filter(isReference).forEach((entry) => {
    blocking.push('загрузка ' + entry.file + ': ' + entry.error.message + '\n    ' +
      String(entry.error.stack || '').split('\n')[1]);
  });
  problems.filter(isReference).forEach((entry) => {
    const where = entry[0] || entry.where;
    const error = getError(entry);
    blocking.push(where + ': ' + error.message + '\n    ' +
      String(error.stack || '').split('\n')[1]);
  });
  const unique = [...new Set(blocking)];

  console.log('Панель: файлов ' + all.length + ' (сразу ' + eager.length + ', лениво ' + lazy.length + ')');
  if (VERBOSE) {
    const noise = problems.filter((entry) => {
      const err = entry.error || entry[1];
      return err && err.name !== 'ReferenceError';
    });
    const seen = new Set();
    noise.forEach((entry) => {
      const where = entry[0] || entry.where;
      const error = entry.error || entry[1];
      const line = where + ': ' + error.name + ': ' + error.message;
      if (!seen.has(line)) { seen.add(line); console.log('  [справочно] ' + line); }
    });
    loadErrors.filter((entry) => entry.error.name !== 'ReferenceError').forEach((entry) => {
      console.log('  [справочно] загрузка ' + entry.file + ': ' + entry.error.message);
    });
  }
  if (unique.length) {
    console.log('\nНЕ ОБЪЯВЛЕНЫ ПЕРЕМЕННЫЕ (' + unique.length + '):');
    unique.forEach((line) => console.log('  ' + line));
    process.exit(2);
  }
  console.log('Необъявленных переменных нет.');
  process.exit(0);
}, 400);
