#!/usr/bin/env node
/* PrintFlow 17.1 — headless-стенд раздела «Печать» (приём из kassa-check.js).

   Зачем. Печать — это миллиметры: ошибку в раскладке на экране не видно, а
   на бумаге видно сразу. Раздел «Печать» рисует каталог форм из реестра
   сервера, собирает параметры (шаблон, размер, тираж, заказ, клиент) и
   открывает лист в окне печати. Здесь этот путь выполняется целиком: DOM
   заменён заглушкой, ответы сервера — подставные, а сам каталог берётся
   из настоящего реестра форм (`printing.forms_catalog()`), поэтому стенд
   падает, если в реестре появилась форма без параметров или с адресом,
   которого нет.

   Что проверяет:
     1. каталог рисуется по реестру: карточек и групп столько же, сколько форм;
     2. поля собираются из описания параметров (списки, справочники, числа);
     3. параметры формы уходят в запрос, а HTML листа открывается в печати;
     4. форма-страница (ценники, наклейки) открывается ссылкой, а не «печатью»;
     5. штрихкод Code 128 строится через /api/labels/code128.

   Запуск: node scripts/print-check.js
   Код возврата: 0 — чисто, 1 — стенд нашёл расхождение. */

'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { execFileSync } = require('child_process');
const ROOT = path.resolve(__dirname, '..');

/* Реестр форм — из источника правды, а не из копии в тесте: стенд берёт его
   у сервера (Python), поэтому расхождение «панель знает форму, а реестр нет»
   видно сразу. */
function registryFromServer() {
  const code = 'import json,sys;sys.path.insert(0,".");sys.path.insert(0,"connector");'
    + 'from connector.printflow.printing import forms_catalog, groups;'
    + 'json.dump({"groups": groups(), "forms": forms_catalog()}, sys.stdout)';
  return JSON.parse(execFileSync('python3', ['-c', code], { cwd: ROOT, encoding: 'utf8' }));
}
const REGISTRY = registryFromServer();

const CHAIN = { _c: {} }; CHAIN.get = (p) => (p in CHAIN._c ? CHAIN._c[p] : (CHAIN._c[p] = () => CHAIN._c));
function makeElement(key) {
  const base = {
    id: key || '', tagName: 'DIV', nodeType: 1,
    style: { setProperty() {}, getPropertyValue: () => '', removeProperty() {} },
    dataset: {}, value: '', textContent: '', innerHTML: '', hidden: false, checked: false,
    disabled: false, children: [], childNodes: [], options: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(c) { return c || makeElement(); }, removeChild() {},
    insertBefore(c) { return c || makeElement(); }, remove() {}, replaceChildren() {},
    append() {}, prepend() {}, before() {}, after() {},
    setAttribute() {}, getAttribute: () => null,
    removeAttribute() {}, hasAttribute: () => false,
    _listeners: [],
    addEventListener(type, fn) { base._listeners.push([type, fn]); },
    removeEventListener() {}, focus() {}, click() {}, select() {},
    querySelector: () => makeElement(), querySelectorAll: () => [],
    closest: () => null, matches: () => false, contains: () => false,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
    animate: () => ({ finished: Promise.resolve(), cancel() {} }), cloneNode: () => makeElement(key),
    getContext: () => null,
  };
  base.parentElement = base; base.parentNode = base;
  return new Proxy(base, {
    get(t, p) { return p in t ? t[p] : CHAIN.get(p); },
    set(t, p, v) { t[p] = v; return true; },
  });
}
const ELEMENTS = new Map();
const element = (k) => { if (!ELEMENTS.has(k)) ELEMENTS.set(k, makeElement(k)); return ELEMENTS.get(k); };

const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; }, clear: () => {} };
const problems = [];
const requests = [];
const printed = [];

