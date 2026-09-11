#!/usr/bin/env node
/**
 * Каркас для запуска страницы кассы (site/cashier.html) без браузера.
 *
 * Телефона и WebView в песочнице нет, но сама страница — это один инлайн-скрипт
 * без модулей, поэтому её можно выполнить в vm с заглушкой DOM. Транспорт
 * подключается двумя способами:
 *
 *   transport: 'stub' — fetch-заглушка, страница изолирована от сети
 *                        (scripts/kassa-check.js: логика очереди и связи);
 *   transport: 'real' — настоящий fetch в живой коннектор
 *                        (scripts/kassa-lan.js: замеры на стенде).
 *
 * Origin намеренно переменный: и «ПК сменил IP», и обрыв лечатся в замере
 * именно переключением адреса.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const net = require('net');

const ROOT = path.resolve(__dirname, '..');

function inlineScript() {
  const html = fs.readFileSync(path.join(ROOT, 'site', 'cashier.html'), 'utf8');
  const m = html.match(/<script>\n"use strict";([\s\S]*?)<\/script>/);
  if (!m) throw new Error('инлайн-скрипт кассы не найден в site/cashier.html');
  return m[1];
}

function makeElement(id) {
  const classes = new Set();
  const el = {
    id, children: [], style: {}, dataset: {}, hidden: false, disabled: false,
    value: '', textContent: '', innerHTML: '', title: '', className: '', href: '',
    classList: {
      add: (...c) => c.forEach((x) => classes.add(x)),
      remove: (...c) => c.forEach((x) => classes.delete(x)),
      toggle: (c, on) => (on === undefined ? (classes.has(c) ? classes.delete(c) : classes.add(c))
        : (on ? classes.add(c) : classes.delete(c))),
      contains: (c) => classes.has(c),
    },
    _classes: classes,
    addEventListener(type, fn) { (this._h = this._h || {})[type] = fn; },
    removeEventListener() {}, setAttribute(k, v) { this['attr_' + k] = v; },
    getAttribute: (k) => (k in el ? el[k] : null),
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); },
    querySelector: () => makeElement('q'), querySelectorAll: () => [],
    closest: () => null, focus() {}, blur() {}, click() {}, remove() {},
    getBoundingClientRect: () => ({ width: 100, height: 100, top: 0, left: 0 }),
    scrollIntoView() {}, insertBefore() {}, contains: () => false, dispatchEvent: () => true,
    offsetParent: {}, offsetWidth: 100, offsetHeight: 40, firstChild: null,
  };
  return el;
}

function createStorage() {
  const map = new Map();
  return {
    map,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
    key: (i) => [...map.keys()][i] || null,
    get length() { return map.size; },
  };
}

/** Минимальный SSE-клиент поверх fetch — браузерного EventSource в node нет. */
function makeEventSourceClass(resolveUrl, onFrame, registry, live) {
  return class EventSource {
    constructor(url) {
      this.url = resolveUrl(url);
      this.readyState = 0;
      this._h = {};
      this.frames = [];
      this.onFrame = onFrame;
      this._ctl = new AbortController();
      if (registry) registry.push(this);
      if (live) this._run();
    }
    addEventListener(type, fn) { this._h[type] = fn; }
    removeEventListener(type) { delete this._h[type]; }
    close() { this.readyState = 2; this._closed = true; try { this._ctl.abort(); } catch (e) {} }
    _emit(type, data) { const fn = this._h[type]; if (fn) { try { fn({ data }); } catch (e) { this._err = e; } } }
    /** Стенд без сети: изобразить кадр потока (ping, catalog_changed, event…). */
    emitNamed(name, payload) {
      const data = typeof payload === 'string' ? payload : JSON.stringify(payload || {});
      this.frames.push({ name, payload: data, at: Date.now() });
      if (this.onFrame) this.onFrame(this, name, data);
      this._emit(name, data);
    }
    async _run() {
      try {
        const res = await fetch(this.url, {
          headers: { Accept: 'text/event-stream' }, signal: this._ctl.signal, cache: 'no-store',
        });
        this.readyState = 1;
        this._emit('open', '');
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            this._frame(buf.slice(0, idx));
            buf = buf.slice(idx + 2);
          }
        }
      } catch (e) { /* поток закрыт или прерван — это штатное завершение */ }
      this.readyState = 2;
      if (!this._closed) this._emit('error', '');
    }
    _frame(raw) {
      let name = 'message';
      const data = [];
      raw.split('\n').forEach((line) => {
        if (!line || line.startsWith(':')) return;          // комментарий-пинг
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).trim());
      });
      const payload = data.join('\n');
      this.frames.push({ name, payload, at: Date.now() });
      if (onFrame) onFrame(this, name, payload);
      this._emit(name, payload);
    }
  };
}