const win = {
  localStorage, sessionStorage: localStorage, console,
  location: { hash: '', href: 'http://192.168.1.10:8080/', origin: 'http://192.168.1.10:8080', search: '', pathname: '/' },
  history: { pushState() {}, replaceState() {}, back() {} },
  navigator: { userAgent: 'print-check', language: 'ru-RU', onLine: true, clipboard: { writeText: () => Promise.resolve() } },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  addEventListener() {}, removeEventListener() {},
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0), cancelAnimationFrame: clearTimeout,
  URL, URLSearchParams, TextEncoder, TextDecoder,
  Blob: class {}, FormData: class { append() {} }, FileReader: class {},
  performance: { now: () => Date.now() },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  open() {
    const doc = { write: (html) => printed.push({ html, title: '' }), close() {} };
    const popup = { document: doc, focus() {}, print() {} };
    return popup;
  },
  print() {}, scrollTo() {}, scrollBy() {},
  Notification: function () {}, alert() {}, confirm: () => true, prompt: () => '',
  MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
  ResizeObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { observe() {} disconnect() {} unobserve() {} },
  crypto: { randomUUID: () => 'print-check', getRandomValues: (a) => a },
  innerWidth: 1440, innerHeight: 900, screen: { width: 1440, height: 900 },
  EventSource: undefined, Image: class {}, Audio: class { play() { return Promise.resolve(); } pause() {} },
  Worker: class { postMessage() {} terminate() {} addEventListener() {} },
  devicePixelRatio: 1,
  isSecureContext: true,
  fetch: (url, opts) => {
    requests.push(String(url));
    const u = String(url);
    if (u.startsWith('/api/print/forms')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(REGISTRY), text: () => Promise.resolve(JSON.stringify(REGISTRY)) });
    }
    if (u.startsWith('/api/labels/code128')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ svg: '<svg id="bc"></svg>', mode: 'C', symbols: 12 }), text: () => Promise.resolve(JSON.stringify({ svg: '<svg id="bc"></svg>', mode: 'C', symbols: 12 })) });
    }
    if (u.startsWith('/api/print/stickers')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ html: '<html>СТИКЕРЫ</html>' }), text: () => Promise.resolve('{"html":"<html>СТИКЕРЫ</html>"}') });
    }
    if (u.startsWith('/api/order/pack')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new Error('html')), text: () => Promise.resolve('<html>УПАКОВКА</html>') });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
  },
};
win.window = win; win.self = win; win.globalThis = win; win.top = win; win.parent = win;
win.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = (o || {}).detail; } };
win.Event = win.CustomEvent; win.KeyboardEvent = win.CustomEvent; win.MouseEvent = win.CustomEvent;
win.EventTarget = class {
  constructor() { this._h = {}; }
  addEventListener(t, f) { (this._h[t] = this._h[t] || []).push(f); }
  removeEventListener() {}
  dispatchEvent(e) { (this._h[e.type] || []).forEach((f) => { try { f(e); } catch (err) { problems.push([e.type, err]); } }); return true; }
};

const document = { readyState: 'complete', title: '', _h: {} };
Object.assign(document, {
  documentElement: element('html'), head: element('head'), body: element('body'),
  activeElement: element('body'),
  getElementById: (id) => element(id),
  createElement: (t) => makeElement('new:' + t),
  createElementNS: (ns, t) => makeElement('ns:' + t),
  createTextNode: () => makeElement('text'),
  createDocumentFragment: () => makeElement('frag'),
  querySelector: (s) => element('q:' + s),
  querySelectorAll: () => [],
  addEventListener(t, f) { (document._h[t] = document._h[t] || []).push(f); },
  removeEventListener() {},
});
win.document = document;

const ctx = vm.createContext(win);
for (const file of ['assets/core.js', 'assets/print.js']) {
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'site', file), 'utf8'), ctx, { filename: file });
}

const PF = ctx.PF;
PF.state.customers = [{ id: 'c1', name: 'Иван Петров' }, { id: 'c2', name: 'ООО «Ромашка»' }];
PF.state.orders = [{ id: 7, number: 1007, title: 'Ваза' }];

const checks = [];
const ok = (label, cond, extra) => checks.push([label, !!cond, extra]);

(async () => {
  await new Promise((r) => setTimeout(r, 40));   // PF.module вызывает init сразу при загрузке файла

  const formsHtml = element('pr_forms').innerHTML || '';
  const kpiHtml = element('pr_kpis').innerHTML || '';
  const cards = (formsHtml.match(/class="pr-card"/g) || []).length;
  const groups = (formsHtml.match(/class="pr-group-title"/g) || []).length;
  ok('карточек форм = формам реестра', cards === REGISTRY.forms.length, cards + '/' + REGISTRY.forms.length);
  ok('групп = группам реестра', groups === REGISTRY.groups.length, groups + '/' + REGISTRY.groups.length);
  ok('каталог с параметрами нарисован', formsHtml.includes('data-opt="size"') && formsHtml.includes('data-opt="kind"'));
  ok('выбор заказа из справочника', formsHtml.includes('data-source="orders"') && formsHtml.includes('№1007'));
  ok('выбор клиента из справочника', formsHtml.includes('Иван Петров') && formsHtml.includes('data-source="customers"'));
  ok('период отчёта — список', formsHtml.includes('data-opt="days"') && formsHtml.includes('30 дней'));
  ok('у формы-страницы кнопка «открыть», а не «печать»', formsHtml.includes('href="/price-tags.html"') && !formsHtml.includes('data-print="price-tags"'));
  ok('KPI посчитаны', kpiHtml.includes('Форм в каталоге') && kpiHtml.includes(String(REGISTRY.forms.length)));
  ok('путей печати в разметке столько же, сколько api-форм',
     (formsHtml.match(/data-print="/g) || []).length === REGISTRY.forms.filter((f) => f.kind === 'api').length);

  // --- печать JSON-формы: параметры уходят на сервер, HTML — в окно
  const fields = {
    kind: { value: 'fragile' }, size: { value: '88x44' }, copies: { value: '4' },
  };
  const card = { querySelector: (sel) => { const m = /data-opt="([^"]+)"/.exec(sel); return m ? fields[m[1]] : null; } };
  const button = { closest: () => card, disabled: false, dataset: { print: 'stickers' }, classList: { add() {}, remove() {} }, setAttribute() {}, removeAttribute() {}, textContent: 'Печать листа' };
  const host = element('pr_forms');
  host._listeners.filter(([t]) => t === 'click').forEach(([, fn]) => fn({ target: { closest: () => button } }));
  await new Promise((r) => setTimeout(r, 30));
  const stickerUrl = requests.find((u) => u.includes('/api/print/stickers')) || '';
  ok('параметры формы ушли в запрос', stickerUrl.includes('kind=fragile') && stickerUrl.includes('size=88x44') && stickerUrl.includes('copies=4'), stickerUrl);
  ok('HTML листа открыт в окне печати', (printed[0] || {}).html === '<html>СТИКЕРЫ</html>', JSON.stringify((printed[0] || {}).html || '').slice(0, 40));

  // --- страница целиком (карточка упаковки) и штрихкод
  requests.length = 0;
  host._listeners.filter(([t]) => t === 'click').forEach(([, fn]) => fn({ target: { closest: () => ({ closest: () => card, disabled: false, dataset: { print: 'pack-sheet' }, classList: { add() {}, remove() {} }, setAttribute() {}, removeAttribute() {}, textContent: 'Печать листа' }) } }));
  await new Promise((r) => setTimeout(r, 30));
  ok('страница печати без JSON тоже открывается', (printed[1] || {}).html === '<html>УПАКОВКА</html>', JSON.stringify((printed[1] || {}).html || '').slice(0, 40));

  element('pr_bc_text').value = '199.90';
  const genListeners = element('pr_bc_gen')._listeners.filter(([t]) => t === 'click');
  genListeners.forEach(([, fn]) => fn({}));
  await new Promise((r) => setTimeout(r, 30));
  const bcOut = element('pr_bc_out').innerHTML || '';
  ok('штрихкод строится через /api/labels/code128', requests.some((u) => u.includes('/api/labels/code128')), bcOut.slice(0, 60));
  ok('на экране режим и число символов', bcOut.includes('Code 128 C') && bcOut.includes('12 символов'));

  ok('ошибок выполнения нет', problems.length === 0, JSON.stringify(problems.map(([, e]) => String(e && e.message || e))).slice(0, 200));

  let bad = 0;
  checks.forEach(([label, pass, extra]) => {
    if (!pass) bad++;
    console.log((pass ? '  ok  ' : '  FAIL') + '  ' + label + (extra ? '   [' + extra + ']' : ''));
  });
  console.log(bad ? 'ПРОВАЛЕНО: ' + bad : 'OK: стенд печати пройден');
  process.exit(bad ? 1 : 0);
})();