/**
 * Поднять страницу кассы в песочнице.
 *   opts.origin     — адрес коннектора (можно менять через setOrigin)
 *   opts.transport  — 'stub' (по умолчанию) или 'real'
 *   opts.storage    — общий Map, если нужно переиспользовать «память телефона»
 *   opts.respond    — для stub: (url, init) => {ok,status,body} | Promise
 *   opts.bridge     — объект, который стенд выдаёт за window.PfApp (мостик
 *                     оболочки: queueSave/queueLoad, offline, appInfo…)
 *   opts.setInterval— подмена таймера страницы, если он мешает замеру
 */
function createKassa(opts = {}) {
  const transport = opts.transport || 'stub';
  const storage = opts.storage || createStorage();
  const calls = [];
  const nodes = {};
  let origin = String(opts.origin || 'http://127.0.0.1:8766').replace(/\/+$/, '');

  const resolveUrl = (url) => {
    const s = String(url);
    return /^https?:/i.test(s) ? s : origin + (s.startsWith('/') ? s : '/' + s);
  };

  const documentStub = {
    visibilityState: opts.hidden ? 'hidden' : 'visible',
    hidden: !!opts.hidden,
    getElementById(id) { return (nodes[id] = nodes[id] || makeElement(id)); },
    createElement: (tag) => makeElement(tag),
    addEventListener(type, fn) { (this._h = this._h || {})[type] = fn; },
    removeEventListener() {},
    querySelector: () => makeElement('q'),
    querySelectorAll: () => [],
    body: makeElement('body'),
    documentElement: makeElement('html'),
    cookie: '',
  };

  const listeners = {};
  const win = {
    document: documentStub,
    localStorage: storage,
    sessionStorage: storage,
    navigator: { onLine: true, language: 'ru-RU', vibrate: null },
    location: { href: origin + '/cashier.html', origin, reload() {}, assign() {} },
    history: { pushState() {}, replaceState() {}, back() {} },
    addEventListener(type, fn) { listeners[type] = fn; },
    removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    alert() {}, confirm: () => true, prompt: () => 'дубль',
    isSecureContext: false, devicePixelRatio: 1, innerWidth: 360, innerHeight: 740,
  };
  // Мостик оболочки Android (window.PfApp) — если стенд его подставил.
  if (opts.bridge) { win.PfApp = opts.bridge; }
  win.window = win;
  win.self = win;
  win.globalThis = win;

  function record(url, ms, extra) { calls.push(Object.assign({ url, ms, at: Date.now() }, extra || {})); }

  async function stubFetch(url, init) {
    const reply = typeof opts.respond === 'function' ? await opts.respond(url, init) : (opts.respond || {});
    if (reply && reply.reject) throw reply.reject;
    const body = typeof reply.body === 'string' ? reply.body : JSON.stringify(reply.body || {});
    const status = reply.status || 200;
    return { ok: reply.ok !== false && status < 400, status, text: () => Promise.resolve(body) };
  }

  function realFetch(url, init) {
    return fetch(url, init || {});
  }

  function fetchStub(url, init) {
    const full = resolveUrl(url);
    const t0 = Date.now();
    let bodyText = '';
    try { bodyText = init && init.body ? String(init.body) : ''; } catch (e) {}
    const send = transport === 'real' ? realFetch : stubFetch;
    return Promise.resolve()
      .then(() => send(full, init))
      .then(async (res) => {
        const text = await res.text();
        record(full, Date.now() - t0, {
          status: res.status, bytes: Buffer.byteLength(text, 'utf8'),
          body: bodyText, reply: text,
        });
        return { ok: res.ok, status: res.status, text: () => Promise.resolve(text) };
      })
      .catch((err) => {
        record(full, Date.now() - t0, { status: 0, body: bodyText, error: String(err && err.message || err) });
        throw err;
      });
  }

  const context = vm.createContext({
    console, JSON, Math, Date, Number, String, Boolean, Object, Array, isFinite, isNaN,
    parseFloat, parseInt, encodeURIComponent, decodeURIComponent, unescape, escape,
    setTimeout, clearTimeout, setInterval: opts.setInterval || (() => 0), clearInterval,
    fetch: fetchStub, AbortController, TextDecoder, Promise, RegExp, Map, Set,
    // Error/TypeError хостовые: fetch в node бросает TypeError, и страница
    // отличает «сети нет» от «сервер ответил ошибкой» именно по нему.
    Error, TypeError, RangeError, encodeURI, Blob: undefined, URL,
    document: documentStub, localStorage: storage, navigator: win.navigator,
    location: win.location, history: win.history, window: win, self: win, globalThis: win,
  });
  for (const key of Object.getOwnPropertyNames(globalThis)) {
    if (!(key in context) && !['global', 'process', 'require', 'module'].includes(key)) {
      try { context[key] = globalThis[key]; } catch (e) {}
    }
  }
  const sources = [];
  context.EventSource = makeEventSourceClass(resolveUrl, opts.onFrame, sources, transport === 'real');
  // страница обращается и к голым именам, и через window.* — держим оба пути
  win.EventSource = context.EventSource;
  win.AbortController = AbortController;
  win.fetch = fetchStub;
  win.setTimeout = setTimeout;
  win.clearTimeout = clearTimeout;
  win.JSON = JSON;
  context.window = win;

  const kassa = {
    context, win, nodes, storage, calls, listeners, sources,
    get origin() { return origin; },
    setOrigin(next) {
      origin = String(next).replace(/\/+$/, '');
      win.location.origin = origin;
      win.location.href = origin + '/cashier.html';
      context.location = win.location;
      return origin;
    },
    ev: (code) => vm.runInContext(code, context),
    run: () => vm.runInContext(inlineScript(), context, { filename: 'cashier-inline.js' }),
    async api(path, body) {
      const init = body
        ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
        : { method: 'GET' };
      const t0 = Date.now();
      const res = await fetchStub(path, init);
      const text = await res.text();
      return {
        status: res.status, ok: res.ok, ms: Date.now() - t0,
        bytes: Buffer.byteLength(text, 'utf8'),
        json: text ? JSON.parse(text) : {},
      };
    },
    /** Дождаться условия в контексте страницы: таймеры страницы асинхронны. */
    async waitFor(expr, timeoutMs = 20000, stepMs = 25) {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        if (vm.runInContext(expr, context)) return true;
        if (Date.now() > deadline) return false;
        await new Promise((r) => setTimeout(r, stepMs));
      }
    },
    callsFor(pathPart) { return calls.filter((c) => c.url.includes(pathPart)); },
    lastCall() { return calls[calls.length - 1]; },
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
  };
  return kassa;
}

/**
 * TCP-прокси «гнилой Wi-Fi»: соединение принимается, запрос уходит на живой
 * коннектор, но ответ по выбранному маршруту не возвращается. Так на живом коде
 * воспроизводится обрыв ровно в момент продажи (запрос ушёл, ответ потерян).
 */
function startRottenProxy({ listen, target, dropPath = '/api/cashier/sell' }) {
  const dropped = [];
  const server = net.createServer((client) => {
    const up = net.connect({ host: '127.0.0.1', port: target });
    let head = Buffer.alloc(0);
    let done = false;      // заголовки разобраны
    let dropping = false;  // этот запрос «уронили»: ответ клиенту не идёт
    const stop = () => { try { client.destroy(); } catch (e) {} try { up.destroy(); } catch (e) {} };
    client.on('data', (chunk) => {
      if (!done) {
        head = Buffer.concat([head, chunk]);
        if (!head.includes('\r\n\r\n')) return;
        done = true;
        const line = head.toString('utf8').split('\r\n')[0];
        if (line.includes(dropPath)) {
          dropping = true;
          dropped.push(line);
          up.write(head);                    // сервер запрос получит и проведёт
          setTimeout(stop, 250);             // а ответ до кассы не доедет
          return;
        }
      }
      if (!dropping) up.write(chunk);
    });
    up.on('data', (chunk) => { if (!dropping) client.write(chunk); });
    client.on('error', stop);
    up.on('error', stop);
    client.on('close', () => { try { up.destroy(); } catch (e) {} });
  });
  return new Promise((resolve) => server.listen(listen, '127.0.0.1', () => resolve({ server, dropped, close: () => server.close() })));
}

/** Чёрная дыра: порт принимает соединение и молчит — таймаут запроса. */
function startBlackHole(listen) {
  const server = net.createServer((client) => { client.on('error', () => {}); });
  return new Promise((resolve) => server.listen(listen, '127.0.0.1', () => resolve({ server, close: () => server.close() })));
}

module.exports = { createKassa, startRottenProxy, startBlackHole, inlineScript, ROOT };
